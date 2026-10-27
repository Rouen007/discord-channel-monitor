import re
from typing import Dict, Any

def clean_text(text: Any) -> str:
    if not text:
        return ""
    return re.sub(r'\s+', ' ', str(text)).strip()

def format_currency(val: float) -> str:
    if abs(val) >= 1_000_000:
        return f"${val / 1_000_000:.2f}M"
    elif abs(val) >= 1_000:
        return f"${val / 1_000:.2f}K"
    else:
        return f"${val:.2f}"
