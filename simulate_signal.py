#!/usr/bin/env python3
"""
Discord channel monitor demo message injector
用于本地调试卡片渲染、GEX胶囊标签、止盈止损显示与贴边未读角标联动
"""
import json
import os
import sys
import time

MOCK_PATH = os.path.expanduser("~/.dennis_tape_monitor_mock.json")

def send_mock_open(ticker="NVDA", direction="CALL", strike="145", exp="2026-10-16", entry=4.50, size=3, gex="悬崖结构 | 主导墙-下 | 路径薄 | 加分"):
    cp_char = "C" if direction.upper() == "CALL" else "P"
    payload = {
        "id": f"mock_open_{int(time.time()*1000)}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "force_toast": True,
        "author": {"username": "AlphaAgent"},
        "embeds": [{
            "type": "rich",
            "title": "T1 OPEN",
            "description": f"{ticker} {cp_char} {strike} {exp}\nEntry {entry:.2f} | {size}x\nGEX {gex}",
            "color": 2278750
        }]
    }
    with open(MOCK_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    print(f"✅ 已注入 T1 OPEN 模拟开仓: {ticker} {strike}{cp_char} Entry ${entry:.2f} {size}x")

def send_mock_close(ticker="NVDA", direction="CALL", strike="145", exp="2026-10-16", exit_p=6.30, pnl_pct="+40.0%", pnl_dol="+$540.00", stop_t="trailing_stop"):
    cp_char = "C" if direction.upper() == "CALL" else "P"
    payload = {
        "id": f"mock_close_{int(time.time()*1000)}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "force_toast": True,
        "author": {"username": "AlphaAgent"},
        "embeds": [{
            "type": "rich",
            "title": "T1 CLOSE",
            "description": f"{ticker} {cp_char} {strike} {exp}\nExit {exit_p:.2f} | {pnl_pct} | {pnl_dol} | {stop_t}",
            "color": 15680580 if pnl_pct.startswith("-") else 2278750
        }]
    }
    with open(MOCK_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    print(f"✅ 已注入 T1 CLOSE 模拟平仓: {ticker} {strike}{cp_char} Exit ${exit_p:.2f} ({pnl_pct} {pnl_dol}) [{stop_t}]")

if __name__ == "__main__":
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "open"
    if mode == "close":
        send_mock_close()
    else:
        send_mock_open()
