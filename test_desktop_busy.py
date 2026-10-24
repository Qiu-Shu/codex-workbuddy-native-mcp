import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import local_bridge as bridge


def event(name, **data):
    return '2026-10-20T11:20:51.440Z ' + name + ' ' + json.dumps(data)


class DesktopBusyTest(unittest.TestCase):
    def test_pending_desktop_prompt_is_busy_before_transcript_changes(self):
        lines = [
            event('method:requests:result', states={'completed': 22}),
            event('method:sendPrompt', blockCount=1),
            event('method:requests:result', states={'completed': 22}),
            event('state-machine:transition', input='PROMPT_SENT', to='working'),
            event('state-machine:transition', input='PHASE_IDLE', to='working'),
        ]
        self.assertEqual(bridge.parse_desktop_turn(lines), 'busy')
        lines.append(event('state-machine:transition', input='TURN_COMPLETED', to='idle'))
        self.assertEqual(bridge.parse_desktop_turn(lines), 'idle')

    def test_unknown_or_streaming_window_is_not_idle(self):
        self.assertEqual(bridge.parse_desktop_turn(['invalid']), 'unknown')
        self.assertEqual(bridge.parse_desktop_turn([
            event('method:requests:result', states={'completed': 22, 'streaming': 1})
        ]), 'busy')

    def test_busy_window_stops_before_loading_or_sending(self):
        client = MagicMock()
        client.prompt.return_value = {'result': {'stopReason': 'end_turn'}, 'answer': 'unexpected'}
        client.permissions = []
        task = {'cancel': threading.Event(), 'task_id': 'wb-test'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch.object(bridge, 'NATIVE_LOCK_PATH', root / 'lock'),
                  patch.object(bridge, 'ACTIVE_PATH', root / 'active.json'),
                  patch.object(bridge, 'discover_native_server', return_value=('server', 'sid', 'D:/native')),
                  patch.object(bridge, 'ScopedClient', return_value=client),
                  patch.object(bridge, 'desktop_turn_state', return_value='busy', create=True)):
                result = bridge.run_task(task, 'text only', root, 30)
            self.assertEqual(result['state'], 'failed')
            self.assertIn('desktop', result['error'].lower())
            client.request.assert_not_called()
            client.prompt.assert_not_called()
            self.assertFalse((root / 'active.json').exists())


if __name__ == '__main__':
    unittest.main()
