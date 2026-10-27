"""Local connection diagnostics; never record payloads, tokens, or exception text."""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

PATH = Path.home() / '.local/state/dennis-tape-monitor/diagnostics/connection.jsonl'
_lock = threading.Lock()


def record_status(status):
    try:
        with _lock:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps({
                    'time': datetime.now(timezone.utc).isoformat(),
                    'pid': os.getpid(), 'status': status,
                }, ensure_ascii=False) + '\n')
    except OSError:
        pass  # Diagnostics must never stop monitoring.


def exception_reason(exc):
    parts = [type(exc).__name__]
    for label, value in (
        ('errno', getattr(exc, 'errno', None)),
        ('server', getattr(getattr(exc, 'rcvd', None), 'code', None)),
        ('client', getattr(getattr(exc, 'sent', None), 'code', None)),
    ):
        if isinstance(value, int):
            parts.append(f'{label}={value}')
    return ' '.join(parts)
