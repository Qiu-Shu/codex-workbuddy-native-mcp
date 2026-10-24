import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import local_bridge as bridge

SID = '12345678-1234-5678-1234-567812345678'
NATIVE_CWD = 'D:/native'


class BusyAfterLoadGuardTest(unittest.TestCase):
    def test_desktop_busy_between_load_and_prompt_stops_submission(self):
        client = MagicMock()
        client.prompt.return_value = {'result': {'stopReason': 'end_turn'}, 'answer': 'unexpected'}
        client.permissions = []
        task = {'cancel': threading.Event(), 'task_id': 'wb-guard'}
        terminal_content = json.dumps({'session_id': SID, 'task_id': 'wb-earlier',
                                       'stop_reason': 'end_turn',
                                       'fingerprint': {'size': 1, 'mtime_ns': 2}})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active_path = root / 'native-active.json'
            terminal_path = root / 'native-terminal.json'
            terminal_path.write_text(terminal_content, encoding='utf-8')
            # Desktop binding with an empty model selection.
            (root / 'desktop_session.json').write_text(
                json.dumps({'session_id': SID, 'model': ''}), encoding='utf-8')
            with (patch.object(bridge, 'ROOT', root),
                  patch.object(bridge, 'NATIVE_LOCK_PATH', root / 'native-session.lock'),
                  patch.object(bridge, 'ACTIVE_PATH', active_path),
                  patch.object(bridge, 'TERMINAL_PATH', terminal_path),
                  patch.object(bridge, 'discover_native_server',
                               return_value=(MagicMock(name='server'), SID, NATIVE_CWD)),
                  patch.object(bridge, 'ScopedClient', return_value=client),
                  patch.object(bridge, 'desktop_turn_state',
                               side_effect=['idle', 'busy'], create=True) as turn_state):
                result = bridge.run_task(task, 'text only', root, 30)

            self.assertEqual(result['state'], 'failed')
            self.assertIn('busy', result['error'])
            self.assertFalse(result['submission_may_have_started'])
            self.assertTrue(result['retry_safe'])

            # Idle check ran in load_native_writer, then again before the prompt.
            self.assertEqual(turn_state.call_count, 2)

            # session/load happened exactly once, with no takeWriter takeover.
            client.request.assert_called_once()
            method, params = client.request.call_args.args[:2]
            self.assertEqual(method, 'session/load')
            self.assertEqual(params, {'sessionId': SID, 'cwd': NATIVE_CWD, 'mcpServers': []})
            self.assertNotIn('takeWriter', json.dumps(params))

            # Bound desktop model is empty.
            client.configure_session.assert_called_once_with(permission_mode='default', model='')

            # Nothing was submitted, no cancel was sent to the original desktop.
            client.prompt.assert_not_called()
            client.notify.assert_not_called()
            client.close.assert_called_once()

            # No active marker; the pre-existing terminal receipt is untouched.
            self.assertFalse(active_path.exists())
            self.assertEqual(terminal_path.read_text(encoding='utf-8'), terminal_content)


if __name__ == '__main__':
    unittest.main()
