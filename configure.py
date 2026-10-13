"""Bind one existing local WorkBuddy chat and optionally register the stdio MCP."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
import tomllib
import uuid

ROOT = Path(__file__).resolve().parent


def register_mcp(config_path, root, executable):
    config_path = Path(config_path)
    original = config_path.read_text(encoding='utf-8') if config_path.exists() else ''
    config = tomllib.loads(original)
    if 'workbuddy' in config.get('mcp_servers', {}):
        raise ValueError('mcp_servers.workbuddy already exists; review it manually before replacing it')
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    addition = ('\n[mcp_servers.workbuddy]\ncommand = ' + quote(Path(executable).resolve())
                + '\nargs = [' + quote(Path(root).resolve() / 'local_bridge.py') + ']\n'
                + 'env = { PYTHONIOENCODING = "utf-8" }\n')
    tomllib.loads(original + addition)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if config_path.exists():
        suffix = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
        backup = config_path.with_name(config_path.name + '.before-workbuddy-' + suffix + '.bak')
        backup.write_bytes(config_path.read_bytes())
    temporary = config_path.with_name(config_path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(original + addition, encoding='utf-8')
    temporary.replace(config_path)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--list', action='store_true', help='List 30 recent native chat IDs/titles; no message bodies')
    mode.add_argument('--session-id', help='UUID of an existing chat on THIS computer')
    parser.add_argument('--model', default='auto')
    parser.add_argument('--register', action='store_true', help='Append global Codex MCP config, backing up any existing config')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('This release supports Windows only')
    if args.list and args.register:
        parser.error('--register requires --session-id')
    database = Path.home() / '.workbuddy/workbuddy.db'
    if not database.is_file():
        parser.error('WorkBuddy database missing. Install and sign in to WorkBuddy first.')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        if args.list:
            rows = db.execute('SELECT id,COALESCE(custom_title,title),cwd FROM sessions '
                              'WHERE deleted_at IS NULL ORDER BY updated_at DESC LIMIT 30').fetchall()
            for sid, title, cwd in rows:
                print(json.dumps({'session_id': sid, 'title': title, 'cwd': cwd}, ensure_ascii=False))
            return
        sid = str(uuid.UUID(args.session_id))
        row = db.execute('SELECT id FROM sessions WHERE id=? AND deleted_at IS NULL', (sid,)).fetchone()
        if not row:
            parser.error('That session does not exist on this computer; use --list')
    if args.register:
        codex_dir = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        backup = register_mcp(codex_dir / 'config.toml', ROOT, sys.executable)
        print('Registered global MCP workbuddy.' + (f' Backup: {backup}' if backup else ''))
    binding = ROOT / 'desktop_session.json'
    if binding.exists():
        binding.with_name('desktop_session.backup.json').write_bytes(binding.read_bytes())
    binding.write_text(json.dumps({'session_id': sid, 'model': args.model}, indent=2), encoding='utf-8')
    print('Bound existing chat: ' + sid)
    print('Restart/reload Codex MCP. Keep WorkBuddy signed in and the selected chat active.')


if __name__ == '__main__':
    main()
