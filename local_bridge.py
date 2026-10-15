"""Local MCP adapter for the running WorkBuddy desktop. Serial tasks, scoped approvals."""
import asyncio
import concurrent.futures
import contextlib
import json
import msvcrt
import socket
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit
import native_runtime

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'vendor' / 'desktop-bridge'))
sys.path.insert(0, str(ROOT / 'vendor' / 'live-bridge'))
import acp_live_send as live
from mcp.server.fastmcp import FastMCP
from workbuddy_bridge.acp import AcpClient, DesktopServer, WorkBuddyError
from workbuddy_bridge.activity_log import _tool_name

mcp = FastMCP('workbuddy', instructions=(
    'Delegate only user-authorized tasks to WorkBuddy. Specify an absolute cwd and explicit scope. '
    'Use start then wait. Web search/fetch and file Read/Write/Edit approvals within cwd are supported. '
    'Other permission requests are denied and reported. This is not an OS sandbox. '
    'Verify outputs independently. WorkBuddy must be running and logged in. '
    'If status reports can_attempt_recovery, start may restore the bound chat through local sidecar IPC. '
    'Tasks use one dedicated native chat with persistent shared history. Do not use that chat manually '
    'while a delegated task runs. Never retry a failed or timed-out task automatically. '
    'Use absolute file paths. No desktop typing is needed.'), log_level='WARNING')
# ponytail: one dedicated native chat, serialized across MCP processes with an OS file lock.
pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
tasks = {}
lock = threading.Lock()
NATIVE_LOCK_PATH = ROOT / 'work/native-session.lock'
ACTIVE_PATH = ROOT / 'work/native-active.json'


class NativeRuntimeDormant(RuntimeError):
    pass


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


def native_turn_state(native_cwd, sid):
    path = Path(live._transcript_path(native_cwd, sid))
    with path.open('rb') as handle:
        offset = max(0, path.stat().st_size - 1024 * 1024)
        handle.seek(offset)
        if offset:
            handle.readline()
        lines = handle.read().splitlines(keepends=True)
    rows = []
    for line in lines:
        if not line.endswith(b'\n'):
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return parse_native_turn(rows)


def discover_native_server(activate=False):
    binding = ROOT / 'desktop_session.json'
    if not binding.is_file():
        raise RuntimeError('Not configured: run setup.ps1 and bind an existing native chat on this computer')
    sid = str(uuid.UUID(json.loads(binding.read_text(encoding='utf-8'))['session_id']))
    # Discovery/status must not allocate a competing ACP connection.
    found = live.find_live_endpoint(session_id=sid, probe=False)
    if found:
        port, actual_sid, native_cwd = found
        if actual_sid != sid or not native_cwd:
            raise RuntimeError('Native endpoint identity mismatch')
        endpoint = f'http://127.0.0.1:{port}/api/v1/acp'
    else:
        native_cwd = native_runtime.session_cwd(sid)
        endpoint = None
    state = native_turn_state(native_cwd, sid)
    if state['state'] != 'idle':
        raise RuntimeError('Dedicated native chat is busy or its state is unknown: ' + state['state'])
    if ACTIVE_PATH.exists():
        active = json.loads(ACTIVE_PATH.read_text(encoding='utf-8'))
        if active['session_id'] != sid or not state['user_text'].startswith('[' + active['task_id'] + ']'):
            raise RuntimeError('Previous submission outcome is uncertain; inspect native chat before retrying')
    if endpoint is None:
        endpoint = native_runtime.resolve_runtime(sid, native_cwd, create=activate)
        if endpoint is None:
            raise NativeRuntimeDormant('Native runtime is dormant; workbuddy_start can attempt local sidecar recovery')
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
        super().__init__(server, **kwargs)
        self.root = root
        self.native_cwd = None
        self.permissions = []

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
    params = {'sessionId': sid, 'cwd': native_cwd, 'mcpServers': []}
    try:
        return client.request('session/load', params, timeout_seconds=30)
    except WorkBuddyError as exc:
        if str(exc) != 'Persistent Session already has an external writer':
            raise
    # Only a known pre-prompt rejection permits another load. Never retry a prompt.
    # Caller holds the cross-process lock; the dedicated chat must not be used manually.
    state = native_turn_state(native_cwd, sid)['state']
    if state != 'idle':
        raise RuntimeError('Cannot take over native writer: session is ' + state)
    params['_meta'] = {'codebuddy.ai/takeWriter': True}
    return client.request('session/load', params, timeout_seconds=30)


def _run_native_task(task, prompt, cwd, timeout_seconds):
    client = None
    prompt_started = False
    try:
        if task['cancel'].is_set():
            return {'state': 'cancelled'}
        server, sid, native_cwd = discover_native_server(activate=True)
        client = ScopedClient(server, cwd, timeout_seconds=timeout_seconds)
        client.native_cwd = native_cwd
        task['client'] = client
        client.connect()
        # WorkBuddy 5.8.2 requires session/load on each fresh connection before prompt.
        load_native_writer(client, sid, native_cwd)
        client.session_id = sid
        config = json.loads((ROOT / 'desktop_session.json').read_text(encoding='utf-8'))
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
            active_tmp = ACTIVE_PATH.with_suffix('.tmp')
            active_tmp.write_text(json.dumps({'task_id': task['task_id'], 'session_id': sid}), encoding='utf-8')
            active_tmp.replace(ACTIVE_PATH)
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
                try:
                    client.close()
                except Exception as exc:
                    task['cleanup_error'] = str(exc)


@mcp.tool()
async def workbuddy_status() -> dict:
    """Check connection to the running local WorkBuddy desktop, without exposing credentials."""
    try:
        server, sid, native_cwd = await asyncio.to_thread(discover_native_server)
        return {'connected': True, 'endpoint': server.acp_endpoint, 'concurrency': 1,
                'connection_check': 'TCP reachable; native transcript idle; writer not acquired',
                'session_id': sid, 'native_history': True, 'native_cwd': native_cwd,
                'permissions': 'default mode; scoped allow_once responses; not an OS sandbox'}
    except NativeRuntimeDormant as exc:
        return {'connected': False, 'can_attempt_recovery': True, 'error': str(exc)}
    except Exception as exc:
        return {'connected': False, 'can_attempt_recovery': False, 'error': str(exc)}


@mcp.tool()
def workbuddy_start(prompt: str, cwd: str, timeout_seconds: int = 240) -> dict:
    """Start an authorized task: text, web research, or Read/Write/Edit within explicit absolute cwd."""
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
