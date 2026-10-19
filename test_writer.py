import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import httpx
import local_bridge as bridge
from workbuddy_bridge.acp import AcpClient, DesktopServer, WorkBuddyError


class WriterTest(unittest.TestCase):
    def test_writer_conflicts_stop_in_all_transcript_states(self):
        occupied = WorkBuddyError('Persistent Session already has an external writer')
        for state, expected_calls in [('idle', 1), ('unknown', 1), ('busy', 1)]:
            with self.subTest(state=state):
                client = MagicMock()
                client.request.side_effect = occupied
                with patch.object(bridge, 'native_turn_state', return_value={'state': state}):
                    with self.assertRaises(Exception):
                        bridge.load_native_writer(client, 'sid', 'D:/native')
                self.assertEqual(client.request.call_count, expected_calls)
                client.prompt.assert_not_called()
        client = MagicMock()
        client.request.side_effect = WorkBuddyError('authentication required')
        with self.assertRaises(WorkBuddyError):
            bridge.load_native_writer(client, 'sid', 'D:/native')
        self.assertEqual(client.request.call_count, 1)
        client.request.side_effect = occupied
        client.request.reset_mock()
        with patch.object(bridge, 'native_turn_state', side_effect=OSError('unreadable')):
            with self.assertRaises(WorkBuddyError):
                bridge.load_native_writer(client, 'sid', 'D:/native')
        self.assertEqual(client.request.call_count, 1)

    def test_cleanup_failure_preserves_completed_task_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'desktop_session.json').write_text('{"model":"auto"}')
            client = MagicMock()
            client.prompt.return_value = {'result': {'stopReason': 'end_turn'}, 'answer': 'done'}
            client.permissions = []
            client.close.side_effect = OSError('cleanup failed')
            task = {'cancel': threading.Event(), 'task_id': 'wb-test'}
            with (patch.object(bridge, 'ROOT', root),
                  patch.object(bridge, 'NATIVE_LOCK_PATH', root / 'lock'),
                  patch.object(bridge, 'ACTIVE_PATH', root / 'active.json'),
                  patch.object(bridge, 'discover_native_server', return_value=('server', 'sid', 'D:/native')),
                  patch.object(bridge, 'ScopedClient', return_value=client)):
                result = bridge.run_task(task, 'one task', root, 30)
            self.assertEqual(result['state'], 'completed')
            self.assertEqual(result['answer'], 'done')
            self.assertEqual(result['connection_cleanup_error'], 'cleanup failed')
            client.prompt.assert_called_once()

    def test_occupied_idle_session_never_uses_explicit_takeover(self):
        client = MagicMock()
        def load(method, params, **kwargs):
            if not params.get('_meta', {}).get('codebuddy.ai/takeWriter'):
                raise WorkBuddyError('Persistent Session already has an external writer')
            return {}
        client.request.side_effect = load
        with patch.object(bridge, 'native_turn_state', return_value={'state': 'idle'}):
            with self.assertRaises(WorkBuddyError):
                bridge.load_native_writer(client, 'sid', 'D:/native')
        self.assertEqual(client.request.call_count, 1)
        client.prompt.assert_not_called()

    def test_busy_session_is_never_taken_over(self):
        client = MagicMock()
        client.request.side_effect = WorkBuddyError('Persistent Session already has an external writer')
        with patch.object(bridge, 'native_turn_state', return_value={'state': 'busy'}):
            with self.assertRaises(WorkBuddyError):
                bridge.load_native_writer(client, 'sid', 'D:/native')
        self.assertEqual(client.request.call_count, 1)

    def test_other_load_failure_is_not_retried(self):
        client = MagicMock()
        client.request.side_effect = TimeoutError('unknown load outcome')
        with self.assertRaises(TimeoutError):
            bridge.load_native_writer(client, 'sid', 'D:/native')
        self.assertEqual(client.request.call_count, 1)

    def test_status_discovery_never_allocates_acp_connection(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen()
            root = Path(directory)
            (root / 'desktop_session.json').write_text(json.dumps({'session_id': '11111111-1111-4111-8111-111111111111'}))
            def find(*, probe=True):
                self.assertFalse(probe, 'status must not allocate an ACP connection')
                return [{'port': listener.getsockname()[1], 'sessionId': '11111111-1111-4111-8111-111111111111', 'cwd': 'D:/native'}]
            with (patch.object(bridge, 'ROOT', root),
                  patch.object(bridge, 'ACTIVE_PATH', root / 'active.json'),
                  patch.object(bridge.live, 'list_interactive_sessions', side_effect=find),
                  patch.object(bridge.native_runtime, 'session_cwd', return_value='D:/native'),
                  patch.object(bridge.native_runtime, 'owned_endpoints', return_value=set()),
                  patch.object(bridge, 'native_turn_state', return_value={'state': 'idle'})):
                bridge.discover_native_server()

    def test_cleanup_http_failure_is_reported_and_transport_closed(self):
        client = AcpClient(DesktopServer('http://localhost/api/v1/acp', '', '', '', 0))
        client._http.close()
        client._http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        client.connection_id = 'test-connection'
        with self.assertRaises(httpx.HTTPStatusError):
            client.close()
        self.assertTrue(client._http.is_closed)


if __name__ == '__main__':
    unittest.main()
