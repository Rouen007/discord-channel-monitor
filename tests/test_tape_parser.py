import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tape_parser import parse_tape_signal

class TestTapeParser(unittest.TestCase):
    def test_t1_open_with_gex(self):
        embeds = [{
            "type": "rich",
            "title": "T1 OPEN",
            "description": "PLTR P 190 2026-10-16\nEntry 7.25 | 2x\nGEX 悬崖结构 | 主导墙-下 | 路径薄 | 加分"
        }]
        res = parse_tape_signal(embeds=embeds)
        self.assertIsNotNone(res)
        self.assertEqual(res["strategy"], "T1")
        self.assertEqual(res["action"], "OPEN")
        self.assertEqual(res["ticker"], "PLTR")
        self.assertEqual(res["direction"], "PUT")
        self.assertEqual(res["contract"], "10/16/26 190P")
        self.assertEqual(res["entry_price"], "$7.25")
        self.assertEqual(res["size"], "2x")
        self.assertEqual(res["premium_est"], "$1.45K")
        self.assertEqual(res["gex_tags"], ["悬崖结构", "主导墙-下", "路径薄", "加分"])

    def test_t1_close_hard_stop(self):
        embeds = [{
            "type": "rich",
            "title": "T1 CLOSE",
            "description": "META P 755 2026-09-28\nExit 6.88 | -22.1% | -$391.08 | hard_stop"
        }]
        res = parse_tape_signal(embeds=embeds)
        self.assertIsNotNone(res)
        self.assertEqual(res["strategy"], "T1")
        self.assertEqual(res["action"], "CLOSE")
        self.assertEqual(res["ticker"], "META")
        self.assertEqual(res["direction"], "PUT")
        self.assertEqual(res["contract"], "09/28/26 755P")
        self.assertEqual(res["exit_price"], "$6.88")
        self.assertEqual(res["pnl_pct"], "-22.1%")
        self.assertEqual(res["pnl_dollar"], "-$391.08")
        self.assertEqual(res["stop_type"], "hard_stop")

    def test_t1_close_trailing_stop_profit(self):
        embeds = [{
            "type": "rich",
            "title": "T1 CLOSE",
            "description": "CRM C 240 2026-10-16\nExit 9.24 | +39.2% | +$780 | trailing_stop"
        }]
        res = parse_tape_signal(embeds=embeds)
        self.assertIsNotNone(res)
        self.assertEqual(res["strategy"], "T1")
        self.assertEqual(res["action"], "CLOSE")
        self.assertEqual(res["ticker"], "CRM")
        self.assertEqual(res["direction"], "CALL")
        self.assertEqual(res["contract"], "10/16/26 240C")
        self.assertEqual(res["exit_price"], "$9.24")
        self.assertEqual(res["pnl_pct"], "+39.2%")
        self.assertEqual(res["pnl_dollar"], "+$780")
        self.assertEqual(res["stop_type"], "trailing_stop")

    def test_s1_close_triple_digit_gain(self):
        embeds = [{
            "type": "rich",
            "title": "S1 CLOSE",
            "description": "ARM C 277.50 2026-10-02\nExit 26.31 | +120.9% | +$1,440.2 | trailing_stop"
        }]
        res = parse_tape_signal(embeds=embeds)
        self.assertIsNotNone(res)
        self.assertEqual(res["strategy"], "S1")
        self.assertEqual(res["action"], "CLOSE")
        self.assertEqual(res["ticker"], "ARM")
        self.assertEqual(res["direction"], "CALL")
        self.assertEqual(res["contract"], "10/02/26 277.5C")
        self.assertEqual(res["pnl_pct"], "+120.9%")
        self.assertEqual(res["pnl_dollar"], "+$1,440.2")

    def test_bno_2027_exp_no_gex(self):
        embeds = [{
            "type": "rich",
            "title": "T1 OPEN",
            "description": "BNO C 66 2027-01-15\nEntry 5.10 | 3x"
        }]
        res = parse_tape_signal(embeds=embeds)
        self.assertIsNotNone(res)
        self.assertEqual(res["ticker"], "BNO")
        self.assertEqual(res["contract"], "01/15/27 66C")
        self.assertEqual(res["entry_price"], "$5.10")
        self.assertEqual(res["size"], "3x")
        self.assertEqual(res["gex_tags"], [])

    def test_filter_chat_and_daily_nav(self):
        # 每日净值报告过滤
        self.assertIsNone(parse_tape_signal(content="T1 每日净值\n净值 13,110 | 持仓 3 | 日变化 +0.85%"))
        # 闲聊过滤
        self.assertIsNone(parse_tape_signal(content="meta牛逼"))
        self.assertIsNone(parse_tape_signal(content="@everyone Dennis更新了，加入了一些Dealer Gex和Vex的品味..."))


if __name__ == '__main__':
    unittest.main()
