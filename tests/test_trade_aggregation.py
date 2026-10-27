import unittest
from datetime import datetime
from ui_window import DennisTradeCard, format_compact_duration

class TestTradeAggregation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        import sys
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def test_duration_calculation(self):
        dur = format_compact_duration("09-25 10:15:00", "09-25 14:04:33")
        self.assertEqual(dur, "3h49m")

        dur_short = format_compact_duration("09-25 10:15:00", "09-25 10:45:00")
        self.assertEqual(dur_short, "30m")

    def test_trade_card_open_then_close_profit(self):
        # 1. 模拟开仓
        card = DennisTradeCard("PLTR", "10/16/26 190P", "PUT", "T1", theme="dark")
        open_data = {
            "strategy": "T1",
            "action": "OPEN",
            "ticker": "PLTR",
            "direction": "PUT",
            "contract": "10/16/26 190P",
            "entry_price": "3.45",
            "size": "1/2x",
            "premium_est": "$172.50",
            "gex_tags": ["悬崖结构", "主导墙-下", "路径薄"]
        }
        card.set_open(open_data, "09-25 10:15:20")
        self.assertEqual(card.status, "OPEN")
        self.assertEqual(card.open_time_str, "09-25 10:15:20")
        self.assertIsNone(card.close_data)

        # 2. 模拟同一个标的平仓（获利了结）
        close_data = {
            "strategy": "T1",
            "action": "CLOSE",
            "ticker": "PLTR",
            "direction": "PUT",
            "contract": "10/16/26 190P",
            "exit_price": "5.80",
            "pnl_pct": "+68.1%",
            "pnl_dollar": "+$235.00",
            "stop_type": "profit_target"
        }
        card.set_close(close_data, "09-25 14:04:33")
        self.assertEqual(card.status, "PROFIT")
        self.assertEqual(card.close_time_str, "09-25 14:04:33")
        self.assertEqual(card.close_data["exit_price"], "5.80")
        self.assertEqual(card.close_data["pnl_pct"], "+68.1%")

    def test_trade_card_open_then_close_loss(self):
        card = DennisTradeCard("ARM", "10/16/26 140C", "CALL", "T1", theme="dark")
        open_data = {
            "strategy": "T1",
            "action": "OPEN",
            "ticker": "ARM",
            "direction": "CALL",
            "contract": "10/16/26 140C",
            "entry_price": "2.80",
            "size": "1x"
        }
        card.set_open(open_data, "09-25 09:45:00")
        self.assertEqual(card.status, "OPEN")

        close_data = {
            "strategy": "T1",
            "action": "CLOSE",
            "ticker": "ARM",
            "direction": "CALL",
            "contract": "10/16/26 140C",
            "exit_price": "2.20",
            "pnl_pct": "-21.4%",
            "pnl_dollar": "-$60.00",
            "stop_type": "hard_stop"
        }
        card.set_close(close_data, "09-25 10:30:00")
        self.assertEqual(card.status, "LOSS")
        self.assertEqual(card.close_time_str, "09-25 10:30:00")

if __name__ == "__main__":
    from PyQt6.QtWidgets import QApplication
    import sys
    app = QApplication(sys.argv)
    unittest.main()
