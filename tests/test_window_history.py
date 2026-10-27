import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PyQt6.QtWidgets import QApplication

import ui_window as ui
from history_store import HistoryStore, in_history_window
from test_history_store import NOW, message


def signal(index, strategy='T1', action='OPEN'):
    result = message(index)
    result['embeds'] = [{'title': f'{strategy} {action}', 'description':
        'PLTR P 190 2026-10-16\n' + ('Entry 7.25 | 2x' if action == 'OPEN' else 'Exit 9.00')}]
    return result


class TestWindowHistory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = [
            patch.object(ui, 'HISTORY_DB_PATH', str(Path(self.tmp.name) / 'history.sqlite3')),
            patch.object(ui, 'HISTORY_CACHE_PATH', str(Path(self.tmp.name) / 'absent.json')),
            patch.object(ui, 'load_config', return_value={
                'token': 'offline-test', 'channel_id': '123', 'fixed_expanded': True,
                'only_market_hours': True}),
            patch.object(ui, 'is_market_monitoring_time', return_value=(False, '休市中')),
            patch.object(ui, 'in_history_window', side_effect=lambda m: in_history_window(m, NOW)),
            patch.object(ui.DennisTapeMonitorWindow, 'setup_cocoa_level'),
            patch.object(ui, 'DiscordGatewayClient'),
        ]
        self.mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.window = ui.DennisTapeMonitorWindow()
        self.addCleanup(self.window.close)
        store = self.window.history
        store.visible = lambda channel, *args, **kwargs: HistoryStore.visible(store, channel, NOW, *args, **kwargs)
        self.window.show_toast_popup = lambda *args, **kwargs: None

    def test_halt_survives_scheduler_until_manual_reset(self):
        self.window._pending_messages = [signal(1)]
        self.window._connection_status(self.window._generation, '🔴 已停止自动监听：连续失败 5 次')
        self.assertTrue(self.window._monitoring_halted)
        self.assertEqual(len(self.window.history.visible('123')), 1)
        self.mocks[3].return_value = (True, '交易进行中')
        self.window.check_market_status()
        self.mocks[-1].assert_not_called()
        self.assertEqual(self.window.status_text.text(), '已停止自动监听')
        self.assertFalse(self.window.recover_btn.isHidden())
        self.assertIn('连续失败', self.window.connection_notice.text())
        self.assertIn('⛔', self.window.edge_label.text())
        self.window.start_monitoring(reset_safety=True)
        self.mocks[-1].assert_called_once()
        self.assertFalse(self.window._monitoring_halted)

    def test_recovery_button_resets_once_and_connected_hides_notice(self):
        self.window._connection_status(self.window._generation, '🔴 已停止自动监听：连接线程异常')
        self.mocks[3].return_value = (True, '交易进行中')
        self.window.recover_btn.click()
        self.mocks[-1].assert_called_once()
        self.assertFalse(self.window._monitoring_halted)
        self.window.update_status('🟢 已连接')
        self.assertTrue(self.window.recover_btn.isHidden())
        self.assertTrue(self.window.connection_notice.isHidden())

    def test_history_pause_survives_gateway_resume(self):
        self.window._history_guard['reason'] = 'HTTP 429 自动补齐已暂停'
        self.window._loading_history = True
        self.window._pending_messages = [signal(1)]
        with patch.object(self.window._history_executor, 'submit') as submit:
            self.window._connection_status(self.window._generation, '🟢 会话已恢复')
            submit.assert_not_called()
        self.assertFalse(self.window._loading_history)
        self.assertFalse(self.window._history_ok)
        self.assertEqual(len(self.window.history.visible('123')), 1)
        self.assertEqual(self.window.status_text.text(), '补齐已暂停')
        self.window.start_monitoring(reset_safety=True)
        self.assertEqual(self.window._history_guard['reason'], '')

    def test_network_cooldown_does_not_halt_or_start_another_client(self):
        self.window._connection_status(self.window._generation, '🟡 等待网络恢复：5分钟后自动尝试')
        self.assertFalse(self.window._monitoring_halted)
        self.assertEqual(self.window.status_text.text(), '等待恢复')
        self.assertFalse(self.window.connection_notice.isHidden())
        self.assertTrue(self.window.recover_btn.isHidden())
        self.mocks[-1].assert_not_called()
        self.assertFalse(self.window._history_ok)

    def test_offhours_loads_cache_without_connection(self):
        self.window.history.save('123', [signal(1)])
        self.window.start_monitoring()
        self.mocks[-1].assert_not_called()
        self.assertEqual(len(self.window.active_positions), 1)

    def test_buffered_close_merges_after_history_without_missing_pnl_crash(self):
        self.window.history.save('123', [signal(1)])
        self.window._loading_history = True
        self.window.handle_incoming_signal(signal(2, action='CLOSE'))
        self.assertEqual(len(self.window._pending_messages), 1)
        self.window._history_ready(self.window._generation, self.window._sync_id, True, '')
        self.assertEqual(len(self.window.active_positions), 0)
        cards = self.window.signals_container.findChildren(ui.DennisTradeCard)
        self.assertEqual(len(cards), 1)
        self.assertIsNotNone(cards[0].open_data)
        self.assertIsNotNone(cards[0].close_data)

    def test_strategy_isolation_and_duplicate_replay(self):
        self.window.handle_incoming_signal(signal(1))
        self.window.handle_incoming_signal(signal(2, strategy='S1'))
        self.window.handle_incoming_signal(signal(2, strategy='S1'))
        self.assertEqual(len(self.window.active_positions), 2)
        self.window.handle_incoming_signal(signal(3, action='CLOSE'))
        self.assertEqual(len(self.window.active_positions), 1)
        self.assertEqual(next(iter(self.window.active_positions.values())).strategy, 'S1')

    def test_market_close_cancels_client_and_history(self):
        from unittest.mock import Mock
        client = Mock()
        self.window.dc_client = client
        self.window.check_market_status()
        client.stop.assert_called_once()
        self.assertTrue(self.window._history_cancel.is_set())
        self.assertIsNone(self.window.dc_client)

    def test_stale_worker_completion_is_ignored(self):
        self.window._loading_history = True
        self.window._history_ready(self.window._generation - 1, self.window._sync_id, True, '')
        self.assertTrue(self.window._loading_history)

if __name__ == '__main__':
    unittest.main()
