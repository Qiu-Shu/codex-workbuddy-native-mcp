"""Persistent conversation handles backed by disposable, isolated native workers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import msvcrt
from pathlib import Path
import threading
import uuid

import local_bridge as bridge
from background_runtime import owned_worker, stop_owned, validate_model_id

BACKGROUND_REVISION = '2026-10-24-verified-model-v2'
CANCEL_GRACE_SECONDS = 8
STORE = Path.home() / '.codex/workbuddy-background'
POOL = ThreadPoolExecutor(max_workers=2)
_SOURCES = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in
            (Path(__file__), Path(__file__).with_name('background_runtime.py'))}


def now():
    return datetime.now(timezone.utc).isoformat()


def check_revision():
    bridge.ensure_current_revision()
    if any(hashlib.sha256(p.read_bytes()).hexdigest() != digest for p, digest in _SOURCES.items()):
        raise RuntimeError('Background bridge changed; reload WorkBuddy MCP before submitting')


def record_path(session_id):
    try:
        if str(uuid.UUID(session_id)) != session_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Use a background session ID returned by this bridge') from None
    return STORE / (session_id + '.json')


def read_record(session_id):
    try:
        record = json.loads(record_path(session_id).read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise ValueError('Unknown background session; desktop session IDs cannot be used here') from None
    if record['session_id'] != session_id:
        raise ValueError('Background session identity mismatch')
    return record


def save_record(record):
    STORE.mkdir(parents=True, exist_ok=True)
    record['updated_at'] = now()
    dest = record_path(record['session_id'])
    temp = dest.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(dest)
    finally:
        temp.unlink(missing_ok=True)


def acquire_session(session_id):
    path = record_path(session_id).with_suffix('.lock')
    STORE.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    try:
        if handle.tell() == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return handle
    except OSError:
        handle.close()
        raise RuntimeError('This background session already has a queued or running task') from None


def release_session(handle):
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def public_record(record):
    return {k: record.get(k) for k in ('session_id', 'native_session_id', 'cwd', 'title', 'state',
            'created_at', 'updated_at', 'requires_recovery', 'native_history_visible', 'model_id', 'actual_model_id')}


def background_start(prompt: str, cwd: str, session_id: str = '', timeout_seconds: int = 240, model_id: str = '') -> dict:
    check_revision()
    validate_model_id(model_id)
    path = Path(cwd)
    if not path.is_absolute() or not path.is_dir() or not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('Provide a nonempty prompt and an existing absolute cwd')
    if isinstance(timeout_seconds, bool) or not 10 <= timeout_seconds <= 600:
        raise ValueError('timeout_seconds must be between 10 and 600')
    if len(prompt) > 200000:
        raise ValueError('Prompt must be at most 200000 characters')
    path = path.resolve()
    sid = session_id or str(uuid.uuid4())
    handle = acquire_session(sid)
    try:
        if session_id:
            record = read_record(sid)
            if Path(record['cwd']).resolve() != path:
                raise ValueError('A background session cannot change its authorized cwd')
            if record.get('requires_recovery') or record['state'] in ('queued', 'running'):
                raise RuntimeError('Inspect history and use workbuddy_background_recover before a new instruction; do not retry')
        else:
            record = {'session_id': sid, 'native_session_id': None, 'cwd': str(path),
                      'title': prompt.strip().splitlines()[0][:80], 'created_at': now(),
                      'native_history_visible': False, 'turns': []}
        record['model_id'] = model_id or record.get('model_id', '')
        task_id = 'wb-bg-' + uuid.uuid4().hex[:12]
        record.update(state='queued', requires_recovery=False, task_id=task_id)
        record['turns'].append({'task_id': task_id, 'prompt': prompt, 'state': 'queued',
                                'requested_model_id': record['model_id'], 'started_at': now()})
        save_record(record)
        task = {'task_id': task_id, 'session_id': sid, 'cancel': threading.Event(),
                'control_lock': threading.Lock(), 'client': None, 'prompt_started': False}
        with bridge.lock:
            bridge.tasks[task_id] = task
            task['future'] = POOL.submit(run_turn, record, task, prompt, timeout_seconds, handle)
        return {'task_id': task_id, 'session_id': sid, 'state': 'queued', 'native_history_visible': False,
                'requested_model_id': record['model_id']}
    except Exception:
        release_session(handle)
        raise


def run_turn(record, task, prompt, timeout_seconds, handle):
    client = None
    submitted = False
    cleaned = False
    finished = threading.Event()
    result = {'state': 'failed'}
    model_info = {}
    try:
        if task['cancel'].is_set():
            result = {'state': 'cancelled', 'retry_safe': True}
        else:
            record['state'] = 'running'
            save_record(record)
            def remember_worker(metadata):
                record['worker'] = dict(metadata)
                save_record(record)
            with owned_worker(Path(record['cwd']), on_created=remember_worker,
                              model_id=record.get('model_id', '')) as (server, metadata):
                client = bridge.ScopedClient(server, Path(record['cwd']), timeout_seconds=timeout_seconds)
                client.native_cwd = record['cwd']
                client.connect()
                if record.get('native_session_id'):
                    client.load_session(record['native_session_id'], record['cwd'], as_task=False)
                else:
                    record['native_session_id'] = client.new_session(record['cwd'], as_task=False)
                # Verify native metadata before sending any user content; self-reported identity is not evidence.
                loaded = client.request('session/load', {'sessionId': record['native_session_id'],
                    'cwd': record['cwd'], 'mcpServers': []}, timeout_seconds=30)
                models = loaded.get('result', loaded).get('models') or {}
                actual = models.get('currentModelId')
                requested = record.get('model_id', '')
                offered = {row.get('modelId') for row in models.get('availableModels', []) if isinstance(row, dict)}
                model_ok = isinstance(actual, str) and bool(actual) and (not requested or actual == requested and requested in offered)
                model_info = {'requested_model_id': requested, 'actual_model_id': actual if isinstance(actual, str) else None,
                              'model_verified': model_ok, 'model_source': 'native ACP session/load'}
                record['actual_model_id'] = model_info['actual_model_id']
                save_record(record)
                with task['control_lock']:
                    task['client'] = client
                    if not model_ok:
                        result = {'state': 'failed', 'error_code': 'model_verification_failed',
                                  'submission_may_have_started': False, 'retry_safe': True,
                                  'error': 'Native metadata did not confirm the requested model; no prompt was sent.'}
                    elif task['cancel'].is_set():
                        result = {'state': 'cancelled', 'retry_safe': True}
                    else:
                        task['prompt_started'] = submitted = True
                if submitted:
                    # Cancellation has a bounded grace period even if ACP never acknowledges it.
                    def cancellation_watch():
                        while not finished.wait(0.25):
                            if task['cancel'].is_set():
                                if not finished.wait(CANCEL_GRACE_SECONDS):
                                    try:
                                        stop_owned(metadata)
                                    except Exception:
                                        pass  # finally retries cleanup and records uncertainty
                                return
                    threading.Thread(target=cancellation_watch, daemon=True).start()
                    record['turns'][-1]['state'] = 'submitted'
                    save_record(record)
                    response = client.prompt(
                        f'[{task["task_id"]}] Authorized task directory: {record["cwd"]}. '
                        'Only use files inside it, with absolute paths. Use Read/Write/Edit for files '
                        'and WebSearch/WebFetch for research. Do not run shell commands, access other '
                        'accounts/connectors, save persistent memory, or delegate. If denied, report and stop.\n\n' + prompt)
                    stop = response['result'].get('stopReason')
                    result = {'state': 'completed' if stop == 'end_turn' else 'cancelled' if stop == 'cancelled' else 'failed',
                              'answer': response['answer'], 'stop_reason': stop, 'permissions': client.permissions,
                              'retry_safe': False}
                finished.set()
                with task['control_lock']:
                    task['client'] = None
                    client.close()
                    client = None
            cleaned = True
            record.pop('worker', None)
    except Exception as exc:
        result = {**result, 'state': 'cancelled' if task['cancel'].is_set() else 'failed',
                  'error_type': type(exc).__name__, 'submission_may_have_started': submitted,
                  'retry_safe': not submitted,
                  'error': 'Background turn did not finish cleanly. Inspect history and recover this session; no prompt was retried.'}
        # owned_worker has already attempted cleanup. Retain exact identity for explicit recovery.
    finally:
        finished.set()
        with task['control_lock']:
            task['client'] = None
            if client:
                try:
                    client.close()
                except Exception as exc:
                    result.update(state='failed', close_error_type=type(exc).__name__)
        result.update(model_info)
        record['state'] = result['state']
        record['requires_recovery'] = bool(record.get('worker')) or (submitted and result.get('stop_reason') not in ('end_turn', 'cancelled'))
        record['turns'][-1].update(result, finished_at=now())
        result.update(session_id=record['session_id'], native_session_id=record.get('native_session_id'),
                      native_history_visible=False, worker_released=cleaned,
                      requires_recovery=record['requires_recovery'])
        try:
            save_record(record)
        finally:
            release_session(handle)
    return result


def background_sessions() -> dict:
    rows = []
    for path in STORE.glob('*.json'):
        try:
            rows.append(public_record(read_record(path.stem)))
        except (ValueError, OSError, KeyError):
            continue
    return {'revision': BACKGROUND_REVISION, 'sessions': sorted(rows, key=lambda r: r['updated_at'], reverse=True),
            'native_history_visible': False}


def background_history(session_id: str, limit: int = 20) -> dict:
    if isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError('limit must be 1 to 100')
    record = read_record(session_id)
    turns = []
    for turn in record['turns'][-limit:]:
        item = {k: turn[k] for k in ('task_id', 'state', 'prompt', 'answer', 'stop_reason', 'started_at', 'finished_at',
                                     'requested_model_id', 'actual_model_id', 'model_verified', 'model_source') if k in turn}
        for key in ('prompt', 'answer'):
            if len(item.get(key, '')) > 20000:
                item[key] = item[key][:20000] + '\n[truncated; full text remains in local record]'
        turns.append(item)
    return {**public_record(record), 'turns': turns, 'total_turns': len(record['turns']),
            'note': 'Bridge receipts; an interrupted turn may have additional text in the native JSONL transcript.'}


def background_recover(session_id: str) -> dict:
    check_revision()
    handle = acquire_session(session_id)
    try:
        record = read_record(session_id)
        if record.get('worker'):
            stop_owned(record['worker'])
        record.pop('worker', None)
        record.update(state='recovered', requires_recovery=False)
        save_record(record)
        return {**public_record(record), 'resubmitted': False,
                'note': 'Owned worker released. Review history before issuing a new instruction; previous side effects may exist.'}
    finally:
        release_session(handle)


def register(mcp):
    @mcp.tool()
    def workbuddy_background_start(prompt: str, cwd: str, session_id: str = '', timeout_seconds: int = 240, model_id: str = '') -> dict:
        """Preferred for automatic independent WorkBuddy chats. No manual opening/waking required.

        Authorized tasks only. Absolute existing cwd and explicit scope required. Empty session_id creates a
        new isolated conversation; pass this tool's returned session_id to resume it with the same cwd.
        Pass model_id from workbuddy_models(cwd=...). It is verified before submitting and retained on resume.
        No model_id uses the worker default for new chats. Shared desktop model selection is unrelated.
        timeout_seconds is 10 to 600. Do not reject this route because shared_session.connected is false.
        Use workbuddy_wait/cancel with task_id. Worker automatically releases after each turn.
        Persistent history is available in Codex; it is NOT in the WorkBuddy desktop chat list.
        Same scoped file/web permissions as workbuddy_start. Never automatically retry uncertain tasks.
        """
        return background_start(prompt, cwd, session_id, timeout_seconds, model_id)

    @mcp.tool()
    def workbuddy_background_sessions() -> dict:
        """List bridge-created independent conversations and persisted task states; no prompts submitted."""
        return background_sessions()

    @mcp.tool()
    def workbuddy_background_history(session_id: str, limit: int = 20) -> dict:
        """Read a bridge-created conversation's saved prompts and answers in Codex. Read-only."""
        return background_history(session_id, limit)

    @mcp.tool()
    async def workbuddy_background_recover(session_id: str) -> dict:
        """After inspecting interrupted history, release only this bridge-owned worker and clear uncertainty.

        Does not resend a prompt, delete history, or stop a desktop chat. Cannot recover an actively locked task:
        use workbuddy_cancel and wait for that task first. Previous external effects are not undone.
        """
        return await asyncio.to_thread(background_recover, session_id)
