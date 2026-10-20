import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import local_bridge as bridge
from workbuddy_bridge.acp import WorkBuddyError

SID = '11111111-1111-4111-8111-111111111111'


class VisibilityTest(unittest.TestCase):
    def setUp(self):
        desktop = patch.object(bridge, 'desktop_turn_state', return_value='idle')
        desktop.start()
        self.addCleanup(desktop.stop)

    def discover(self, records, owned):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'desktop_session.json').write_text(json.dumps({'session_id': SID}))
            with (patch.object(bridge, 'ROOT', root),
                  patch.object(bridge, 'ACTIVE_PATH', root / 'active.json'),
                  patch.object(bridge.live, 'list_interactive_sessions', return_value=records),
                  patch.object(bridge.native_runtime, 'owned_endpoints', return_value=owned, create=True),
                  patch.object(bridge.native_runtime, 'session_cwd', return_value='D:/native'),
                  patch.object(bridge.native_runtime, 'resolve_runtime') as recover,
                  patch.object(bridge, 'native_turn_state', return_value={'state': 'idle'}),
                  patch.object(bridge.socket, 'create_connection')):
                try:
                    return bridge.discover_native_server(activate=True)[0].acp_endpoint
                finally:
                    recover.assert_not_called()

    def test_newer_owned_worker_cannot_hide_desktop_runtime(self):
        records = [dict(sessionId=SID, pid=1, port=61001, cwd='D:/native', updatedAt=1),
                   dict(sessionId=SID, pid=2, port=61002, cwd='D:/native', updatedAt=2)]
        self.assertEqual(self.discover(records, {'http://127.0.0.1:61002/api/v1/acp'}),
                         'http://127.0.0.1:61001/api/v1/acp')

    def test_owned_only_or_dormant_never_starts_hidden_worker(self):
        for records in [[], [dict(sessionId=SID, pid=2, port=61002, cwd='D:/native')]]:
            with self.subTest(records=records), self.assertRaises(bridge.NativeRuntimeDormant):
                self.discover(records, {'http://127.0.0.1:61002/api/v1/acp'})

    def test_multiple_desktop_candidates_are_not_guessed(self):
        records = [dict(sessionId=SID, pid=i, port=61000+i, cwd='D:/native') for i in [1, 2]]
        with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
            self.discover(records, set())

    def test_writer_conflict_does_not_close_desktop_subscription(self):
        client = MagicMock()
        client.request.side_effect = WorkBuddyError('Persistent Session already has an external writer')
        with patch.object(bridge, 'native_turn_state', return_value={'state': 'idle'}):
            with self.assertRaises(WorkBuddyError):
                bridge.load_native_writer(client, SID, 'D:/native')
        self.assertEqual(client.request.call_count, 1)

    def test_dormant_status_requires_input_focus_without_automatic_retry(self):
        with patch.object(bridge, 'discover_native_server',
                          side_effect=bridge.NativeRuntimeDormant('input focus required')):
            status = asyncio.run(bridge.workbuddy_status())
        self.assertFalse(status['connected'])
        self.assertFalse(status['can_attempt_recovery'])
        self.assertTrue(status.get('requires_input_focus'))
        self.assertEqual(status.get('recovery_action'), 'click_bound_chat_input_without_sending')


if __name__ == '__main__':
    unittest.main()
