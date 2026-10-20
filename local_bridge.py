"""Local MCP adapter for the running WorkBuddy desktop. Serial tasks, scoped approvals."""
import asyncio
import concurrent.futures
import contextlib
import hashlib
import json
import msvcrt
import os
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
BRIDGE_REVISION = '2026-10-20-prompt-stop-v4'
SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
sys.path.insert(0, str(ROOT / 'vendor' / 'desktop-bridge'))
sys.path.insert(0, str(ROOT / 'vendor' / 'live-bridge'))
import acp_live_send as live
from mcp.server.fastmcp import FastMCP
from workbuddy_bridge.acp import AcpClient, DesktopServer
from workbuddy_bridge.activity_log import _tool_name

mcp = FastMCP('workbuddy', instructions=(
    'Delegate only user-authorized tasks to WorkBuddy. Specify an absolute cwd and explicit scope. '
    'Use start then wait. Web search/fetch and file Read/Write/Edit approvals within cwd are supported. '
    'Other permission requests are denied and reported. This is not an OS sandbox. '
    'Verify outputs independently. WorkBuddy must be running and logged in. '
    'Tasks use one dedicated native chat with persistent shared history. Do not use that chat manually '
    'while a delegated task runs. Never retry a failed or timed-out task automatically. '
    'Use absolute file paths. No desktop typing is needed.'), log_level='WARNING')
# ponytail: one dedicated native chat, serialized across MCP processes with an OS file lock.
pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
tasks = {}
lock = threading.Lock()
NATIVE_LOCK_PATH = ROOT / 'work/native-session.lock'
ACTIVE_PATH = ROOT / 'work/native-active.json'
TERMINAL_PATH = ROOT / 'work/native-terminal.json'


class BridgeUpdateRequired(RuntimeError):
    pass


def ensure_current_revision():
    # A loaded Python process keeps old code after an on-disk deployment.
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != SOURCE_SHA256:
        raise BridgeUpdateRequired('Bridge files changed; reload WorkBuddy MCP or restart Codex before submitting')


def parse_native_turn(rows):
    state = {'state': 'unknown', 'user_text': '', 'error': None}
    for row in rows:
        if row.get('type') == 'function_call' and state['state'] != 'unknown':
            state.update(state='busy', error=None)
        if row.get('type') != 'message':
            continue
        if row.get('role') == 'user':
            content = row.get('content') or []
            text = '\n'.join(c.get('text', '') for c in content if isinstance(c, dict)
                             and c.get('type') in ('text', 'input_text'))
            state = {'state': 'busy', 'user_text': text, 'error': None}
        elif row.get('role') == 'assistant' and state['state'] != 'unknown':
            error = (row.get('providerData') or {}).get('error') or {}
            if row.get('status') in ('completed', 'cancelled', 'interrupted', 'failed', 'error') or error:
                state.update(state='idle', error=error.get('message'))
    return state


