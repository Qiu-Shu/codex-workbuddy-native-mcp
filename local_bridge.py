"""Local MCP adapter for the running WorkBuddy desktop. Serial tasks, scoped approvals."""
import asyncio
import concurrent.futures
import contextlib
import hashlib
import json
import msvcrt
import os
import socket
import native_runtime
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
BRIDGE_REVISION = '2026-10-24-merged-guards-v6'
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
    'Prefer workbuddy_background_start for automatic independent conversations, with saved history in Codex. '
    'A disconnected shared chat does not block background tasks. Check status.available and background.ready. '
    'Use workbuddy_models(cwd=...) for an independent model catalog, then pass model_id to background_start; '
    'the bridge verifies it before sending the prompt. Never rely on the model describing its own identity. '
    'The legacy workbuddy_start uses one dedicated native chat with shared history. Do not use that chat manually '
    'while a delegated task runs. Shared chat requires desktop idle; writer takeover is never used. '
    'Never retry a failed or timed-out task automatically. '
    'Use absolute file paths. No desktop typing is needed.'), log_level='WARNING')
# ponytail: one dedicated native chat, serialized across MCP processes with an OS file lock.
pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
tasks = {}
lock = threading.Lock()
NATIVE_LOCK_PATH = ROOT / 'work/native-session.lock'
ACTIVE_PATH = ROOT / 'work/native-active.json'
TERMINAL_PATH = ROOT / 'work/native-terminal.json'


class NativeRuntimeDormant(RuntimeError):
    pass


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


def parse_desktop_turn(lines):
    """The desktop can be working before its prompt reaches the transcript."""
    state = 'unknown'
    for line in lines:
        head, separator, body = line.partition(' {')
        if not separator:
            continue
        try:
            data = json.loads('{' + body)
        except ValueError:
            continue
        if head.endswith(' method:sendPrompt'):
            state = 'busy'
        elif head.endswith(' state-machine:transition'):
            state = 'idle' if data.get('to') == 'idle' else 'busy'
        elif state == 'unknown' and head.endswith(' method:requests:result'):
            states = data.get('states')
            if isinstance(states, dict):
                terminal = {'completed', 'cancelled', 'error', 'failed'}
                state = 'busy' if any(count for key, count in states.items() if key not in terminal) else 'idle'
    return state


def desktop_turn_state(sid):
    sid = str(uuid.UUID(sid))
    root = Path.home() / '.workbuddy/logs'
    paths = list(root.glob('????-??-??/sdk/conversations/' + sid + '.log'))
    if not paths:
        return 'unknown'
    path = max(paths, key=lambda item: item.stat().st_mtime_ns)
    with path.open('rb') as handle:
        offset = max(0, path.stat().st_size - 256 * 1024)
        handle.seek(offset)
        if offset:
            handle.readline()
        lines = handle.read().splitlines(keepends=True)
    return parse_desktop_turn(line.decode('utf-8', errors='replace').rstrip()
                              for line in lines if line.endswith(b'\n'))


def require_desktop_idle(sid):
    state = desktop_turn_state(sid)
    if state != 'idle':
        raise RuntimeError('Dedicated desktop chat is ' + state +
                           '; wait for the window to finish before submitting. No task sent.')


def discover_native_server(activate=False):
    ensure_current_revision()
    binding = ROOT / 'desktop_session.json'
    if not binding.is_file():
        raise RuntimeError('Not configured: run setup.ps1 and bind an existing native chat on this computer')
    sid = str(uuid.UUID(json.loads(binding.read_text(encoding='utf-8'))['session_id']))
    # Discovery/status must not allocate a competing ACP connection.
    native_cwd = native_runtime.session_cwd(sid)
    state = native_turn_state(native_cwd, sid)
    if state['state'] != 'idle':
        raise RuntimeError('Dedicated native chat is busy or its state is unknown: ' + state['state'])
    if ACTIVE_PATH.exists():
        active = json.loads(ACTIVE_PATH.read_text(encoding='utf-8'))
        if active['session_id'] != sid or not state['user_text'].startswith('[' + active['task_id'] + ']'):
            raise RuntimeError('Previous submission outcome is uncertain; inspect native chat before retrying')
    owned = native_runtime.owned_endpoints(sid)
    candidates = {}
    for record in live.list_interactive_sessions(probe=False):
        if record.get('sessionId') != sid or not record.get('port'):
            continue
        endpoint = f"http://127.0.0.1:{record['port']}/api/v1/acp"
        if endpoint in owned:
            continue
        if not record.get('cwd') or Path(record['cwd']).resolve() != Path(native_cwd).resolve():
            raise RuntimeError('Native endpoint working directory mismatch')
        candidates[endpoint] = record
    if not candidates:
        raise NativeRuntimeDormant(
            'No desktop runtime is connected. Open the bound WorkBuddy chat and click its input box '
            'without typing or sending a message; keep that chat open, then check status again. '
            'Opening the window or a deep link alone is insufficient on WorkBuddy 5.8.3. '
            'Automatic recovery is unavailable; no task sent.')
    if len(candidates) != 1:
        raise RuntimeError('Native desktop runtime is ambiguous; no task submitted')
    endpoint = next(iter(candidates))
    require_desktop_idle(sid)
    with socket.create_connection(('127.0.0.1', urlsplit(endpoint).port), timeout=3):
        pass
    return DesktopServer(endpoint, '', sid, '', 0), sid, native_cwd


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
            result = _run_native_task(task, prompt, cwd, timeout_seconds)
            if task.get('cleanup_error'):
                result['connection_cleanup_error'] = task['cleanup_error']
            return result
    except Exception as exc:
        return {'state': 'failed', 'error': str(exc)}


