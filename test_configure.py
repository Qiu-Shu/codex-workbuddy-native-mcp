import tempfile
import tomllib
import unittest
from pathlib import Path
from configure import register_mcp


class RegisterTest(unittest.TestCase):
    def test_preserves_other_settings_and_backs_up_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            original = '# keep this comment\nmodel = "example"\n[mcp_servers.other]\ncommand = "other"\n'
            path.write_text(original, encoding='utf-8')
            backup = register_mcp(path, Path(tmp) / '目录 space', Path(tmp) / 'python.exe')
            parsed = tomllib.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(backup.read_text(encoding='utf-8'), original)
            self.assertEqual(parsed['mcp_servers']['other']['command'], 'other')
            self.assertTrue(parsed['mcp_servers']['workbuddy']['args'][0].endswith('local_bridge.py'))
            self.assertTrue(path.read_text(encoding='utf-8').startswith(original))

    def test_refuses_existing_server_without_changing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            original = '[mcp_servers.workbuddy]\ncommand="existing"\n'
            path.write_text(original)
            with self.assertRaisesRegex(ValueError, 'already exists'):
                register_mcp(path, Path(tmp), Path(tmp) / 'python.exe')
            self.assertEqual(path.read_text(), original)


if __name__ == '__main__':
    unittest.main()