def _native_turn_snapshot(native_cwd, sid):
    path = Path(live._transcript_path(native_cwd, sid))
    with path.open('rb') as handle:
        before = os.fstat(handle.fileno())
        offset = max(0, before.st_size - 1024 * 1024)
        handle.seek(offset)
        if offset:
            handle.readline()
        tail = handle.read()
        after = os.fstat(handle.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError('Native history changed during status check; check again before submitting')
    lines = tail.splitlines(keepends=True)
    rows = []
    for line in lines:
        if not line.endswith(b'\n'):
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    fingerprint = {'path': str(path.resolve()), 'size': after.st_size,
                   'mtime_ns': after.st_mtime_ns, 'tail_sha256': hashlib.sha256(tail).hexdigest()}
    return parse_native_turn(rows), fingerprint


def _write_state(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def record_terminal_turn(native_cwd, sid, task_id, stop_reason):
    # Only an actual prompt response is terminal; a cancel notification is not.
    if stop_reason not in ('end_turn', 'cancelled'):
        return False
    try:
        state, fingerprint = _native_turn_snapshot(native_cwd, sid)
    except OSError:
        return False
    if not state['user_text'].startswith('[' + task_id + ']'):
        return False
    _write_state(TERMINAL_PATH, {'session_id': sid, 'task_id': task_id,
                              'stop_reason': stop_reason, 'fingerprint': fingerprint})
    return True


def native_turn_state(native_cwd, sid):
    state, fingerprint = _native_turn_snapshot(native_cwd, sid)
    if state['state'] == 'busy':
        try:
            receipt = json.loads(TERMINAL_PATH.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            receipt = {}
        recovery = receipt.get('recovery') if isinstance(receipt, dict) else None
        stopped_worker = (isinstance(recovery, dict) and recovery.get('kind') == 'verified_worker_exit'
                          and recovery.get('exited') is True and isinstance(recovery.get('pid'), int)
                          and recovery['pid'] > 0 and receipt.get('stop_reason') == 'interrupted')
        if (isinstance(receipt, dict) and receipt.get('session_id') == sid
                and (receipt.get('stop_reason') in ('end_turn', 'cancelled') or stopped_worker)
                and isinstance(receipt.get('task_id'), str)
                and state['user_text'].startswith('[' + receipt['task_id'] + ']')
                and receipt.get('fingerprint') == fingerprint):
            state.update(state='idle')
    return state


def discover_native_server(activate=False):
    ensure_current_revision()
    binding = ROOT / 'desktop_session.json'
    if not binding.is_file():
        raise RuntimeError('Not configured: run setup.ps1 and bind an existing native chat on this computer')
    sid = str(uuid.UUID(json.loads(binding.read_text(encoding='utf-8'))['session_id']))
    found = live.find_live_endpoint(session_id=sid)
    if not found:
        raise RuntimeError('Dedicated native chat has no live endpoint. Keep WorkBuddy running; '
                           'after choosing the model, leave and reopen this chat, then click its input box '
                           '(no message needed). Opening the chat alone may not start its worker: ' + sid)
    port, actual_sid, native_cwd = found
    if actual_sid != sid or not native_cwd:
        raise RuntimeError('Native endpoint identity mismatch')
    state = native_turn_state(native_cwd, sid)
    if state['state'] != 'idle':
        raise RuntimeError('Dedicated native chat is busy or its state is unknown: ' + state['state'])
    if ACTIVE_PATH.exists():
        active = json.loads(ACTIVE_PATH.read_text(encoding='utf-8'))
        if active['session_id'] != sid or not state['user_text'].startswith('[' + active['task_id'] + ']'):
            raise RuntimeError('Previous submission outcome is uncertain; inspect native chat before retrying')
    return DesktopServer(f'http://127.0.0.1:{port}/api/v1/acp', '', sid, '', 0), sid, native_cwd


@contextlib.contextmanager
def native_session_lock():
    folder = NATIVE_LOCK_PATH.parent
    folder.mkdir(exist_ok=True)
    with NATIVE_LOCK_PATH.open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError('Another MCP process is using the dedicated native chat') from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def permitted(name, raw, root):
    name = name.casefold()
    if name == 'websearch':
        return bool(raw.get('query'))
    if name == 'webfetch':
        url = urlsplit(str(raw.get('url', '')))
        return url.scheme in ('http', 'https') and bool(url.hostname)
    if name not in ('read', 'write', 'edit'):
        return False
    value = raw.get('file_path')
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    if any(':' in part for part in path.parts[1 if path.drive else 0:]):
        return False
    try:
        resolved = (path if path.is_absolute() else root / path).resolve()
        return resolved.is_relative_to(root) and resolved != root
    except (OSError, ValueError):
        return False


class ScopedClient(AcpClient):
    def __init__(self, server, root, **kwargs):
        callback = kwargs.pop('event_callback', None)
        super().__init__(server, **kwargs)
        self.root = root
        self.native_cwd = None
        self.permissions = []
        self.permission_cancel_sent = False
        self.permission_cancel_pending = False
        def observe(event):
            params = event.get('params') or {}
            phase = ((params.get('update') or {}).get('_meta') or {}).get('codebuddy.ai/agentPhase') or {}
            if (self.permission_cancel_pending and params.get('sessionId') == self.session_id
                    and phase.get('phase') == 'idle' and not self.permission_cancel_sent):
                self.permission_cancel_sent = True
                # Finish native rejection first; an earlier cancel races its late abort callback.
                self.notify('session/cancel', {'sessionId': self.session_id})
            if callback:
                callback(event)
        self.event_callback = observe

    def _grant_permission(self, event):
        params = event.get('params') or {}
        call = params.get('toolCall') or {}
        name = _tool_name(call)
        checked = dict(call.get('rawInput') or {})
        value = checked.get('file_path')
        if self.native_cwd and isinstance(value, str) and value and not Path(value).is_absolute():
            checked['file_path'] = str(Path(self.native_cwd) / value)
        allowed = permitted(name, checked, self.root)
        option = next((o.get('optionId') for o in params.get('options', [])
                       if o.get('kind') == 'allow_once'), None) if allowed else None
        raw = call.get('rawInput') or {}
        self.permissions.append({'tool': name, 'allowed': bool(option),
                                 'target': raw.get('file_path') or raw.get('url') or raw.get('query')})
        outcome = {'outcome': 'selected', 'optionId': option} if option else {'outcome': 'cancelled'}
        self._http.post(self.server.acp_endpoint, headers=self._headers(),
                        json={'jsonrpc': '2.0', 'id': event['id'], 'result': {'outcome': outcome}},
                        timeout=10).raise_for_status()
        if not option and self.session_id:
            # The following idle event closes a prompt left open by parallel rejection.
            self.permission_cancel_pending = True


def run_task(task, prompt, cwd, timeout_seconds):
    task.setdefault('control_lock', threading.Lock())
    try:
        with native_session_lock():
            return _run_native_task(task, prompt, cwd, timeout_seconds)
    except Exception as exc:
        return {'state': 'failed', 'error': str(exc)}


def _run_native_task(task, prompt, cwd, timeout_seconds):
    client = None
    prompt_started = False
    try:
        if task['cancel'].is_set():
            return {'state': 'cancelled'}
        server, sid, native_cwd = discover_native_server()
        client = ScopedClient(server, cwd, timeout_seconds=timeout_seconds)
        client.native_cwd = native_cwd
        task['client'] = client
        client.connect()
        config = json.loads((ROOT / 'desktop_session.json').read_text(encoding='utf-8'))
        # Explicitly opt into WorkBuddy's native writer handoff for a dedicated idle chat.
        load_params = {'sessionId': sid, 'cwd': native_cwd, 'mcpServers': []}
        if config.get('take_writer') is True:
            load_params['_meta'] = {'codebuddy.ai/takeWriter': True}
        client.request('session/load', load_params, timeout_seconds=30)
        client.session_id = sid
        client.configure_session(permission_mode='default', model=config.get('model', ''))
        task['session_id'] = client.session_id
        if task['cancel'].is_set():
            return {'state': 'cancelled'}
        events = []
        cancelled_on_event = False
        def collect(event):
            nonlocal cancelled_on_event
            # Covers cancellation arriving after the pre-submit check but before HTTP dispatch.
            if task['cancel'].is_set() and not cancelled_on_event:
                cancel_active_task(task)
                cancelled_on_event = True
            update = (event.get('params') or {}).get('update') or {}
            if (update.get('sessionUpdate') in ('tool_call', 'tool_call_update')
                    and update.get('status') in ('completed', 'failed') and len(events) < 500):
                events.append({'tool': _tool_name(update), 'status': update.get('status')})
        with task['control_lock']:
            if task['cancel'].is_set():
                return {'state': 'cancelled'}
            TERMINAL_PATH.unlink(missing_ok=True)
            _write_state(ACTIVE_PATH, {'task_id': task['task_id'], 'session_id': sid})
            prompt_started = True
            task['prompt_started'] = True
        response = client.prompt(
            f'[{task["task_id"]}] This is a NEW authorized task. Do not repeat earlier tasks. '
            f'Authorized task directory: {cwd}. Only use files inside it, with absolute file paths. '
            'Use Read/Write/Edit for file operations and WebSearch/WebFetch for online research. '
            'Do not execute shell commands, use other accounts/connectors, save persistent memory, '
            'or delegate to other agents. If a required operation is denied, report it and stop.\n\n' + prompt,
            event_callback=collect)
        stop = response['result'].get('stopReason')
        if record_terminal_turn(native_cwd, sid, task['task_id'], stop):
            ACTIVE_PATH.unlink(missing_ok=True)
        error = None
        if stop not in ('end_turn', 'cancelled'):
            try:
                error = native_turn_state(native_cwd, sid)['error']
            except OSError:
                pass
        return {'state': 'completed' if stop == 'end_turn' else 'cancelled' if stop == 'cancelled' else 'failed',
                'session_id': client.session_id, 'answer': response['answer'],
                'stop_reason': stop, 'error': error, 'permissions': client.permissions, 'tool_events': events}
    except Exception as exc:
        if prompt_started and client and client.session_id:
            try:
                client.notify('session/cancel', {'sessionId': client.session_id})
            except Exception:
                pass
        return {'state': 'failed', 'error': str(exc), 'submission_may_have_started': prompt_started,
                'retry_safe': not prompt_started}
    finally:
        with task['control_lock']:
            task['client'] = None
            if client:
                client.close()


@mcp.tool()
async def workbuddy_status() -> dict:
    """Check connection to the running local WorkBuddy desktop, without exposing credentials."""
    runtime = {'bridge_revision': BRIDGE_REVISION, 'process_id': os.getpid(), 'source_sha256': SOURCE_SHA256}
    try:
        ensure_current_revision()
        def probe():
            with native_session_lock():
                return discover_native_server()
        server, sid, native_cwd = await asyncio.to_thread(probe)
        return {**runtime, 'connected': True, 'endpoint': server.acp_endpoint, 'concurrency': 1,
                'session_id': sid, 'native_history': True, 'native_cwd': native_cwd,
                'permissions': 'default mode; scoped allow_once responses; not an OS sandbox'}
    except Exception as exc:
        return {**runtime, 'connected': False, 'error': str(exc),
                'error_code': 'bridge_update_required' if isinstance(exc, BridgeUpdateRequired) else 'native_unavailable'}


@mcp.tool()
def workbuddy_start(prompt: str, cwd: str, timeout_seconds: int = 240) -> dict:
    """Start an authorized task: text, web research, or Read/Write/Edit within explicit absolute cwd."""
    ensure_current_revision()
    path = Path(cwd)
    if not path.is_absolute() or not path.is_dir() or not prompt.strip():
        raise ValueError('Provide a nonempty prompt and an existing absolute cwd')
    if not 10 <= timeout_seconds <= 600:
        raise ValueError('timeout_seconds must be between 10 and 600')
    task = {'cancel': threading.Event(), 'client': None, 'session_id': None,
            'control_lock': threading.Lock()}
    task_id = 'wb-' + uuid.uuid4().hex[:12]
    task['task_id'] = task_id
    with lock:
        tasks[task_id] = task
        task['future'] = pool.submit(run_task, task, prompt, path.resolve(), timeout_seconds)
    return {'task_id': task_id, 'state': 'queued'}


@mcp.tool()
async def workbuddy_wait(task_id: str, timeout_seconds: int = 45) -> dict:
    """Wait up to 45 seconds; returns task result or running state. Repeat until terminal."""
    with lock:
        task = tasks.get(task_id)
    if not task:
        return {'state': 'unknown', 'error': 'Task is not known to this MCP process'}
    try:
        result = await asyncio.to_thread(task['future'].result, timeout=max(0, min(timeout_seconds, 45)))
        return {'task_id': task_id, **result}
    except concurrent.futures.TimeoutError:
        return {'task_id': task_id, 'state': 'running', 'session_id': task['session_id']}


@mcp.tool()
async def workbuddy_cancel(task_id: str) -> dict:
    """Request cancellation of only the specified bridge task."""
    with lock:
        task = tasks.get(task_id)
    if not task:
        return {'state': 'unknown'}
    if task['future'].done():
        return {'task_id': task_id, **task['future'].result()}
    task['cancel'].set()
    await asyncio.to_thread(cancel_active_task, task)
    return {'task_id': task_id, 'state': 'cancelling'}


def cancel_active_task(task):
    with task['control_lock']:
        client = task.get('client')
        if task.get('prompt_started') and client and client.session_id:
            client.notify('session/cancel', {'sessionId': client.session_id})


if __name__ == '__main__':
    mcp.run(transport='stdio')
