"""Restore a bound native chat through WorkBuddy's local sidecar, without UI calls."""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'vendor/desktop-bridge'))
from workbuddy_bridge.acp import _candidate_sidecars, _process_executable, _rpc


def rpc(pipe, method, params=None):
    # A hung named-pipe read must not hang the MCP worker indefinitely. A timeout
    # never retries a mutation; a later task can discover any created runtime.
    request = json.dumps({'pipe': pipe, 'method': method, 'params': params})
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), '--rpc'], input=request,
            encoding='utf-8', capture_output=True, timeout=45,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('WorkBuddy sidecar timed out; runtime creation may be pending, no prompt retried') from exc
    if result.returncode:
        raise RuntimeError('WorkBuddy sidecar request failed: ' + result.stderr.strip()[-500:])
    return json.loads(result.stdout)


def session_cwd(sid, database=None):
    sid = str(uuid.UUID(sid))
    database = Path(database or Path.home() / '.workbuddy/workbuddy.db').resolve()
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
        row = db.execute('SELECT cwd FROM sessions WHERE id=? AND deleted_at IS NULL', (sid,)).fetchone()
    if not row or not row[0] or not Path(row[0]).is_absolute() or not Path(row[0]).is_dir():
        raise RuntimeError('Bound native chat or its original working directory is unavailable')
    return row[0]


def validate_endpoint(endpoint):
    url = urlsplit(endpoint)
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
            or url.username or url.password or url.path != '/api/v1/acp' or url.query or url.fragment):
        raise ValueError('WorkBuddy returned an invalid loopback ACP endpoint')
    return endpoint


def owned_endpoints(sid):
    """Identify legacy bridge workers so they cannot shadow the desktop worker."""
    runtime_id = 'codex-native-' + str(uuid.UUID(sid))
    endpoints = set()
    for _, pipe, sidecar_pid in _candidate_sidecars():
        exe = _process_executable(sidecar_pid)
        if exe.name.casefold() != 'workbuddy.exe':
            continue
        # Fail closed when a live sidecar cannot be inspected. Otherwise its
        # hidden worker might be mistaken for the desktop's native runtime.
        for host in rpc(pipe, 'session.list') or []:
            if host.get('sessionId') != runtime_id:
                continue
            if _process_executable(int(host['pid'])) != exe:
                raise RuntimeError('Recovered runtime executable does not match WorkBuddy')
            endpoints.add(validate_endpoint(host['acpEndpoint']))
    return endpoints


def resolve_runtime(sid, cwd, *, create=False):
    sid = str(uuid.UUID(sid))
    runtime_id = 'codex-native-' + sid
    candidate = None
    for _, pipe, sidecar_pid in _candidate_sidecars():
        try:
            exe = _process_executable(sidecar_pid)
            if exe.name.casefold() != 'workbuddy.exe':
                continue
            cli = exe.parent / 'resources/app.asar.unpacked/cli/bin/codebuddy'
            if not cli.is_file():
                continue
            hosts = rpc(pipe, 'session.list') or []
        except (OSError, RuntimeError):
            continue
        host = next((item for item in hosts if item.get('sessionId') == runtime_id), None)
        if host:
            if _process_executable(int(host['pid'])) != exe:
                raise RuntimeError('Recovered runtime executable does not match WorkBuddy')
            return validate_endpoint(host['acpEndpoint'])
        if candidate is None:
            candidate = (pipe, exe, cli)
    if candidate is not None and create:
        pipe, exe, cli = candidate
        # port=0 is essential: sidecar v6 supplies the app's credential bootstrap
        # through its private child channel. No login secrets are read or copied.
        host = rpc(pipe, 'session.create', {
            'sessionId': runtime_id, 'command': str(exe), 'cwd': cwd, 'port': 0,
            'args': [str(cli), '--serve', '--session-id', sid, '--setting-sources', 'user',
                     '--permission-mode', 'default', '--model', 'auto', '--strict-mcp-config'],
            'env': {'ELECTRON_RUN_AS_NODE': '1', 'CODEBUDDY_FORCE_LITE_WB_BUNDLE': '1',
                    'CODEBUDDY_FORCE_HEADLESS_BUNDLE': '1', 'CODEBUDDY_HOST': 'workbuddy-desktop',
                    'CODEBUDDY_CONFIG_DIR': str(Path.home() / '.workbuddy'),
                    'WORKBUDDY_CONFIG_DIR': str(Path.home() / '.workbuddy'),
                    'CODEBUDDY_DISABLE_AUTO_MEMORY': '1', 'CODEBUDDY_DISABLE_CRON': '1'},
        })
        if host.get('sessionId') != runtime_id:
            raise RuntimeError('Created runtime identity mismatch; no prompt submitted')
        if _process_executable(int(host['pid'])) != exe:
            raise RuntimeError('Created runtime executable does not match WorkBuddy')
        return validate_endpoint(host['acpEndpoint'])
    if create:
        raise RuntimeError('No running WorkBuddy sidecar found; start and sign in to WorkBuddy first')
    return None


if __name__ == '__main__':
    if sys.argv[1:] != ['--rpc']:
        raise SystemExit('This module is used by local_bridge.py')
    request = json.load(sys.stdin)
    try:
        print(json.dumps(_rpc(request['pipe'], request['method'], request['params'])))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
