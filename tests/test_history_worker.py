import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ui_window import DennisTapeMonitorWindow


class TestHistoryWorker(unittest.TestCase):
    def window(self):
        return SimpleNamespace(
            config={'channel_id': '123', 'channel_ids': '456,789'},
            _history_guard={'reason': ''},
            history=Mock(), bridge=SimpleNamespace(history_ready=Mock(emit=Mock())))

    def test_event_and_all_channels(self):
        w = self.window()
        w.history.sync.return_value = True
        with patch('ui_window.is_market_monitoring_time', return_value=(True, 'open')):
            DennisTapeMonitorWindow._fetch_history(w, 'offline', 1, 1, threading.Event())
        self.assertEqual([c.args[0] for c in w.history.sync.call_args_list], ['123', '456', '789'])
        w.bridge.history_ready.emit.assert_called_once_with(1, 1, True, '')

    def test_cancelled_event_makes_no_request_or_completion(self):
        w = self.window()
        cancelled = threading.Event()
        cancelled.set()
        DennisTapeMonitorWindow._fetch_history(w, 'offline', 1, 1, cancelled)
        w.history.sync.assert_not_called()
        w.bridge.history_ready.emit.assert_not_called()

    def test_429_stops_round_and_future_rounds(self):
        delays = []
        @contextmanager
        def slot(**kwargs):
            yield delays.append
        w = self.window()
        w.history.sync.side_effect = lambda cid, fetch, **kw: fetch(after='1', limit=100)
        with patch('ui_window.is_market_monitoring_time', return_value=(True, 'open')), \
             patch('history_store.request_slot', slot), \
             patch('history_store.requests.get') as get:
            get.return_value.status_code = 429
            get.return_value.json.return_value = {'retry_after': 90}
            for sync_id in (1, 2):
                DennisTapeMonitorWindow._fetch_history(w, 'offline', 1, sync_id, threading.Event())
            get.assert_called_once()
        self.assertEqual(w.history.sync.call_count, 1)
        self.assertEqual(delays, [90])
        self.assertIn('429', w._history_guard['reason'])
        self.assertFalse(w.bridge.history_ready.emit.call_args.args[2])
