"""Local, channel-scoped history. Never expire or overwrite the legacy JSON.

Each successful page is committed with its cursor. A failed request cannot move
the cursor; old messages remain on disk while the view uses only three sessions.
"""
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import requests
from discord_request_budget import request_slot
from market_timer import NY_TZ, recent_trading_days, history_window_start

DISCORD_EPOCH = 1420070400000


def snowflake(dt):
    return (int(dt.timestamp() * 1000) - DISCORD_EPOCH) << 22


def in_history_window(message, now=None):
    try:
        dt = datetime.fromisoformat(message['timestamp'].replace('Z', '+00:00'))
        if dt.tzinfo is None:
            return False
        return dt.astimezone(NY_TZ).date() in recent_trading_days(now)
    except (KeyError, ValueError, TypeError):
        return False


class HistorySyncStopped(RuntimeError):
    """Stop the whole history round and require an explicit user restart."""


class HistoryStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS messages (
                    channel TEXT NOT NULL, id INTEGER NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(channel, id));
                CREATE TABLE IF NOT EXISTS progress (
                    channel TEXT PRIMARY KEY, forward INTEGER, floor INTEGER);
                CREATE TABLE IF NOT EXISTS imports (path TEXT PRIMARY KEY);
            ''')

    def connect(self):
        # Callers use a short-lived connection per operation, including workers.
        from contextlib import contextmanager

        @contextmanager
        def connection():
            db = sqlite3.connect(self.path, timeout=15)
            try:
                with db:
                    yield db
            finally:
                db.close()
        return connection()

    @staticmethod
    def _insert(db, channel, messages):
        for message in messages:
            if str(message.get('channel_id', channel)) != channel:
                raise ValueError('History channel mismatch')
            body = {k: v for k, v in message.items() if not k.startswith('_')}
            body['channel_id'] = channel
            db.execute('INSERT OR IGNORE INTO messages VALUES (?, ?, ?)',
                       (channel, int(body['id']), json.dumps(body, ensure_ascii=False)))

    def save(self, channel, messages, advance=False, filter_fn=None):
        if not messages:
            return
        to_save = [m for m in messages if filter_fn(m)] if filter_fn else messages
        with self.connect() as db:
            if to_save:
                self._insert(db, channel, to_save)
            if advance:
                self._forward(db, channel, max(int(m['id']) for m in messages))

    @staticmethod
    def _forward(db, channel, cursor):
        db.execute('''INSERT INTO progress(channel, forward) VALUES (?, ?)
                      ON CONFLICT(channel) DO UPDATE SET
                      forward=MAX(COALESCE(forward, 0), excluded.forward)''', (channel, cursor))

    def import_legacy(self, path):
        """One-time import of the old REST batch; its original file is untouched."""
        path = Path(path)
        if not path.exists():
            return
        with self.connect() as db:
            if db.execute('SELECT 1 FROM imports WHERE path=?', (str(path),)).fetchone():
                return
            messages = json.loads(path.read_text())
            if not isinstance(messages, list):
                raise ValueError('Legacy cache is not a message list')
            channels = {str(m['channel_id']) for m in messages if m.get('channel_id')}
            for channel in channels:
                batch = [m for m in messages if str(m.get('channel_id')) == channel]
                self._insert(db, channel, batch)
                # Legacy cache is one contiguous REST page, not a live snapshot.
                self._forward(db, channel, max(int(m['id']) for m in batch))
            db.execute('INSERT INTO imports VALUES (?)', (str(path),))

    def visible(self, channel, now=None, allow_weekend=False):
        cutoff = snowflake(history_window_start(now))
        with self.connect() as db:
            rows = db.execute('SELECT body FROM messages WHERE channel=? AND id>=? ORDER BY id',
                              (channel, cutoff)).fetchall()
        if allow_weekend:
            start_dt = history_window_start(now)
            def _check(m):
                try:
                    dt = datetime.fromisoformat(m['timestamp'].replace('Z', '+00:00'))
                    return dt.astimezone(NY_TZ) >= start_dt
                except Exception:
                    return False
            return [m for (body,) in rows if _check(m := json.loads(body))]
        return [m for (body,) in rows if in_history_window(m := json.loads(body), now)]

    def sync(self, channel, fetch_page, now=None, cancelled=lambda: False, filter_fn=None):
        """Fill the older missing tail, then fetch strictly after the saved cursor.

        fetch_page accepts before/after/limit. Gateway messages must be buffered
        by the caller during sync so they cannot jump over a missing REST page.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = snowflake(history_window_start(now))
        with self.connect() as db:
            bounds = db.execute('SELECT MIN(id), MAX(id) FROM messages WHERE channel=?', (channel,)).fetchone()
            state = db.execute('SELECT forward, floor FROM progress WHERE channel=?', (channel,)).fetchone()
        oldest, newest = bounds
        cursor, floor = state if state else (None, None)

        # Existing batches are reused. Only fetch BEFORE the oldest cached row.
        if newest is not None and newest >= cutoff and (floor is None or floor > cutoff):
            before = oldest
            while before > cutoff:
                if cancelled():
                    return False
                page = fetch_page(before=str(before), limit=100)
                if page and any(int(m['id']) >= before for m in page):
                    raise ValueError('History page did not advance backwards')
                self.save(channel, page, filter_fn=filter_fn)
                if not page or len(page) < 100:
                    break
                before = min(int(m['id']) for m in page)
            with self.connect() as db:
                db.execute('''INSERT INTO progress(channel, floor) VALUES (?, ?)
                              ON CONFLICT(channel) DO UPDATE SET floor=excluded.floor''', (channel, cutoff))
        elif newest is None or newest < cutoff:
            # A fresh window never requests all-time history.
            cursor = max(cursor or 0, cutoff - 1)
            with self.connect() as db:
                db.execute('''INSERT INTO progress(channel, floor) VALUES (?, ?)
                              ON CONFLICT(channel) DO UPDATE SET floor=excluded.floor''', (channel, cutoff))

        cursor = max(cursor or cutoff - 1, cutoff - 1)
        while True:
            if cancelled():
                return False
            page = fetch_page(after=str(cursor), limit=100)
            if page and any(int(m['id']) <= cursor for m in page):
                raise ValueError('History page did not advance forwards')
            self.save(channel, page, advance=True, filter_fn=filter_fn)
            if page:
                cursor = max(int(m['id']) for m in page)
            if len(page) < 100:
                break
        with self.connect() as db:
            # Covers an empty interval too; only after the complete scan succeeds.
            self._forward(db, channel, max(cursor, snowflake(now) - 1))
            db.execute('UPDATE progress SET floor=? WHERE channel=?', (cutoff, channel))
        return True


def discord_page_fetcher(token, channel, allowed=lambda: True):
    def fetch(**params):
        with request_slot(cancelled=lambda: not allowed()) as cooldown:
            # Recheck AFTER waiting for the shared request budget.
            if not allowed():
                raise RuntimeError('History sync cancelled or outside monitoring hours')
            response = requests.get(
                f'https://discord.com/api/v9/channels/{channel}/messages',
                params=params, headers={'Authorization': token}, timeout=15)
            if response.status_code == 429:
                try:
                    delay = max(3.0, float(response.json().get('retry_after', 60)))
                except (ValueError, TypeError):
                    delay = 60.0
                cooldown(delay)
                raise HistorySyncStopped(f'HTTP 429: 暂停补齐，保留进度，冷却 {delay:g} 秒')
            if response.status_code in (401, 403):
                raise HistorySyncStopped(f'历史补齐 HTTP {response.status_code}，已停止自动补齐，请检查权限')
            if response.headers.get('X-RateLimit-Remaining') == '0':
                cooldown(float(response.headers.get('X-RateLimit-Reset-After', 3)))
            if response.status_code != 200:
                # Never log response bodies or authentication headers.
                raise RuntimeError(f'历史补齐 HTTP {response.status_code}，保留进度')
            page = response.json()
            if not isinstance(page, list):
                raise ValueError('Invalid history response')
            return page
    return fetch
