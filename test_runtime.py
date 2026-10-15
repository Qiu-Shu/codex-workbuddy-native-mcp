import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import native_runtime as runtime
import local_bridge as bridge

SID = '11111111-1111-4111-8111-111111111111'


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root / 'WorkBuddy.exe'
        self.exe.touch()
        self.cli = self.root / 'resources/app.asar.unpacked/cli/bin/codebuddy'
        self.cli.parent.mkdir(parents=True)
        self.cli.touch()
        self.cwd = self.root / 'workspace'
        self.cwd.mkdir()
        self.database = self.root / 'workbuddy.db'
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('CREATE TABLE sessions (id TEXT,cwd TEXT,deleted_at TEXT)')
            db.execute('INSERT INTO sessions VALUES (?,?,NULL)', (SID, str(self.cwd)))
            db.commit()

    def test_cold_start_uses_dynamic_port_and_original_session(self):
        calls = []
        host = {'sessionId': 'codex-native-' + SID, 'pid': 99,
                'acpEndpoint': 'http://127.0.0.1:54321/api/v1/acp', 'runtimeId': 'generation-1'}
        def rpc(pipe, method, params=None):
            calls.append((method, params))
            return [] if method == 'session.list' else host
        with (patch.object(runtime, 'rpc', side_effect=rpc),
              patch.object(runtime, '_candidate_sidecars', return_value=[('file', 'pipe', 10)]),
              patch.object(runtime, '_process_executable', return_value=self.exe)):
            endpoint = runtime.resolve_runtime(SID, str(self.cwd), create=True)
        self.assertEqual(endpoint, host['acpEndpoint'])
        params = calls[1][1]
        self.assertEqual(params['port'], 0)
        self.assertEqual(params['sessionId'], 'codex-native-' + SID)
        self.assertEqual(params['cwd'], str(self.cwd))
        self.assertIn(SID, params['args'])
        self.assertNotIn('--permission-mode-before-plan', params['args'])

    def test_status_does_not_create_runtime_and_other_chats_are_ignored(self):
        calls = []
        def rpc(pipe, method, params=None):
            calls.append(method)
            return [{'sessionId': 'unrelated-chat', 'acpEndpoint': 'http://127.0.0.1:55555/api/v1/acp'}]
        with (patch.object(runtime, 'rpc', side_effect=rpc),
              patch.object(runtime, '_candidate_sidecars', return_value=[('file', 'pipe', 10)]),
              patch.object(runtime, '_process_executable', return_value=self.exe)):
            self.assertIsNone(runtime.resolve_runtime(SID, str(self.cwd), create=False))
        self.assertEqual(calls, ['session.list'])

    def test_existing_runtime_is_reused_without_spawn(self):
        host = {'sessionId': 'codex-native-' + SID, 'pid': 99,
                'acpEndpoint': 'http://127.0.0.1:54321/api/v1/acp'}
        with (patch.object(runtime, 'rpc', return_value=[host]) as rpc,
              patch.object(runtime, '_candidate_sidecars', return_value=[('file', 'pipe', 10)]),
              patch.object(runtime, '_process_executable', return_value=self.exe)):
            self.assertEqual(runtime.resolve_runtime(SID, str(self.cwd), create=True), host['acpEndpoint'])
        self.assertEqual(rpc.call_count, 1)

    def test_rejects_foreign_endpoint_and_deleted_binding(self):
        for value in ['https://remote.example/api/v1/acp', 'http://0.0.0.0:123/api/v1/acp',
                      'http://user:pass@127.0.0.1:123/api/v1/acp']:
            with self.assertRaises(ValueError):
                runtime.validate_endpoint(value)
        self.assertEqual(runtime.session_cwd(SID, self.database), str(self.cwd))
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('UPDATE sessions SET deleted_at="deleted"')
            db.commit()
        with self.assertRaises(RuntimeError):
            runtime.session_cwd(SID, self.database)

    def test_searches_later_sidecars_before_creating(self):
        host = {'sessionId': 'codex-native-' + SID, 'pid': 99,
                'acpEndpoint': 'http://127.0.0.1:54321/api/v1/acp'}
        for create in [False, True]:
            with (self.subTest(create=create),
                  patch.object(runtime, 'rpc', side_effect=[[], [host]]) as rpc,
                  patch.object(runtime, '_candidate_sidecars', return_value=[
                      ('first', 'pipe1', 10), ('second', 'pipe2', 20)]),
                  patch.object(runtime, '_process_executable', return_value=self.exe)):
                self.assertEqual(runtime.resolve_runtime(SID, str(self.cwd), create=create), host['acpEndpoint'])
                self.assertEqual([call.args[1] for call in rpc.call_args_list], ['session.list', 'session.list'])

    def test_missing_endpoint_is_restored_before_task_but_busy_chat_is_not(self):
        (self.root / 'desktop_session.json').write_text(json.dumps({'session_id': SID}))
        for state in ['idle', 'busy']:
            with (
                self.subTest(state=state),
                patch.object(bridge, 'ROOT', self.root),
                patch.object(bridge, 'ACTIVE_PATH', self.root / 'active.json'),
                patch.object(bridge.live, 'find_live_endpoint', return_value=None),
                patch.object(runtime, 'session_cwd', return_value=str(self.cwd)),
                patch.object(runtime, 'resolve_runtime', return_value='http://127.0.0.1:54321/api/v1/acp') as resolve,
                patch.object(bridge, 'native_turn_state', return_value={'state': state}),
                patch.object(bridge.socket, 'create_connection'),
            ):
                if state == 'idle':
                    server, sid, cwd = bridge.discover_native_server(activate=True)
                    self.assertEqual(server.acp_endpoint, 'http://127.0.0.1:54321/api/v1/acp')
                    self.assertEqual(sid, SID)
                    resolve.assert_called_once_with(SID, str(self.cwd), create=True)
                else:
                    with self.assertRaisesRegex(RuntimeError, 'busy'):
                        bridge.discover_native_server(activate=True)
                    resolve.assert_not_called()


if __name__ == '__main__':
    unittest.main()
