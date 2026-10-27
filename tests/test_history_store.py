import json
import asyncio
from contextlib import contextmanager
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from history_store import HistoryStore, snowflake, in_history_window, discord_page_fetcher
from market_timer import NY_TZ, recent_trading_days

NOW = datetime(2026, 9, 28, 12, tzinfo=NY_TZ)
START = datetime(2026, 9, 24, 10, tzinfo=NY_TZ)


def message(index, channel='123', start=START):
    dt = start + timedelta(seconds=index)
    return {'id': str(snowflake(dt)), 'channel_id': channel,
            'timestamp': dt.isoformat(), 'content': str(index)}


class Pages:
    def __init__(self, messages):
        self.messages = messages
        self.requests = []
        self.returned = []

    def __call__(self, **params):
        self.requests.append(params)
        rows = sorted(self.messages, key=lambda m: int(m['id']))
        if 'before' in params:
            rows = [m for m in rows if int(m['id']) < int(params['before'])][-params['limit']:]
        else:
            rows = [m for m in rows if int(m['id']) > int(params['after'])][:params['limit']]
        self.returned.extend(m['id'] for m in rows)
        return rows[::-1]


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.store = HistoryStore(self.path / 'history.sqlite3')

    def import_batch(self, batch):
        legacy = self.path / 'legacy.json'
        legacy.write_text(json.dumps(batch))
        original = legacy.read_bytes()
        self.store.import_legacy(legacy)
        self.store.import_legacy(legacy)
        self.assertEqual(legacy.read_bytes(), original)

    def test_calendar_skips_weekend_and_market_holiday(self):
        self.assertEqual([str(d) for d in recent_trading_days(NOW)],
                         ['2026-09-24', '2026-09-25', '2026-09-28'])
        holiday = datetime(2026, 9, 7, 12, tzinfo=NY_TZ)
        self.assertEqual([str(d) for d in recent_trading_days(holiday)],
                         ['2026-09-02', '2026-09-03', '2026-09-04'])
        self.assertFalse(in_history_window(message(0, start=NOW - timedelta(days=1)), NOW))

    def test_cold_start_paginated_then_incremental_without_repeat(self):
        pages = Pages([message(i) for i in range(205)])
        self.assertTrue(self.store.sync('123', pages, NOW))
        self.assertEqual(len(pages.returned), 205)
        self.assertEqual(len(self.store.visible('123', NOW)), 205)
        new = message(1, start=NOW)
        pages.messages.append(new)
        pages.returned.clear()
        self.store = HistoryStore(self.path / 'history.sqlite3')  # restart
        self.store.sync('123', pages, NOW + timedelta(seconds=2))
        self.assertEqual(pages.returned, [new['id']])

    def test_legacy_middle_batch_only_fetches_missing_ends(self):
        batch = [message(i) for i in range(250)]
        self.import_batch(batch[110:125])
        pages = Pages(batch)
        self.store.sync('123', pages, NOW)
        self.assertTrue(set(pages.returned).isdisjoint({m['id'] for m in batch[110:125]}))
        self.assertEqual(len(pages.returned), 235)
        self.assertEqual(len(self.store.visible('123', NOW)), 250)

    def test_failed_forward_resumes_after_committed_page(self):
        pages = Pages([message(i) for i in range(205)])
        def fail_second(**params):
            if len(pages.requests) == 1:
                raise RuntimeError('offline')
            return pages(**params)
        with self.assertRaises(RuntimeError):
            self.store.sync('123', fail_second, NOW)
        first = set(pages.returned)
        self.assertEqual(len(first), 100)
        pages.returned.clear()
        self.store.sync('123', pages, NOW)
        self.assertTrue(first.isdisjoint(pages.returned))
        self.assertEqual(len(self.store.visible('123', NOW)), 205)

    def test_failed_backfill_resumes_before_committed_page(self):
        batch = [message(i) for i in range(350)]
        self.import_batch(batch[320:335])
        pages = Pages(batch)
        def fail_second(**params):
            if len(pages.requests) == 1:
                raise RuntimeError('offline')
            return pages(**params)
        with self.assertRaises(RuntimeError):
            self.store.sync('123', fail_second, NOW)
        first = set(pages.returned)
        pages.returned.clear()
        self.store.sync('123', pages, NOW)
        self.assertTrue(first.isdisjoint(pages.returned))
        self.assertEqual(len(self.store.visible('123', NOW)), 350)

    def test_live_persistence_and_channel_isolation(self):
        pages = Pages([message(0)])
        self.store.sync('123', pages, NOW)
        live = message(1, start=NOW)
        self.store.save('123', [live], advance=True)
        self.store.save('123', [live], advance=True)
        self.store.save('456', [message(2, channel='456')])
        pages.messages.append(live)
        pages.returned.clear()
        self.store.sync('123', pages, NOW + timedelta(seconds=3))
        self.assertEqual(pages.returned, [])
        self.assertEqual(len(self.store.visible('123', NOW)), 2)
        self.assertEqual(len(self.store.visible('456', NOW)), 1)

    def test_old_records_preserved_but_not_displayed(self):
        old = message(0, start=START - timedelta(days=1))
        self.store.save('123', [old, message(1)])
        self.assertEqual(len(self.store.visible('123', NOW)), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 2)

    def test_cancelled_sync_makes_no_request(self):
        pages = Pages([])
        self.assertFalse(self.store.sync('123', pages, NOW, cancelled=lambda: True))
        self.assertEqual(pages.requests, [])

    def test_half_day_monitoring_stops_after_early_close(self):
        import market_timer
        with patch.object(market_timer, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 11, 27, 13, 16, tzinfo=NY_TZ)
            self.assertFalse(market_timer.is_market_monitoring_time()[0])

    def test_rate_limit_shares_cooldown_without_retry(self):
        delays = []
        @contextmanager
        def slot(**kwargs):
            yield delays.append
        with patch('history_store.request_slot', slot), patch('history_store.requests.get') as request:
            request.return_value.status_code = 429
            request.return_value.json.return_value = {'retry_after': 8.5}
            with self.assertRaisesRegex(RuntimeError, '429'):
                discord_page_fetcher('offline', '123')(after='1', limit=100)
            request.assert_called_once()
        self.assertEqual(delays, [8.5])

    def test_unauthorized_request_is_not_retried(self):
        @contextmanager
        def slot(**kwargs):
            yield lambda seconds: None
        with patch('history_store.request_slot', slot), patch('history_store.requests.get') as request:
            request.return_value.status_code = 403
            request.return_value.headers = {}
            with self.assertRaisesRegex(RuntimeError, '403'):
                discord_page_fetcher('offline', '123')(after='1', limit=100)
            request.assert_called_once()

    def test_gateway_never_connects_outside_allowed_hours(self):
        from discord_listener import DiscordGatewayClient
        client = DiscordGatewayClient('offline', '123', lambda msg: None, should_connect=lambda: False)
        client.is_running = True
        with patch('discord_listener.websockets.connect') as connect:
            asyncio.run(client._connect_gateway())
            connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
