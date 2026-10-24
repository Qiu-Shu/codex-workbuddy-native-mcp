"""Disposable, bridge-owned WorkBuddy workers with native credential bootstrap."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid
from urllib.parse import urlsplit

import local_bridge as bridge
from workbuddy_bridge.acp import DesktopServer, _candidate_sidecars, _process_executable, _rpc

PREFIX = 'codex-background-'


class SidecarError(RuntimeError):
    pass


class SidecarUnavailable(SidecarError):
    pass


def process_exists(pid):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if handle:
        kernel.CloseHandle(handle)
        return True
    # ERROR_INVALID_PARAMETER is the documented result for a nonexistent PID.
    # Access denied is not evidence that a process exited.
    return ctypes.get_last_error() != 87


def rpc(pipe, method, params=None, timeout=75):
    """A stuck named-pipe read cannot leave a thread or block MCP shutdown."""
    process = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), '--rpc'],
        input=json.dumps({'pipe': pipe, 'method': method, 'params': params}),
        capture_output=True, text=True, encoding='utf-8', timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        payload = json.loads(process.stdout)
    except (ValueError, TypeError):
        raise SidecarError('Sidecar transport failed; no prompt was retried') from None
    if not payload.get('ok'):
        if payload.get('pipe_missing'):
            raise SidecarUnavailable('Previous WorkBuddy sidecar pipe is absent')
        if payload.get('not_found'):
            raise FileNotFoundError('Owned worker is absent')
        raise SidecarError('Sidecar request failed (' + payload.get('error_type', 'unknown') + ')')
    return payload.get('result')


def find_sidecar():
    for _, pipe, pid in _candidate_sidecars():
        try:
            exe = _process_executable(pid)
            cli = exe.parent / 'resources/app.asar.unpacked/cli/bin/codebuddy'
            if exe.name.casefold() != 'workbuddy.exe' or not cli.is_file():
                continue
            rpc(pipe, 'session.list', timeout=5)
            return pipe, pid, exe
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            continue
    raise RuntimeError('WorkBuddy must be running and logged in; no live native sidecar found')


def validate_endpoint(endpoint):
    url = urlsplit(endpoint)
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
            or url.username or url.password or url.path != '/api/v1/acp'
            or url.query or url.fragment):
        raise ValueError('Unexpected native ACP endpoint')
    return endpoint


def stop_owned(metadata):
    owner = metadata.get('owner', '')
    if not re.fullmatch(PREFIX + r'[a-f0-9]{32}', owner):
        raise ValueError('Refusing to stop a worker not owned by this bridge')
    params = {'sessionId': owner}
    if metadata.get('runtime_id'):
        params['expectedRuntimeId'] = metadata['runtime_id']
    try:
        rpc(metadata['sidecar_pipe'], 'session.kill', params, timeout=15)
    except SidecarUnavailable:
        if not metadata.get('pid') or process_exists(metadata['pid']):
            raise
    except FileNotFoundError:
        pass


@contextmanager
def owned_worker(cwd, on_created=None, model_id=''):
    validate_model_id(model_id)
    pipe, pid, executable = find_sidecar()
    cli = executable.parent / 'resources/app.asar.unpacked/cli/bin/codebuddy'
    owner = PREFIX + uuid.uuid4().hex
    metadata = {'owner': owner, 'sidecar_pipe': pipe, 'sidecar_pid': pid}
    if on_created:
        on_created(metadata)
    config = str(Path.home() / '.workbuddy')
    params = {
        'sessionId': owner, 'command': str(executable),
        'args': [str(cli), '--serve', '--setting-sources', 'user', '--permission-mode', 'default',
                 '--permission-mode-before-plan', 'default', '--strict-mcp-config'],
        'cwd': str(cwd), 'port': 0, 'cols': 80, 'rows': 24,
        'env': {'ELECTRON_RUN_AS_NODE': '1', 'CODEBUDDY_FORCE_LITE_WB_BUNDLE': '1',
                'CODEBUDDY_FORCE_HEADLESS_BUNDLE': '1', 'CODEBUDDY_DISABLE_IDE': '1',
                'CODEBUDDY_DISABLE_AUTO_MEMORY': '1', 'CODEBUDDY_DISABLE_SYSTEM_REMINDER_MD': '1',
                'CODEBUDDY_DISABLE_CRON': '1', 'CODEBUDDY_DISABLE_AUTO_UPDATE': '1',
                'CODEBUDDY_CONFIG_DIR': config, 'WORKBUDDY_CONFIG_DIR': config}}
    if model_id:
        params['args'].extend(['--model', model_id])
    try:
        created = rpc(pipe, 'session.create', params)
        if created.get('sessionId') != owner or not created.get('runtimeId') or int(created.get('pid', 0)) <= 0:
            raise RuntimeError('Native worker readiness identity mismatch')
        metadata.update(runtime_id=created['runtimeId'], pid=created['pid'])
        if on_created:
            on_created(metadata)
        endpoint = validate_endpoint(created['acpEndpoint'])
        yield DesktopServer(endpoint, '', owner, pipe, pid, created['pid']), metadata
    finally:
        stop_owned(metadata)


def validate_model_id(model_id):
    if not isinstance(model_id, str) or (model_id and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}', model_id)):
        raise ValueError('Use a model ID from workbuddy_models, up to 200 characters')


if __name__ == '__main__':
    try:
        request = json.load(sys.stdin)
        if request['method'] not in ('session.list', 'session.create', 'session.kill'):
            raise ValueError('Unsupported sidecar operation')
        value = _rpc(request['pipe'], request['method'], request.get('params'))
        print(json.dumps({'ok': True, 'result': value}))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__,
                          'not_found': 'Session not found' in str(exc),
                          'pipe_missing': isinstance(exc.__cause__, FileNotFoundError)}))
