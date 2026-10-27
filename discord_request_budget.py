"""Shared discord CLI budget protocol; cancellable desktop adaptation.

Source: ~/.claude/skills/discord/request_budget.py. Shares its lock/state paths.
No credentials are stored in these files.
"""
import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

STATE = Path.home() / '.local/state/discord-cli/request-budget.json'
INTERVAL = 3.0


@contextlib.contextmanager
def request_slot(cancelled=lambda: False):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(STATE) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'a') as lock:
        while True:
            if cancelled():
                raise RuntimeError('History sync cancelled')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.2)
        try:
            state = json.loads(STATE.read_text()) if STATE.exists() else {}
            deadline = float(state.get('next_request_at', 0))
        except (ValueError, TypeError):
            # Fail closed rather than ignoring damaged shared state.
            raise RuntimeError('Discord request budget is invalid; stop and inspect locally')
        while time.time() < deadline:
            if cancelled():
                raise RuntimeError('History sync cancelled')
            time.sleep(min(0.2, max(0, deadline - time.time())))
        next_request = time.time() + INTERVAL
        def cooldown(seconds):
            nonlocal next_request
            next_request = max(next_request, time.time() + max(0, float(seconds)))
        try:
            yield cooldown
        finally:
            # Lock stays held across the request, including response parsing.
            out = os.open(STATE, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(out, 'w') as f:
                json.dump({'next_request_at': next_request}, f)
            fcntl.flock(lock, fcntl.LOCK_UN)
