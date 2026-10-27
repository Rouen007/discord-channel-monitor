"""Packaged desktop entry point and offline smoke check for Discord Channel Monitor."""
import os
import sys

if "--smoke-test" in sys.argv:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PyQt6.QtWidgets import QApplication
    from tape_parser import parse_tape_signal
    from ui_window import DennisTradeCard

    app = QApplication([])
    parsed = parse_tape_signal(embeds=[{
        "title": "T1 OPEN",
        "description": "PLTR P 190 2026-10-16\nEntry 7.25 | 2x\nGEX 悬崖结构 | 主导墙-下 | 路径薄 | 加分",
    }])
    assert parsed and parsed["ticker"] == "PLTR"
    card = DennisTradeCard(parsed["ticker"], parsed["contract"], parsed["direction"], parsed["strategy"])
    card.set_open(parsed, "09-25 18:28:04")
    print("SMOKE_OK: Qt, Discord Channel Monitor, specialized trade card")
else:
    from ui_window import main
    main()
