import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from contextlib import contextmanager
import uuid


class BackgroundRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('background_runtime'), 'owned worker lifecycle is missing')
        import background_runtime
        self.runtime = background_runtime

    def test_endpoint_rejects_remote_credentials_query_and_wrong_path(self):
        for url in ['http://example.com:9000/api/v1/acp', 'http://localhost:80/api/v1/acp',
                    'http://u:p@127.0.0.1:80/api/v1/acp', 'http://127.0.0.1:80/api/v1/acp?token=x',
                    'http://127.0.0.1:80/other']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.runtime.validate_endpoint(url)
        self.assertEqual(self.runtime.validate_endpoint('http://127.0.0.1:9000/api/v1/acp'),
                         'http://127.0.0.1:9000/api/v1/acp')

    def test_foreign_worker_cannot_be_stopped(self):
        with patch.object(self.runtime, 'rpc') as rpc:
            with self.assertRaises(ValueError):
                self.runtime.stop_owned({'owner': 'user-session', 'sidecar_pipe': 'pipe'})
            rpc.assert_not_called()

    def test_worker_bootstrap_and_cleanup_on_body_failure(self):
        calls = []
        def rpc(pipe, method, params=None, **kwargs):
            calls.append((method, params))
            if method == 'session.create':
                return {'sessionId': params['sessionId'], 'acpEndpoint': 'http://127.0.0.1:9000/api/v1/acp',
                        'pid': 42, 'runtimeId': 'runtime-42'}
            return True
        with TemporaryDirectory() as tmp, patch.object(self.runtime, 'find_sidecar', return_value=('pipe', 10, Path(tmp)/'WorkBuddy.exe')), patch.object(self.runtime, 'rpc', side_effect=rpc):
            with self.assertRaisesRegex(RuntimeError, 'body failure'):
                with self.runtime.owned_worker(Path(tmp)) as (server, metadata):
                    self.assertEqual(server.runtime_pid, 42)
                    self.assertEqual(metadata['runtime_id'], 'runtime-42')
                    raise RuntimeError('body failure')
        params = calls[0][1]
        self.assertEqual(params['port'], 0)
        self.assertNotIn('--no-session-persistence', params['args'])
        self.assertNotIn('DISABLE_REQUEST_VALIDATION', params['env'])
        self.assertEqual(calls[-1][0], 'session.kill')
        self.assertEqual(calls[-1][1]['expectedRuntimeId'], 'runtime-42')

    def test_requested_model_is_passed_as_one_argument_to_owned_worker(self):
        self.assertIn('model_id', __import__('inspect').signature(self.runtime.owned_worker).parameters)
        def rpc(pipe, method, params=None, **kwargs):
            if method == 'session.create':
                self.assertEqual(params['args'][-2:], ['--model', 'kimi-k3-2'])
                return {'sessionId': params['sessionId'], 'acpEndpoint': 'http://127.0.0.1:9000/api/v1/acp',
                        'pid': 42, 'runtimeId': 'runtime-42'}
            return True
        with TemporaryDirectory() as tmp, patch.object(self.runtime, 'find_sidecar', return_value=('pipe', 10, Path(tmp)/'WorkBuddy.exe')), patch.object(self.runtime, 'rpc', side_effect=rpc):
            with self.runtime.owned_worker(Path(tmp), model_id='kimi-k3-2'):
                pass

    def test_creation_failure_still_cleans_exact_owner(self):
        calls = []
        def rpc(pipe, method, params=None, **kwargs):
            calls.append((method, params))
            if method == 'session.create':
                raise TimeoutError('readiness timeout')
            return True
        with TemporaryDirectory() as tmp, patch.object(self.runtime, 'find_sidecar', return_value=('pipe', 10, Path(tmp)/'WorkBuddy.exe')), patch.object(self.runtime, 'rpc', side_effect=rpc):
            with self.assertRaises(TimeoutError):
                with self.runtime.owned_worker(Path(tmp)):
                    self.fail('must not yield')
        self.assertEqual(calls[-1][0], 'session.kill')
        self.assertEqual(calls[-1][1]['sessionId'], calls[0][1]['sessionId'])

    def test_recovery_after_sidecar_and_owned_process_exited(self):
        self.assertTrue(hasattr(self.runtime, 'SidecarUnavailable'), 'missing stale worker recovery')
        with patch.object(self.runtime, 'rpc', side_effect=self.runtime.SidecarUnavailable()), patch.object(self.runtime, 'process_exists', return_value=False):
            self.runtime.stop_owned({'owner': 'codex-background-'+'a'*32, 'sidecar_pipe': 'old-pipe', 'pid': 123})

    def test_missing_sidecar_does_not_authorize_killing_or_forgetting_live_pid(self):
        self.assertTrue(hasattr(self.runtime, 'SidecarUnavailable'), 'missing stale worker recovery')
        with patch.object(self.runtime, 'rpc', side_effect=self.runtime.SidecarUnavailable()), patch.object(self.runtime, 'process_exists', return_value=True):
            with self.assertRaises(self.runtime.SidecarUnavailable):
                self.runtime.stop_owned({'owner': 'codex-background-'+'a'*32, 'sidecar_pipe': 'old-pipe', 'pid': 123})


class BackgroundSessionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('background_sessions'), 'persistent isolated sessions are missing')
        import background_sessions
        self.sessions = background_sessions
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        self.store_patch = patch.object(self.sessions, 'STORE', self.cwd/'registry')
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)

    def install_runtime(self, *, fail=False, blocked=None, close_fail=False, actual_model='kimi-k3-2'):
        observed = {'closed': 0, 'stopped': 0, 'prompts': [], 'loaded': [], 'models': []}
        @contextmanager
        def worker(cwd, on_created=None, model_id=''):
            observed['models'].append(model_id)
            meta = {'owner': 'codex-background-'+'a'*32, 'sidecar_pipe': 'pipe', 'runtime_id': 'r'}
            if on_created:
                on_created(meta)
            try:
                yield object(), meta
            finally:
                observed['stopped'] += 1
        class Client:
            def __init__(self, *args, **kwargs):
                self.permissions = []
                self.session_id = None
            def connect(self): pass
            def close(self):
                observed['closed'] += 1
                if close_fail:
                    raise OSError('transport close failed')
            def new_session(self, cwd, as_task=False):
                self.session_id = str(uuid.uuid4())
                return self.session_id
            def load_session(self, sid, cwd, as_task=False):
                observed['loaded'].append(sid)
                self.session_id = sid
                return sid
            def notify(self, *args, **kwargs): pass
            def request(self, method, params, **kwargs):
                if method != 'session/load':
                    raise AssertionError('Model selection must be verified through native metadata')
                return {'models': {'currentModelId': actual_model,
                                   'availableModels': [{'modelId': actual_model}]}}
            def prompt(self, text, **kwargs):
                observed['prompts'].append(text)
                if blocked:
                    blocked.wait(5)
                if fail:
                    raise TimeoutError('uncertain submission')
                return {'answer': 'test answer', 'result': {'stopReason': 'end_turn'}}
        self.worker_patch = patch.object(self.sessions, 'owned_worker', worker)
        self.client_patch = patch.object(self.sessions.bridge, 'ScopedClient', Client)
        self.worker_patch.start(); self.client_patch.start()
        self.addCleanup(self.worker_patch.stop); self.addCleanup(self.client_patch.stop)
        return observed

    def result(self, started):
        return self.sessions.bridge.tasks[started['task_id']]['future'].result(timeout=10)

    def test_new_resume_keeps_identity_and_releases_each_worker(self):
        observed = self.install_runtime()
        first = self.sessions.background_start('remember A', str(self.cwd))
        result = self.result(first)
        self.assertEqual(result['state'], 'completed')
        second = self.sessions.background_start('recall A', str(self.cwd), first['session_id'])
        result2 = self.result(second)
        self.assertEqual(result2['native_session_id'], result['native_session_id'])
        self.assertEqual(observed['loaded'], [result['native_session_id']])
        self.assertEqual(observed['stopped'], 2)
        self.assertEqual(observed['closed'], 2)
        self.assertEqual(len(self.sessions.background_history(first['session_id'])['turns']), 2)
        self.assertFalse(result2['native_history_visible'])

    def test_explicit_model_verified_before_prompt_and_preserved_on_resume(self):
        self.assertIn('model_id', __import__('inspect').signature(self.sessions.background_start).parameters)
        observed = self.install_runtime()
        first = self.sessions.background_start('first', str(self.cwd), model_id='kimi-k3-2')
        result = self.result(first)
        self.assertEqual(result['actual_model_id'], 'kimi-k3-2')
        self.assertTrue(result['model_verified'])
        second = self.sessions.background_start('next', str(self.cwd), first['session_id'])
        self.result(second)
        self.assertEqual(observed['models'], ['kimi-k3-2', 'kimi-k3-2'])
        self.assertEqual(self.sessions.background_history(first['session_id'])['model_id'], 'kimi-k3-2')

    def test_model_mismatch_never_sends_prompt_and_still_releases_worker(self):
        self.assertIn('model_id', __import__('inspect').signature(self.sessions.background_start).parameters)
        observed = self.install_runtime(actual_model='auto')
        first = self.sessions.background_start('must not send', str(self.cwd), model_id='kimi-k3-2')
        result = self.result(first)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['error_code'], 'model_verification_failed')
        self.assertFalse(result['submission_may_have_started'])
        self.assertTrue(result['worker_released'])
        self.assertFalse(result['requires_recovery'])
        self.assertEqual(observed['prompts'], [])

    def test_invalid_model_rejected_before_queueing(self):
        self.assertIn('model_id', __import__('inspect').signature(self.sessions.background_start).parameters)
        for value in ('--system-prompt', 'model\nargument', 'model id', 'x'*201):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.sessions.background_start('hi', str(self.cwd), model_id=value)

    def test_resume_cannot_change_cwd_or_accept_foreign_id(self):
        self.install_runtime()
        first = self.sessions.background_start('hi', str(self.cwd))
        self.result(first)
        other = self.cwd/'other'; other.mkdir()
        with self.assertRaises(ValueError):
            self.sessions.background_start('hi', str(other), first['session_id'])
        with self.assertRaises(ValueError):
            self.sessions.background_start('hi', str(self.cwd), '../outside')
        with self.assertRaises(ValueError):
            self.sessions.background_start('hi', str(self.cwd), str(uuid.uuid4()))

    def test_uncertain_prompt_never_retried_and_requires_recovery(self):
        observed = self.install_runtime(fail=True)
        first = self.sessions.background_start('hi', str(self.cwd))
        result = self.result(first)
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(result['retry_safe'])
        with self.assertRaisesRegex(RuntimeError, 'recover'):
            self.sessions.background_start('hi', str(self.cwd), first['session_id'])
        self.assertEqual(len(observed['prompts']), 1)
        self.assertEqual(observed['stopped'], 1)

    def test_duplicate_session_start_is_rejected_while_queued_or_running(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.install_runtime(blocked=gate)
        first = self.sessions.background_start('hi', str(self.cwd))
        try:
            with self.assertRaises(RuntimeError):
                self.sessions.background_start('another', str(self.cwd), first['session_id'])
        finally:
            gate.set()
            self.result(first)

    def test_unknown_tool_is_denied_by_existing_scope(self):
        self.assertFalse(self.sessions.bridge.permitted('Bash', {'command': 'echo hi'}, self.cwd))
        self.assertFalse(self.sessions.bridge.permitted('Read', {'file_path': str(self.cwd.parent/'secret')}, self.cwd))

    def test_close_error_does_not_leak_lock_or_lose_terminal_record(self):
        self.install_runtime(close_fail=True)
        first = self.sessions.background_start('hi', str(self.cwd))
        result = self.result(first)
        self.assertEqual(result['state'], 'failed')
        handle = self.sessions.acquire_session(first['session_id'])
        self.sessions.release_session(handle)
        self.assertEqual(self.sessions.background_history(first['session_id'])['state'], 'failed')

    def test_cancel_without_acp_ack_stops_only_owned_worker(self):
        self.assertTrue(hasattr(self.sessions, 'CANCEL_GRACE_SECONDS'))
        gate = threading.Event()
        entered = threading.Event()
        observed = self.install_runtime(blocked=gate, fail=True)
        original_save = self.sessions.save_record
        def save(record):
            original_save(record)
            if record['turns'][-1]['state'] == 'submitted':
                entered.set()
        def stop(metadata):
            self.assertEqual(metadata['owner'], 'codex-background-'+'a'*32)
            gate.set()
        with patch.object(self.sessions, 'CANCEL_GRACE_SECONDS', 0.01), patch.object(self.sessions, 'save_record', side_effect=save), patch.object(self.sessions, 'stop_owned', side_effect=stop) as stop_mock:
            first = self.sessions.background_start('hi', str(self.cwd))
            self.assertTrue(entered.wait(2))
            self.sessions.bridge.tasks[first['task_id']]['cancel'].set()
            result = self.result(first)
            self.assertEqual(result['state'], 'cancelled')
            stop_mock.assert_called_once()
        self.assertEqual(observed['stopped'], 1)


if __name__ == '__main__':
    unittest.main()
