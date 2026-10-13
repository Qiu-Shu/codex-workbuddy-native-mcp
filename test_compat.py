import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'vendor' / 'desktop-bridge'))
import httpx
from workbuddy_bridge.acp import AcpClient, DesktopServer, _candidate_sidecars


class CompatibilityTest(unittest.TestCase):
    def test_cleanup_request_has_a_finite_timeout(self):
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(200)
        client = AcpClient(DesktopServer('http://localhost/api/v1/acp', '', '', '', 0))
        client._http.close()
        client._http = httpx.Client(transport=httpx.MockTransport(handle), timeout=None)
        client.connection_id = 'test-connection'
        client.close()
        self.assertEqual(len(requests), 1)
        self.assertTrue(all(isinstance(t, (int, float)) and 0 < t <= 10
                            for t in requests[0].extensions['timeout'].values()))

    def test_discovers_version6_control_pipe(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'wb' / 'runtime'
            folder.mkdir(parents=True)
            (folder / 'sidecar.pid').write_text(json.dumps({
                'pid': 123, 'version': 6, 'controlPipeUuid': 'abcd1234'}))
            with patch.dict(os.environ, {'TEMP': directory}):
                candidates = _candidate_sidecars()
            self.assertEqual(candidates[0][1],
                             r'\\.\pipe\workbuddy-runtime-sidecar-control-abcd1234')


if __name__ == '__main__':
    unittest.main()
