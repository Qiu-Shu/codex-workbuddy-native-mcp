import tempfile
import tomllib
import unittest
import contextlib
import io
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch
import configure
from configure import register_mcp


class RegisterTest(unittest.TestCase):
    def test_background_registration_needs_no_desktop_binding_or_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (patch.object(configure, 'ROOT', root),
                  patch.dict(configure.os.environ, {'CODEX_HOME': str(root/'codex')}),
                  patch.object(sys, 'argv', ['configure.py', '--background', '--register']),
                  contextlib.redirect_stdout(io.StringIO())):
                configure.main()
            self.assertTrue((root/'codex/config.toml').is_file())
            self.assertFalse((root/'desktop_session.json').exists())

    def test_default_binding_preserves_workbuddy_selected_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.workbuddy').mkdir()
            sid = '00000000-0000-4000-8000-000000000001'
            with sqlite3.connect(root / '.workbuddy/workbuddy.db') as db:
                db.execute('CREATE TABLE sessions (id TEXT, deleted_at TEXT)')
                db.execute('INSERT INTO sessions VALUES (?, NULL)', (sid,))
            db.close()
            with (patch.object(configure, 'ROOT', root),
                  patch.object(Path, 'home', return_value=root),
                  patch.object(sys, 'argv', ['configure.py', '--session-id', sid]),
                  contextlib.redirect_stdout(io.StringIO())):
                configure.main()
            binding = json.loads((root / 'desktop_session.json').read_text())
            self.assertEqual(binding['model'], '')

    def test_preserves_other_settings_and_backs_up_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            original = '# keep this comment\nmodel = "example"\n[mcp_servers.other]\ncommand = "other"\n'
            path.write_text(original, encoding='utf-8')
            backup = register_mcp(path, Path(tmp) / '目录 space', Path(tmp) / 'python.exe')
            parsed = tomllib.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(backup.read_text(encoding='utf-8'), original)
            self.assertEqual(parsed['mcp_servers']['other']['command'], 'other')
            self.assertTrue(parsed['mcp_servers']['workbuddy']['args'][0].endswith('catalog_mcp.py'))
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
