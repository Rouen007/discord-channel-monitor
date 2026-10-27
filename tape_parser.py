import re
from typing import Dict, Optional

def format_date_short(date_str: str) -> str:
    """把 2026-10-16 转换为 10/16/26，2027-01-15 转换为 01/15/27"""
    m = re.match(r'20(\d{2})-(\d{2})-(\d{2})', date_str.strip())
    if m:
        return f"{m.group(2)}/{m.group(3)}/{m.group(1)}"
    return date_str.strip()

def parse_tape_signal(content: str = "", embeds: list = None) -> Optional[Dict]:
    """
    专门针对夜航社 Dennis-Tape (channel: 1492437553171660921) 的策略信号解析器。

    解析对象：
    1. T1/S1 OPEN (开仓信号)：
       Title: T1 OPEN / S1 OPEN
       Line 0: TICKER P/C STRIKE EXP_DATE (例: PLTR P 190 2026-10-16)
       Line 1: Entry PRICE | SIZEx (例: Entry 7.25 | 2x)
       Line 2: GEX 结构标签 (例: GEX 悬崖结构 | 主导墙-下 | 路径薄 | 加分)

    2. T1/S1 CLOSE (平仓信号)：
       Title: T1 CLOSE / S1 CLOSE
       Line 0: TICKER P/C STRIKE EXP_DATE (例: META P 755 2026-09-28)
       Line 1: Exit PRICE | PNL_PCT | PNL_DOLLAR | STOP_TYPE (例: Exit 9.24 | +39.2% | +$780 | trailing_stop)

    非策略信号（纯闲聊、灌水、每日净值汇总等）直接返回 None 丢弃。
    """
    embeds = embeds or []
    full_text = content or ""

    strategy = ""
    action = ""  # OPEN / CLOSE
    desc_text = ""

    # 优先解析 Embed
    for emb in embeds:
        title = (emb.get("title") or "").strip()
        desc = (emb.get("description") or "").strip()
        m_title = re.search(r'\b([A-Z]\d+)\s+(OPEN|CLOSE)\b', title, re.IGNORECASE)
        if m_title:
            strategy = m_title.group(1).upper()
            action = m_title.group(2).upper()
            desc_text = desc
            break

    # 如果 Embed 没匹配到，尝试在 content 里寻找
    if not action:
        m_title = re.search(r'\b([A-Z]\d+)\s+(OPEN|CLOSE)\b', full_text, re.IGNORECASE)
        if m_title:
            strategy = m_title.group(1).upper()
            action = m_title.group(2).upper()
            desc_text = full_text

    if not action or not desc_text:
        return None

    lines = [l.strip() for l in desc_text.split("\n") if l.strip()]
    if not lines:
        return None

    line0 = lines[0]
    # Line 0: TICKER P/C STRIKE EXP_DATE (支持 190 或 47.50，日期 YYYY-MM-DD)
    # 也兼容 STRIKE P/C 的顺序
    m_contract1 = re.search(r'([A-Z]{1,6})\s+([CP])\s+(\d+(?:\.\d+)?)\s+(\d{4}-\d{2}-\d{2})', line0)
    m_contract2 = re.search(r'([A-Z]{1,6})\s+(\d+(?:\.\d+)?)\s+([CP])\s+(\d{4}-\d{2}-\d{2})', line0)

    if m_contract1:
        ticker = m_contract1.group(1)
        cp_raw = m_contract1.group(2).upper()
        strike = m_contract1.group(3)
        exp_date = m_contract1.group(4)
    elif m_contract2:
        ticker = m_contract2.group(1)
        strike = m_contract2.group(2)
        cp_raw = m_contract2.group(3).upper()
        exp_date = m_contract2.group(4)
    else:
        return None

    direction = "CALL" if cp_raw == "C" else "PUT"
    exp_short = format_date_short(exp_date)
    # 清理末尾无用 .0，保持紧凑，如 47.5 或 190
    strike_fmt = strike.rstrip('0').rstrip('.') if '.' in strike else strike
    contract = f"{exp_short} {strike_fmt}{cp_raw}"

    res = {
        "strategy": strategy,
        "action": action,        # "OPEN" 或 "CLOSE"
        "ticker": ticker,
        "direction": direction,  # "CALL" 或 "PUT"
        "strike": strike_fmt,
        "expiration": exp_short,
        "contract": contract,
        "entry_price": None,
        "exit_price": None,
        "size": None,
        "premium_est": None,
        "pnl_pct": None,
        "pnl_dollar": None,
        "stop_type": None,
        "gex_tags": [],
        "raw_lines": lines
    }

    if action == "OPEN":
        # OPEN 信号解析
        # Line 1: Entry 7.25 | 2x
        if len(lines) > 1:
            line1 = lines[1]
            m_entry = re.search(r'Entry\s+(\d+(?:\.\d+)?)(?:\s*\|\s*(\d+)x)?', line1, re.IGNORECASE)
            if m_entry:
                entry_p = float(m_entry.group(1))
                res["entry_price"] = f"${entry_p:.2f}"
                size_x = int(m_entry.group(2)) if m_entry.group(2) else 1
                res["size"] = f"{size_x}x"
                # 估算总权利金成本: 价格 * 张数 * 100
                total_usd = entry_p * size_x * 100
                if total_usd >= 1000000:
                    res["premium_est"] = f"${total_usd / 1000000:.2f}M"
                elif total_usd >= 1000:
                    res["premium_est"] = f"${total_usd / 1000:.2f}K"
                else:
                    res["premium_est"] = f"${int(total_usd)}"

        # Line 2 (可选): GEX 悬崖结构 | 主导墙-下 | 路径薄 | 加分
        if len(lines) > 2:
            line2 = lines[2]
            parts = [p.strip() for p in line2.split("|") if p.strip()]
            gex_tags = []
            for p in parts:
                p_clean = re.sub(r'^GEX\s*', '', p).strip()
                if p_clean:
                    gex_tags.append(p_clean)
            res["gex_tags"] = gex_tags

    elif action == "CLOSE":
        # CLOSE 信号解析
        # Line 1: Exit 6.88 | -22.1% | -$391.08 | hard_stop
        # 或: Exit 9.24 | +39.2% | +$780 | trailing_stop
        if len(lines) > 1:
            line1 = lines[1]
            m_close = re.search(
                r'Exit\s+(\d+(?:\.\d+)?)\s*\|\s*([+-]?\d+(?:\.\d+)?%)\s*\|\s*([+-]?\$?[\d,]+(?:\.\d+)?)\s*\|\s*([a-zA-Z0-9_]+)',
                line1, re.IGNORECASE
            )
            if m_close:
                exit_p = float(m_close.group(1))
                res["exit_price"] = f"${exit_p:.2f}"
                res["pnl_pct"] = m_close.group(2)
                pnl_dol = m_close.group(3)
                if not pnl_dol.startswith("$") and not pnl_dol.startswith("-$") and not pnl_dol.startswith("+$"):
                    pnl_dol = pnl_dol.replace("-", "-$").replace("+", "+$")
                res["pnl_dollar"] = pnl_dol
                res["stop_type"] = m_close.group(4)
            else:
                # 兼容简写或不带部分字段的 Exit
                m_exit = re.search(r'Exit\s+(\d+(?:\.\d+)?)', line1, re.IGNORECASE)
                if m_exit:
                    res["exit_price"] = f"${float(m_exit.group(1)):.2f}"
                m_pnl = re.search(r'([+-]?\d+(?:\.\d+)?%)', line1)
                if m_pnl:
                    res["pnl_pct"] = m_pnl.group(1)
                m_dol = re.search(r'([+-]?\$[\d,]+(?:\.\d+)?)', line1)
                if m_dol:
                    res["pnl_dollar"] = m_dol.group(1)

    return res
