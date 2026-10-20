import tempfile
import unittest
import asyncio
import concurrent.futures
import inspect
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock
import local_bridge

from local_bridge import permitted, tasks, workbuddy_wait


class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_wait_does_not_block_other_mcp_requests(self):
        future = concurrent.futures.Future()
        tasks['test-task'] = {'future': future, 'session_id': None}
        timer = threading.Timer(0.2, lambda: future.set_result({'state': 'completed'}))
        order = []
        async def wait():
            result = workbuddy_wait('test-task', 1)
            if inspect.isawaitable(result):
                await result
            order.append('done')
        async def heartbeat():
            await asyncio.sleep(0.01)
            order.append('heartbeat')
        timer.start()
        try:
            await asyncio.gather(wait(), heartbeat())
            self.assertEqual(order, ['heartbeat', 'done'])
        finally:
            timer.join()
            tasks.pop('test-task')


class PermissionsTest(unittest.TestCase):
    def test_file_access_stays_within_task_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            self.assertTrue(permitted('Write', {'file_path': str(root / 'result.txt')}, root))
            self.assertTrue(permitted('Edit', {'file_path': 'result.txt'}, root))
            self.assertFalse(permitted('Read', {'file_path': '../private.txt'}, root))
            self.assertFalse(permitted('Write', {'file_path': str(root) + '-other/file'}, root))
            self.assertFalse(permitted('Write', {}, root))
            self.assertFalse(permitted('Write', {'file_path': 'file.txt:stream'}, root))

    def test_only_expected_web_and_file_tools_are_allowed(self):
        root = Path.cwd()
        self.assertTrue(permitted('WebSearch', {'query': 'IANA example domains'}, root))
        self.assertTrue(permitted('WebFetch', {'url': 'https://www.iana.org/help/example-domains'}, root))
        for name in ('Bash', 'PowerShell', 'Delete', 'Browser', 'Task', 'Unknown'):
            self.assertFalse(permitted(name, {}, root))
        self.assertFalse(permitted('WebFetch', {'url': 'file:///C:/private.txt'}, root))


class NativeSessionTest(unittest.TestCase):
    def setUp(self):
        desktop = patch.object(local_bridge, 'desktop_turn_state', return_value='idle')
        desktop.start()
        self.addCleanup(desktop.stop)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        for key, filename in [('NATIVE_LOCK_PATH', 'lock'), ('ACTIVE_PATH', 'active.json')]:
            patcher = patch.object(local_bridge, key, Path(self.folder.name) / filename, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        import json
        (Path(self.folder.name) / 'desktop_session.json').write_text(json.dumps({'model': 'auto'}))
        patcher = patch.object(local_bridge, 'ROOT', Path(self.folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_completed_tool_does_not_finish_native_turn(self):
        parser = getattr(local_bridge, 'parse_native_turn', None)
        self.assertIsNotNone(parser, 'require turn-level status, not arbitrary record.status')
        rows = [{'type': 'message', 'role': 'user', 'content': [{'type': 'text', 'text': '[wb-test] task'}]},
                {'type': 'function_call_result', 'status': 'completed'}]
        self.assertEqual(parser(rows)['state'], 'busy')
        rows.append({'type': 'message', 'role': 'assistant', 'status': 'incomplete',
                     'providerData': {'error': {'message': '429 queued'}}})
        self.assertEqual(parser(rows)['state'], 'idle')
        self.assertEqual(parser(rows)['error'], '429 queued')
        rows.append({'type': 'function_call', 'status': 'in_progress'})
        self.assertEqual(parser(rows)['state'], 'busy')

    def test_file_lock_rejects_second_owner_and_releases(self):
        with local_bridge.native_session_lock():
            with self.assertRaisesRegex(RuntimeError, 'Another MCP process'):
                with local_bridge.native_session_lock():
                    self.fail('second owner acquired native chat')
        with local_bridge.native_session_lock():
            pass

    def test_relative_file_is_resolved_against_native_cwd(self):
        client = local_bridge.ScopedClient(MagicMock(), Path('work/scoped').resolve())
        client.native_cwd = str(Path('work/other-native').resolve())
        client._http.close()
        client._http = MagicMock()
        event = {'id': 9, 'params': {'toolCall': {'title': 'Read',
                 'rawInput': {'file_path': 'private.txt'}},
                 'options': [{'kind': 'allow_once', 'optionId': 'yes'}]}}
        with patch.object(client, '_headers', return_value={}):
            client._grant_permission(event)
        self.assertFalse(client.permissions[-1]['allowed'])
        self.assertEqual(client._http.post.call_args.kwargs['json']['result'],
                         {'outcome': {'outcome': 'cancelled'}})

    def test_loads_bound_native_session_before_one_prompt(self):
        client = MagicMock()
        client.prompt.return_value = {'result': {'stopReason': 'end_turn'}, 'answer': 'ok'}
        client.permissions = []
        task = {'cancel': threading.Event(), 'task_id': 'wb-test'}
        with (patch.object(local_bridge, 'discover_native_server', create=True,
                          return_value=('server', 'native-id', 'D:/native')),
             patch.object(local_bridge, 'discover_desktop_server', create=True,
                          side_effect=AssertionError('must bind native session')),
             patch.object(local_bridge, 'ScopedClient', return_value=client)):
            result = local_bridge.run_task(task, 'one task', Path.cwd(), 30)
        self.assertEqual(result['state'], 'completed', result)
        client.new_session.assert_not_called()
        client.request.assert_called_once_with('session/load', {
            'sessionId': 'native-id', 'cwd': 'D:/native', 'mcpServers': []}, timeout_seconds=30)
        client.configure_session.assert_called_once_with(permission_mode='default', model='auto')
        self.assertEqual(client.session_id, 'native-id')
        client.prompt.assert_called_once()
        self.assertIn('[wb-test]', client.prompt.call_args.args[0])

    def test_uncertain_prompt_failure_is_not_retried(self):
        client = MagicMock()
        client.prompt.side_effect = TimeoutError('unknown submission outcome')
        task = {'cancel': threading.Event(), 'task_id': 'wb-test'}
        with (patch.object(local_bridge, 'discover_native_server', create=True,
                          return_value=('server', 'native-id', 'D:/native')),
             patch.object(local_bridge, 'ScopedClient', return_value=client)):
            result = local_bridge.run_task(task, 'one task', Path.cwd(), 30)
        self.assertEqual(result['state'], 'failed')
        client.prompt.assert_called_once()
        client.notify.assert_called_once_with('session/cancel', {'sessionId': 'native-id'})


if __name__ == '__main__':
    unittest.main()
