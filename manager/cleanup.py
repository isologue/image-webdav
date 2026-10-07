import json
import logging
import os
import stat
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


log = logging.getLogger(__name__)
DATA = Path('/data/chatgpt2api/images')
STATE_FILE = Path('/config/cleanup.json')
DEFAULT = {'enabled': False, 'retention_hours': 72, 'interval_hours': 1,
           'next_run_at': None, 'last_result': None}
_state_lock = threading.Lock()
_run_lock = threading.Lock()
_stop = threading.Event()
_thread = None


def _timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _read_state():
    if not STATE_FILE.exists():
        return DEFAULT.copy()
    return {**DEFAULT, **json.loads(STATE_FILE.read_text(encoding='utf-8'))}


def _write_state(state):
    fd, temp_name = tempfile.mkstemp(dir=STATE_FILE.parent, prefix='.cleanup-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(state, output)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, STATE_FILE)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def get_state():
    with _state_lock:
        return _read_state()


def set_policy(enabled, retention_hours, interval_hours):
    with _state_lock:
        state = _read_state()
        state.update(enabled=enabled, retention_hours=retention_hours,
                     interval_hours=interval_hours,
                     next_run_at=_timestamp(time.time() + interval_hours * 3600) if enabled else None)
        _write_state(state)
        return state


def remove_expired(hours, root=None, now=None):
    root = DATA if root is None else Path(root)
    cutoff = (time.time() if now is None else now) - hours * 3600
    result = {'deleted': 0, 'freed_bytes': 0, 'failed': 0}
    if not root.is_dir():
        return result

    def visit_error(exc):
        result['failed'] += 1
        log.warning('Image cleanup scan failed: %s', exc)

    for directory, dirs, files in os.walk(root, topdown=False, followlinks=False, onerror=visit_error):
        for name in files:
            path = Path(directory) / name
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff:
                    continue
                path.unlink()
                result['deleted'] += 1
                result['freed_bytes'] += info.st_size
            except FileNotFoundError:
                continue
            except OSError as exc:
                visit_error(exc)
        for name in dirs:
            path = Path(directory) / name
            try:
                if not path.is_symlink():
                    path.rmdir()
            except (OSError, FileNotFoundError):
                pass
    return result


def run_cleanup(hours, trigger='manual'):
    with _run_lock:
        due_at = None
        if trigger == 'scheduled':
            with _state_lock:
                state = _read_state()
                if (not state['enabled'] or state['retention_hours'] != hours or
                        not state['next_run_at'] or
                        datetime.fromisoformat(state['next_run_at']).timestamp() > time.time()):
                    return None
                due_at = state['next_run_at']
        result = remove_expired(hours)
        result.update(hours=hours, trigger=trigger, finished_at=_timestamp(time.time()))
        with _state_lock:
            state = _read_state()
            state['last_result'] = result
            if (trigger == 'scheduled' and state['enabled'] and
                    state['retention_hours'] == hours and state['next_run_at'] == due_at):
                state['next_run_at'] = _timestamp(time.time() + state['interval_hours'] * 3600)
            _write_state(state)
        return result


def run_due_cleanup():
    state = get_state()
    if (state['enabled'] and state['next_run_at'] and
            datetime.fromisoformat(state['next_run_at']).timestamp() <= time.time()):
        run_cleanup(state['retention_hours'], trigger='scheduled')


def _scheduler():
    while not _stop.wait(30):
        try:
            run_due_cleanup()
        except Exception:
            log.exception('Scheduled image cleanup failed')


def start_scheduler():
    global _thread
    _stop.clear()
    _thread = threading.Thread(target=_scheduler, daemon=True, name='image-cleanup')
    _thread.start()


def stop_scheduler():
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5)