def load_native_writer(client, sid, native_cwd):
    require_desktop_idle(sid)
    params = {'sessionId': sid, 'cwd': native_cwd, 'mcpServers': []}
    # takeWriter closes the desktop's notification stream. A conflict must stop
    # before prompt submission, even when the transcript looks idle.
    return client.request('session/load', params, timeout_seconds=30)


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
        load_native_writer(client, sid, native_cwd)
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
            require_desktop_idle(sid)
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
        error = None
        if stop not in ('end_turn', 'cancelled'):
            try:
                error = native_turn_state(native_cwd, sid)['error']
            except OSError:
                pass
        result = {'state': 'completed' if stop == 'end_turn' else 'cancelled' if stop == 'cancelled' else 'failed',
                  'session_id': client.session_id, 'answer': response['answer'],
                  'stop_reason': stop, 'error': error, 'permissions': client.permissions,
                  'tool_events': events, 'retry_safe': False}
        try:
            saved = record_terminal_turn(native_cwd, sid, task['task_id'], stop)
            if saved:
                ACTIVE_PATH.unlink(missing_ok=True)
            result.update(terminal_receipt_saved=saved, requires_recovery=not saved)
        except Exception as exc:
            # A confirmed response survives local bookkeeping failure. Keep the active marker.
            result.update(requires_recovery=True, terminal_bookkeeping_error=type(exc).__name__)
        return result
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
                try:
                    client.close()
                except Exception as exc:
                    task['cleanup_error'] = str(exc)


@mcp.tool()
async def workbuddy_status() -> dict:
    """Report shared-chat and independent-background readiness separately. connected is shared-only.

    Use available/background.ready for automatic tasks; a missing shared endpoint does not block them.
    Background readiness checks the native sidecar, not model inference or account quota.
    """
    runtime = {'bridge_revision': BRIDGE_REVISION, 'process_id': os.getpid(), 'source_sha256': SOURCE_SHA256}
    try:
        ensure_current_revision()
    except BridgeUpdateRequired as exc:
        return {**runtime, 'available': False, 'connected': False,
                'error_code': 'bridge_update_required', 'error': str(exc)}
    import background_runtime
    try:
        await asyncio.to_thread(background_runtime.find_sidecar)
        background = {'ready': True, 'check': 'native_sidecar', 'model_inference_verified': False}
    except Exception as exc:
        background = {'ready': False, 'error_type': type(exc).__name__,
                      'error': 'No usable native sidecar. Keep WorkBuddy running and logged in.'}
    try:
        def probe():
            with native_session_lock():
                return discover_native_server()
        server, sid, native_cwd = await asyncio.to_thread(probe)
        shared = {'connected': True, 'endpoint': server.acp_endpoint, 'concurrency': 1,
                'session_id': sid, 'native_history': True, 'native_cwd': native_cwd,
                'permissions': 'default mode; scoped allow_once responses; not an OS sandbox'}
    except Exception as exc:
        shared = {'connected': False, 'error': str(exc),
                'error_code': 'bridge_update_required' if isinstance(exc, BridgeUpdateRequired) else 'native_unavailable'}
        if isinstance(exc, NativeRuntimeDormant):
            shared.update(can_attempt_recovery=False, requires_input_focus=True,
                          recovery_action='click_bound_chat_input_without_sending')
    return {**runtime, **shared, 'connected_scope': 'shared_session_only',
            'available': background['ready'] or shared['connected'],
            'shared_session': shared, 'background': background,
            'recommended_tool': 'workbuddy_background_start' if background['ready'] else
                                'workbuddy_start' if shared['connected'] else None,
            'guidance': 'Use the independent background route when ready; it does not require an open shared chat.'}


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
