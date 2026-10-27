import sys
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from PyQt6.QtCore import Qt, QPoint, QRect, QRectF, pyqtSignal, QObject, QTimer, QPropertyAnimation, QEvent, QUrl
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QScrollArea, QFrame, QPushButton, QLineEdit, QDialog,
    QFormLayout, QGraphicsDropShadowEffect, QSlider, QCheckBox,
    QSizeGrip, QGridLayout, QSizePolicy, QComboBox
)
from PyQt6.QtGui import QColor, QFont, QCursor, QPixmap, QPainter, QPainterPath, QPen, QDesktopServices

def get_asset_path(filename: str) -> str:
    if getattr(sys, 'frozen', False):
        if hasattr(sys, '_MEIPASS'):
            p = os.path.join(sys._MEIPASS, "assets", filename)
            if os.path.exists(p):
                return p
        p2 = os.path.join(os.path.dirname(sys.executable), "assets", filename)
        if os.path.exists(p2):
            return p2
        p3 = os.path.join(os.path.dirname(sys.executable), "..", "Resources", "assets", filename)
        if os.path.exists(p3):
            return p3
    base = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(base, "assets", filename)
    if os.path.exists(p):
        return p
    return ""

try:
    import objc
    from ctypes import c_void_p
    from Cocoa import (
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowCollectionBehaviorMoveToActiveSpace,
        NSScreenSaverWindowLevel,
        NSStatusWindowLevel
    )
    HAS_COCOA = True
except Exception:
    HAS_COCOA = False

from discord_listener import DiscordGatewayClient, get_default_discord_token
from tape_parser import parse_tape_signal
from market_timer import is_market_monitoring_time, get_market_status_text, recent_trading_days
from history_store import HistorySyncStopped, HistoryStore, in_history_window, discord_page_fetcher

CONFIG_PATH = os.path.expanduser("~/.dennis_tape_monitor_config.json")
MOCK_TRIGGER_PATH = os.path.expanduser("~/.dennis_tape_monitor_mock.json")
HISTORY_CACHE_PATH = os.path.expanduser("~/.dennis_tape_history_cache.json")
HISTORY_DB_PATH = os.path.expanduser("~/.local/state/dennis-tape-monitor/history.sqlite3")
NY_TZ = ZoneInfo("America/New_York")
BJ_TZ = ZoneInfo("Asia/Shanghai")

def parse_channel_ids(value) -> list:
    """Normalize a channel group entered as IDs separated by spaces or commas."""
    if isinstance(value, (list, tuple, set)):
        parts = value
    else:
        parts = re.split(r'[,|;\s]+', str(value or ''))
    ids = []
    seen = set()
    for part in parts:
        channel_id = str(part).strip()
        if channel_id.isdigit() and channel_id not in seen:
            ids.append(channel_id)
            seen.add(channel_id)
    return ids

def message_matches_keywords(message: dict, keywords: list) -> bool:
    if not keywords:
        return True
    parts = [str(message.get("content") or "")]
    for embed in message.get("embeds") or []:
        if isinstance(embed, dict):
            parts.extend([str(embed.get("title") or ""), str(embed.get("description") or "")])
            for field in embed.get("fields") or []:
                if isinstance(field, dict):
                    parts.extend([str(field.get("name") or ""), str(field.get("value") or "")])
    for attachment in message.get("attachments") or []:
        if isinstance(attachment, dict):
            parts.append(str(attachment.get("filename") or ""))
    text = "\n".join(parts).upper()
    return any(keyword.upper() in text for keyword in keywords)

def custom_message_toast(message: dict) -> dict:
    author = message.get("author") or {}
    author_name = (author.get("global_name") or author.get("username") or "未知作者") if isinstance(author, dict) else str(author)
    content = str(message.get("content") or "").strip()
    if not content:
        for embed in message.get("embeds") or []:
            if isinstance(embed, dict):
                content = str(embed.get("description") or embed.get("title") or "").strip()
                if content:
                    break
    return {
        "source": "CUSTOM",
        "channel_id": str(message.get("channel_id", "")),
        "channel_name": f"频道 {message.get('channel_id', '')}",
        "author_name": author_name,
        "content": content or ("发送了附件" if message.get("attachments") else "（无正文）"),
        "attachments": message.get("attachments") or [],
        "timestamp": message.get("timestamp"),
    }

def format_card_timestamps(ts_val) -> dict:
    dt = None
    if ts_val:
        ts_str = str(ts_val).strip()
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            try:
                fmt = "%m-%d %H:%M:%S"
                now_yr = datetime.now().year
                dt_parsed = datetime.strptime(f"{now_yr}-{ts_str}", f"%Y-{fmt}")
                dt = dt_parsed.replace(tzinfo=NY_TZ)
            except Exception:
                dt = None
    if dt is None:
        dt = datetime.now(timezone.utc)

    bj_dt = dt.astimezone(BJ_TZ)
    ny_dt = dt.astimezone(NY_TZ)

    return {
        "bj_compact": bj_dt.strftime("%H:%M:%S"),
        "bj_full": bj_dt.strftime("%m-%d %H:%M:%S"),
        "ny_compact": ny_dt.strftime("%H:%M:%S"),
        "ny_full": ny_dt.strftime("%m-%d %H:%M:%S")
    }

def load_config() -> dict:
    default_token = get_default_discord_token()
    cfg = {
        "token": default_token,
        "channel_id": "",
        "channel_ids": "",
        "source_filter": "ALL",
        "stance_filter": "ALL",
        "filter_keyword": "",
        "strategy_filter": "ALL",
        "only_market_hours": True,
        "opacity": 0.92,
        "theme": "dark",
        "dock_edge": "left",
        "fixed_expanded": False,
        "width": 380,
        "height": 620
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
                cfg.update(user_cfg)
                if "channel_ids" not in user_cfg and user_cfg.get("additional_channels"):
                    cfg["channel_ids"] = user_cfg["additional_channels"]
                if cfg.get("source_filter") not in {"ALL", "DENNIS", "CUSTOM"}:
                    cfg["source_filter"] = "ALL"
                if not cfg.get("token") and default_token:
                    cfg["token"] = default_token
        except Exception:
            pass
    return cfg

def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


class Bridge(QObject):
    new_signal = pyqtSignal(dict)
    status_update = pyqtSignal(str)
    connection_status = pyqtSignal(int, str)
    history_ready = pyqtSignal(int, int, bool, str)


class SettingsDialog(QDialog):
    def __init__(self, parent=None, config=None):
        super().__init__(parent)
        self.setWindowTitle("Discord 频道监控配置")
        self.setFixedWidth(460)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Dialog)
        self.setModal(True)
        self.config = config or {}

        is_dark = self.config.get("theme", "dark") == "dark"
        bg = "#0B0F17" if is_dark else "#F8FAFC"
        text_c = "#E2E8F0" if is_dark else "#0F172A"
        input_bg = "#161E2E" if is_dark else "#FFFFFF"
        border_c = "#1E293B" if is_dark else "#E2E8F0"

        self.setStyleSheet(f"""
            QDialog {{
                background-color: {bg};
                color: {text_c};
                font-family: 'SF Pro Text', sans-serif;
            }}
            QLabel {{
                color: {text_c};
                font-size: 12px;
                font-weight: 500;
            }}
            QLineEdit {{
                background-color: {input_bg};
                color: {text_c};
                border: 1px solid {border_c};
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 12px;
            }}
            QLineEdit:focus {{
                border: 1px solid #00F0FF;
            }}
            QCheckBox {{
                color: {text_c};
                font-size: 12px;
            }}
            QSlider::groove:horizontal {{
                border: none;
                height: 4px;
                background: {border_c};
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: #00F0FF;
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: #FFFFFF;
                border: 1px solid #CBD5E1;
                width: 14px;
                margin-top: -5px;
                margin-bottom: -5px;
                border-radius: 7px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        form = QFormLayout()
        form.setSpacing(12)

        self.token_input = QLineEdit(self.config.get("token", ""))
        self.token_input.setPlaceholderText("Discord User Token (需星河上宾权限)")
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)

        self.channel_input = QLineEdit(self.config.get("channel_id", ""))
        self.channel_input.setPlaceholderText("专用解析频道 ID (可留空)")

        channel_ids = self.config.get("channel_ids") or self.config.get("additional_channels", "")
        self.channel_ids_input = QLineEdit(str(channel_ids))
        self.channel_ids_input.setPlaceholderText("Discord 频道 ID，逗号、空格或分号分隔")

        self.filter_input = QLineEdit(self.config.get("filter_keyword", ""))
        self.filter_input.setPlaceholderText("标的或关键词 (如 PLTR, META；留空显示全部)")

        self.market_hours_check = QCheckBox("仅交易时段监控（盘前5分至盘后15分，其余休眠）")
        self.market_hours_check.setChecked(self.config.get("only_market_hours", True))

        self.theme_check = QCheckBox("深色奇犽主题 (取消勾选即切换为明亮银白主题)")
        self.theme_check.setChecked(self.config.get("theme", "dark") == "dark")

        self.fixed_expanded_check = QCheckBox("固定展开常驻（不自动收起为贴边条）")
        self.fixed_expanded_check.setChecked(self.config.get("fixed_expanded", False))

        current_op = int(self.config.get("opacity", 0.92) * 100)
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(20, 100)
        self.opacity_slider.setValue(current_op)
        self.opacity_val_lbl = QLabel(f"{current_op}%")
        self.opacity_val_lbl.setStyleSheet("color: #00F0FF; font-weight: bold;")
        self.opacity_slider.valueChanged.connect(lambda v: self.opacity_val_lbl.setText(f"{v}%"))

        op_layout = QHBoxLayout()
        op_layout.addWidget(self.opacity_slider)
        op_layout.addWidget(self.opacity_val_lbl)

        self.dock_combo = QComboBox()
        self.dock_combo.addItem("屏幕左侧 (与右侧期权大单呼应)", "left")
        self.dock_combo.addItem("屏幕右侧", "right")
        cur_dock = self.config.get("dock_edge", "left")
        self.dock_combo.setCurrentIndex(0 if cur_dock == "left" else 1)
        self.dock_combo.setStyleSheet(f"""
            QComboBox {{
                background-color: {input_bg};
                color: {text_c};
                border: 1px solid {border_c};
                border-radius: 6px;
                padding: 4px 8px;
                font-size: 12px;
            }}
            QComboBox QAbstractItemView {{
                background-color: {input_bg};
                color: {text_c};
                selection-background-color: #00F0FF;
                selection-color: #000000;
            }}
        """)

        form.addRow("Discord Token:", self.token_input)
        form.addRow("专用解析频道 ID:", self.channel_input)
        form.addRow("频道组 ID:", self.channel_ids_input)
        form.addRow("标的/关键词:", self.filter_input)
        form.addRow("停靠边缘:", self.dock_combo)
        form.addRow("时段策略:", self.market_hours_check)
        form.addRow("黑白模式:", self.theme_check)
        form.addRow("展开模式:", self.fixed_expanded_check)
        form.addRow("窗口透明度:", op_layout)

        layout.addLayout(form)

        hint = QLabel("💡 提示：所有频道共用监听连接。Dennis 频道使用专用解析；频道组按原文消息流展示，可填写多个 ID。")
        hint.setStyleSheet("color: #64748B; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        btn_box = QHBoxLayout()
        btn_box.setSpacing(10)
        save_btn = QPushButton("保存生效")
        save_btn.setStyleSheet("""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #00F0FF, stop:1 #0284C7);
                color: #000000;
                padding: 6px 18px;
                border-radius: 6px;
                font-weight: bold;
                border: none;
            }}
            QPushButton:hover {{
                background: #00F0FF;
            }}
        """)
        save_btn.clicked.connect(self.accept)

        cancel_btn = QPushButton("取消")
        cancel_btn.setStyleSheet("""
            QPushButton {{
                background: #1E222B;
                color: #94A3B8;
                padding: 6px 14px;
                border-radius: 6px;
                border: none;
            }}
            QPushButton:hover {{
                background: #2D3748;
                color: #E2E8F0;
            }}
        """)
        cancel_btn.clicked.connect(self.reject)

        btn_box.addStretch()
        btn_box.addWidget(cancel_btn)
        btn_box.addWidget(save_btn)
        layout.addLayout(btn_box)

    def get_settings(self) -> dict:
        return {
            "token": self.token_input.text().strip(),
            "channel_id": self.channel_input.text().strip(),
            "channel_ids": self.channel_ids_input.text().strip(),
            # Keep writing the legacy key so an older installed build retains its channel group.
            "additional_channels": self.channel_ids_input.text().strip(),
            "filter_keyword": self.filter_input.text().strip(),
            "dock_edge": self.dock_combo.currentData(),
            "only_market_hours": self.market_hours_check.isChecked(),
            "theme": "dark" if self.theme_check.isChecked() else "light",
            "fixed_expanded": self.fixed_expanded_check.isChecked(),
            "opacity": self.opacity_slider.value() / 100.0
        }

    def center_on_screen(self):
        screen = self.screen() or (self.parent().screen() if self.parent() else QApplication.primaryScreen())
        if screen:
            geo = screen.availableGeometry()
            x = geo.center().x() - self.width() // 2
            y = geo.center().y() - self.height() // 2
            self.move(x, y)

    def showEvent(self, event):
        super().showEvent(event)
        self.center_on_screen()
        if HAS_COCOA:
            def _apply():
                try:
                    win_id = int(self.winId())
                    ns_view = objc.objc_object(c_void_p=win_id)
                    ns_window = ns_view.window()
                    if ns_window:
                        behavior = (
                            NSWindowCollectionBehaviorCanJoinAllSpaces |
                            NSWindowCollectionBehaviorFullScreenAuxiliary
                        )
                        ns_window.setCollectionBehavior_(behavior)
                        ns_window.setLevel_(NSScreenSaverWindowLevel + 10)
                        ns_window.setHidesOnDeactivate_(False)
                        ns_window.orderFrontRegardless()
                        ns_window.makeKeyAndOrderFront_(None)
                except Exception:
                    pass
            QTimer.singleShot(0, _apply)
            QTimer.singleShot(40, _apply)


class KilluaBackgroundCard(QFrame):
    """奇犽主题暗黑/明亮专属背景卡片（实心深邃底色，不透出桌面杂物）"""
    def __init__(self, parent=None, theme="dark"):
        super().__init__(parent)
        self.setObjectName("MainCard")
        self.theme = theme
        self._dark_pixmap = None
        self._light_pixmap = None
        self.setStyleSheet("QFrame#MainCard { background: transparent; border: none; }")
        self._load_images()

    def _load_images(self):
        dark_file = get_asset_path("killua_dark.jpg")
        light_file = get_asset_path("killua_light.jpg")
        if dark_file and os.path.exists(dark_file):
            self._dark_pixmap = QPixmap(dark_file)
        if light_file and os.path.exists(light_file):
            self._light_pixmap = QPixmap(light_file)

    def set_theme(self, theme: str):
        self.theme = theme
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        rect = QRectF(self.rect())
        radius = 12.0
        clip_path = QPainterPath()
        clip_path.addRoundedRect(rect, radius, radius)
        painter.setClipPath(clip_path)

        if self.theme == "light":
            painter.fillRect(rect, QColor("#F8FAFC"))
            pixmap = self._light_pixmap
            img_opacity = 0.08
            border_color = QColor(226, 232, 240)
        else:
            # 纯实心深空暗黑，杜绝底层窗口文字透出
            painter.fillRect(rect, QColor("#0A0D14"))
            pixmap = self._dark_pixmap
            img_opacity = 0.06
            border_color = QColor(255, 255, 255, 22)

        if pixmap and not pixmap.isNull():
            painter.save()
            painter.setOpacity(img_opacity)
            scaled = pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            x = (self.width() - scaled.width()) / 2
            y = (self.height() - scaled.height()) / 2
            painter.drawPixmap(int(x), int(y), scaled)
            painter.restore()

        painter.setClipping(False)
        pen = QPen(border_color, 1.0)
        painter.setPen(pen)
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)


def format_compact_contract(contract_str: str) -> str:
    """把 10/16/26 190P 转换为更紧凑的 10/16 190P"""
    if not contract_str:
        return ""
    return re.sub(r'(\d{2}/\d{2})/\d{2}\s+', r'\1 ', contract_str.strip())


def format_compact_duration(t1_str: str, t2_str: str) -> str:
    """计算紧凑历时，如: 3h49m 或 45m"""
    if not t1_str or not t2_str:
        return ""
    try:
        fmt = "%m-%d %H:%M:%S"
        dt1 = datetime.strptime(f"2026-{t1_str.strip()}", f"%Y-{fmt}")
        dt2 = datetime.strptime(f"2026-{t2_str.strip()}", f"%Y-{fmt}")
        diff = int((dt2 - dt1).total_seconds())
        if diff < 0:
            diff = abs(diff)
        mins = diff // 60
        hours = mins // 60
        rem_mins = mins % 60
        if hours > 0:
            return f"{hours}h{rem_mins}m"
        elif mins > 0:
            return f"{mins}m"
        else:
            return f"{diff}s"
    except Exception:
        return ""


class DennisTradeCard(QFrame):
    """
    Dennis-Tape 标的交易生命周期聚合卡片（极简金融级无截断排版）：
    - 头部前面醒目标注：【🎯 获利 +XX%】 / 【🛡️ 止损 -XX%】 / 【🟢 持仓中】
    - 一行紧凑展示【买入】与【卖出】核心数据，去除冗余字眼，拒绝截断
    - 展示合约规格、入场/出场价、收益金额、仓位以及 GEX 结构特征
    - 收到对应平仓信号时无缝原位更新并置顶
    """
    def __init__(self, ticker: str, contract: str, direction: str, strategy: str = "T1", parent=None, theme="dark"):
        super().__init__(parent)
        self.setObjectName("DennisTradeCard")
        self.source = "DENNIS"
        self.ticker = ticker
        self.contract = contract
        self.direction = direction
        self.strategy = strategy
        self.theme = theme
        self.is_collapsed = False

        self.open_data = None
        self.open_time_str = ""

        self.close_data = None
        self.close_time_str = ""

        self.status = "OPEN"
        self.latest_time_str = ""

        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(8, 7, 8, 7)
        self._main_layout.setSpacing(4)
        self.refresh_ui()

    def set_open(self, open_data: dict, time_str: str):
        self.open_data = open_data
        self.open_time_str = time_str
        self.latest_time_str = time_str
        if not self.close_data:
            self.status = "OPEN"
        self.refresh_ui()

    def set_close(self, close_data: dict, time_str: str):
        self.close_data = close_data
        self.close_time_str = time_str
        self.latest_time_str = time_str
        pnl_pct = close_data.get("pnl_pct") or ""
        if pnl_pct.startswith("+"):
            self.status = "PROFIT"
        elif pnl_pct.startswith("-"):
            self.status = "LOSS"
        else:
            self.status = "FLAT"
        self.refresh_ui()

    def refresh_ui(self):
        while self._main_layout.count():
            item = self._main_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                sub = item.layout()
                while sub.count():
                    w = sub.takeAt(0).widget()
                    if w:
                        w.deleteLater()

        is_dark = self.theme == "dark"
        is_call = self.direction == "CALL"

        # 头部首要标识样式
        if self.status == "PROFIT":
            accent_bar = "#10B981"
            pnl_pct = (self.close_data.get("pnl_pct") if self.close_data else "") or ""
            pnl_clean = pnl_pct.split('.')[0] + '%' if '.' in pnl_pct else pnl_pct
            status_text = f"🎯 获利 {pnl_clean}".strip() if pnl_clean else "🎯 获利"
            status_bg = "#064E3B" if is_dark else "#D1FAE5"
            status_color = "#34D399" if is_dark else "#065F46"
            status_border = "#059669" if is_dark else "#A7F3D0"
        elif self.status == "LOSS":
            accent_bar = "#FB7185"
            pnl_pct = (self.close_data.get("pnl_pct") if self.close_data else "") or ""
            pnl_clean = pnl_pct.split('.')[0] + '%' if '.' in pnl_pct else pnl_pct
            status_text = f"🛡️ 止损 {pnl_clean}".strip() if pnl_clean else "🛡️ 止损"
            status_bg = "#881337" if is_dark else "#FFE4E6"
            status_color = "#FB7185" if is_dark else "#BE123C"
            status_border = "#E11D48" if is_dark else "#FECDD3"
        elif self.status == "FLAT":
            accent_bar = "#94A3B8"
            status_text = "⚪ 保本"
            status_bg = "#334155" if is_dark else "#F1F5F9"
            status_color = "#E2E8F0" if is_dark else "#475569"
            status_border = "#64748B" if is_dark else "#CBD5E1"
        else: # OPEN (持仓中)
            accent_bar = "#00F0FF"
            status_text = "🟢 持仓中"
            status_bg = "#0E7490" if is_dark else "#E0F2FE"
            status_color = "#38BDF8" if is_dark else "#0369A1"
            status_border = "#0284C7" if is_dark else "#BAE6FD"

        # 方向色彩
        if is_call:
            dir_color = "#34D399" if is_dark else "#059669"
            dir_bg = "rgba(52, 211, 153, 0.12)" if is_dark else "rgba(5, 150, 105, 0.08)"
            dir_border = "rgba(52, 211, 153, 0.3)" if is_dark else "rgba(5, 150, 105, 0.2)"
        else:
            dir_color = "#FB7185" if is_dark else "#E11D48"
            dir_bg = "rgba(251, 113, 133, 0.12)" if is_dark else "rgba(225, 29, 72, 0.08)"
            dir_border = "rgba(251, 113, 133, 0.3)" if is_dark else "rgba(225, 29, 72, 0.2)"

        if is_dark:
            card_bg = "#121824"
            card_hover = "#182232"
            card_border = "rgba(255, 255, 255, 0.06)"
            card_border_hover = "rgba(255, 255, 255, 0.14)"
            tick_color = "#FFFFFF"
            contract_color = "#F1F5F9"
            label_muted = "#94A3B8"
            label_dim = "#64748B"
        else:
            card_bg = "#FFFFFF"
            card_hover = "#F8FAFC"
            card_border = "#E2E8F0"
            card_border_hover = "#CBD5E1"
            tick_color = "#0F172A"
            contract_color = "#1E293B"
            label_muted = "#64748B"
            label_dim = "#94A3B8"

        self.setStyleSheet(f"""
            QFrame#DennisTradeCard {{
                background-color: {card_bg};
                border: 1px solid {card_border};
                border-left: 3px solid {accent_bar};
                border-radius: 6px;
            }}
            QFrame#DennisTradeCard:hover {{
                background-color: {card_hover};
                border: 1px solid {card_border_hover};
                border-left: 3px solid {accent_bar};
            }}
        """)

        # 头部行: [▼] [获利/止损/持仓] [$PLTR] [10/16 190P] [PUT] ······ [3h49m]
        header = QHBoxLayout()
        header.setSpacing(5)

        self.collapse_btn = QPushButton("▶" if self.is_collapsed else "▼")
        self.collapse_btn.setFixedSize(14, 14)
        self.collapse_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.collapse_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
                color: #64748B;
                font-size: 9px;
            }
            QPushButton:hover {
                color: #CBD5E1;
            }
        """)
        self.collapse_btn.clicked.connect(self.toggle_collapse)
        header.addWidget(self.collapse_btn)

        # 1. 前面首要标识: 获利 / 止损 / 持仓中
        status_lbl = QLabel(status_text)
        status_lbl.setStyleSheet(f"""
            background-color: {status_bg};
            color: {status_color};
            font-size: 10px;
            font-weight: 800;
            padding: 1px 5px;
            border-radius: 3px;
            border: 1px solid {status_border};
            font-family: 'SF Pro Display', sans-serif;
        """)
        header.addWidget(status_lbl)

        # 2. 标的代码 $TICKER
        tick_lbl = QLabel(f"${self.ticker}")
        tick_lbl.setStyleSheet(f"""
            color: {tick_color};
            font-size: 13px;
            font-weight: 800;
            font-family: 'SF Pro Display', sans-serif;
        """)
        header.addWidget(tick_lbl)

        # 3. 紧凑合约规格 (如 10/16 190P)
        compact_c = format_compact_contract(self.contract)
        contract_lbl = QLabel(compact_c)
        contract_lbl.setStyleSheet(f"""
            color: {contract_color};
            font-size: 11px;
            font-weight: 700;
            font-family: Menlo, Monaco, monospace;
        """)
        header.addWidget(contract_lbl)

        # 4. CALL / PUT 方向
        dir_lbl = QLabel(self.direction)
        dir_lbl.setStyleSheet(f"""
            background-color: {dir_bg};
            color: {dir_color};
            font-size: 9px;
            font-weight: 700;
            padding: 1px 3px;
            border-radius: 3px;
            border: 1px solid {dir_border};
            font-family: sans-serif;
        """)
        header.addWidget(dir_lbl)

        header.addStretch()

        # 5. 右侧紧凑时长标签 (如 3h49m 或 45m)
        dur = format_compact_duration(self.open_time_str, self.close_time_str)
        if dur:
            dur_lbl = QLabel(dur)
            dur_lbl.setStyleSheet(f"""
                color: {label_dim};
                font-size: 9px;
                font-family: Menlo, Monaco, monospace;
                background: {'rgba(255, 255, 255, 0.04)' if is_dark else 'rgba(0, 0, 0, 0.04)'};
                padding: 1px 4px;
                border-radius: 2px;
                border: 1px solid {'rgba(255, 255, 255, 0.06)' if is_dark else 'rgba(0, 0, 0, 0.06)'};
            """)
            header.addWidget(dur_lbl)

        self._main_layout.addLayout(header)

        # 详情容器（支持折叠）
        self.details_widget = QWidget()
        details_layout = QVBoxLayout(self.details_widget)
        details_layout.setContentsMargins(18, 1, 0, 1)
        details_layout.setSpacing(3)

        # 第一行: 【买入】紧凑参数行
        row_buy = QHBoxLayout()
        row_buy.setSpacing(6)
        if self.open_data:
            entry_p = self.open_data.get("entry_price") or ""
            size = self.open_data.get("size") or ""
            prem = self.open_data.get("premium_est") or ""
            prem_s = f" ({prem})" if prem else ""
            buy_text = f"买入 {self.open_time_str} · 入场 {entry_p} · {size}{prem_s}"
        else:
            buy_text = "买入 历史开仓记录"

        buy_lbl = QLabel(buy_text)
        buy_lbl.setStyleSheet(f"""
            color: {'#CBD5E1' if is_dark else '#334155'};
            font-size: 11px;
            font-weight: 500;
            font-family: Menlo, Monaco, monospace;
        """)
        row_buy.addWidget(buy_lbl)
        row_buy.addStretch()
        details_layout.addLayout(row_buy)

        # 第二行: 【卖出】紧凑参数行
        row_sell = QHBoxLayout()
        row_sell.setSpacing(6)
        if self.close_data:
            exit_p = self.close_data.get("exit_price") or ""
            pnl_pct = self.close_data.get("pnl_pct") or ""
            pnl_dol = self.close_data.get("pnl_dollar") or ""
            stop_t = self.close_data.get("stop_type") or ""
            stop_map = {
                "trailing_stop": "移动止盈",
                "hard_stop": "硬止损",
                "profit_target": "目标止盈"
            }
            stop_name = stop_map.get(stop_t, "")
            stop_suffix = f" ({stop_name})" if stop_name else ""
            pnl_clean = pnl_pct.split('.')[0] + '%' if '.' in pnl_pct else pnl_pct
            sell_text = f"卖出 {self.close_time_str} · 出场 {exit_p} · {pnl_clean} {pnl_dol}{stop_suffix}"
            sell_color = "#34D399" if pnl_pct.startswith("+") else ("#FB7185" if pnl_pct.startswith("-") else label_muted)
        else:
            sell_text = "卖出 待平仓 (持仓监控中)"
            sell_color = "#38BDF8" if is_dark else "#0284C7"

        sell_lbl = QLabel(sell_text)
        sell_lbl.setStyleSheet(f"""
            color: {sell_color};
            font-size: 11px;
            font-weight: 600;
            font-family: Menlo, Monaco, monospace;
        """)
        row_sell.addWidget(sell_lbl)
        row_sell.addStretch()
        details_layout.addLayout(row_sell)

        # 第三行: GEX 结构特征胶囊 (仅当有 GEX 时展示)
        gex_tags = (self.open_data.get("gex_tags") if self.open_data else []) or []
        if gex_tags:
            row_gex = QHBoxLayout()
            row_gex.setSpacing(4)
            gex_prefix = QLabel("GEX:")
            gex_prefix.setStyleSheet(f"color: {label_dim}; font-size: 9px; font-weight: 600;")
            row_gex.addWidget(gex_prefix)
            for tag in gex_tags:
                pill = QLabel(tag)
                pill.setStyleSheet(f"""
                    background: {'rgba(255, 255, 255, 0.04)' if is_dark else 'rgba(0, 0, 0, 0.04)'};
                    color: {label_muted};
                    border: 1px solid {'rgba(255, 255, 255, 0.07)' if is_dark else 'rgba(0, 0, 0, 0.07)'};
                    border-radius: 3px;
                    font-size: 9px;
                    padding: 1px 4px;
                    font-family: sans-serif;
                """)
                row_gex.addWidget(pill)
            row_gex.addStretch()
            details_layout.addLayout(row_gex)

        self.details_widget.setVisible(not self.is_collapsed)
        self._main_layout.addWidget(self.details_widget)

    def toggle_collapse(self):
        self.is_collapsed = not self.is_collapsed
        self.details_widget.setVisible(not self.is_collapsed)
        self.collapse_btn.setText("▶" if self.is_collapsed else "▼")

    def set_theme(self, theme: str):
        self.theme = theme
        self.refresh_ui()


# 兼容别名
DennisSignalCard = DennisTradeCard



class DiscordMessageCard(QFrame):
    """Unclassified message card for user-configured Discord channel groups."""
    def __init__(self, message: dict, parent=None, theme="dark"):
        super().__init__(parent)
        self.setObjectName("DiscordMessageCard")
        self.source = "CUSTOM"
        self.message = dict(message)
        self.channel_id = str(self.message.get("channel_id", ""))
        self.theme = theme
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(8, 7, 8, 7)
        self._layout.setSpacing(5)
        self.refresh_ui()

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    def refresh_ui(self):
        self._clear_layout(self._layout)
        is_dark = self.theme == "dark"
        text_color = "#E2E8F0" if is_dark else "#0F172A"
        muted_color = "#64748B" if is_dark else "#64748B"
        border = "rgba(56, 189, 248, 0.34)" if is_dark else "rgba(2, 132, 199, 0.32)"
        background = "rgba(56, 189, 248, 0.045)" if is_dark else "rgba(2, 132, 199, 0.035)"
        self.setStyleSheet(f"""
            QFrame#DiscordMessageCard {{
                background-color: {background};
                border: 1px solid {border};
                border-left: 3px solid #38BDF8;
                border-radius: 6px;
            }}
        """)

        author = self.message.get("author") or {}
        author_name = (author.get("global_name") or author.get("username") or "未知作者") if isinstance(author, dict) else str(author)
        ts = format_card_timestamps(self.message.get("timestamp"))
        header = QHBoxLayout()
        badge = QLabel("📡 频道组")
        badge.setStyleSheet("color: #38BDF8; font-size: 9px; font-weight: 800;")
        header.addWidget(badge)
        channel = QLabel(f"频道 {self.channel_id}")
        channel.setTextFormat(Qt.TextFormat.PlainText)
        channel.setToolTip(f"Discord Channel ID: {self.channel_id}")
        channel.setStyleSheet(f"color: {muted_color}; font-size: 9px; font-family: Menlo, monospace;")
        header.addWidget(channel)
        header.addWidget(QLabel("·"))
        author_label = QLabel(author_name)
        author_label.setTextFormat(Qt.TextFormat.PlainText)
        author_label.setStyleSheet(f"color: {text_color}; font-size: 10px; font-weight: 700;")
        header.addWidget(author_label)
        header.addStretch()
        time_label = QLabel(f"🕒 {ts['bj_compact']}")
        time_label.setToolTip(f"北京时间 {ts['bj_full']} · 美东 {ts['ny_full']}")
        time_label.setStyleSheet(f"color: {muted_color}; font-size: 9px; font-family: Menlo, monospace;")
        header.addWidget(time_label)
        self._layout.addLayout(header)

        text_parts = []
        content = str(self.message.get("content") or "").strip()
        if content:
            text_parts.append(content)
        for embed in self.message.get("embeds") or []:
            if not isinstance(embed, dict):
                continue
            title = str(embed.get("title") or "").strip()
            description = str(embed.get("description") or "").strip()
            fields = embed.get("fields") or []
            field_text = []
            for field in fields:
                if isinstance(field, dict):
                    name = str(field.get("name") or "").strip()
                    value = str(field.get("value") or "").strip()
                    if name or value:
                        field_text.append(f"{name}: {value}" if name else value)
            embed_text = "\n".join(part for part in [title, description, *field_text] if part)
            if embed_text and embed_text not in text_parts:
                text_parts.append(embed_text)
        if not text_parts:
            text_parts.append("（无正文）" if not self.message.get("attachments") else "发送了附件")

        body = QLabel("\n\n".join(text_parts))
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setStyleSheet(f"color: {text_color}; font-size: 11px; line-height: 1.35;")
        self._layout.addWidget(body)

        for attachment in self.message.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            url = attachment.get("url") or attachment.get("proxy_url")
            filename = str(attachment.get("filename") or "Discord 附件")
            if url:
                link = QPushButton(f"📎 {filename}")
                link.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                link.setToolTip("在浏览器中打开 Discord 附件")
                link.setStyleSheet(f"text-align: left; color: {'#38BDF8' if is_dark else '#0369A1'}; background: transparent; border: none; font-size: 10px;")
                link.clicked.connect(lambda _, target=url: QDesktopServices.openUrl(QUrl(target)))
                self._layout.addWidget(link)

    def set_theme(self, theme: str):
        self.theme = theme
        self.refresh_ui()


class SignalToastPopup(QWidget):
    """
    Discord 新消息桌面 HUD Toast 弹窗：
    - 支持频道组原文消息及 Dennis 专用解析消息
    - 位于折叠贴边条旁（左停靠时在左侧，右停靠时在右侧）
    - 拥有全屏穿透能力 (NSPanel + Layer 27+)
    - 点击直接平滑展开主面板
    - 15 秒后自动关闭，悬停暂停倒计时，移开后保留 5 秒
    """
    def __init__(self, parent_window, parsed: dict, time_str: str):
        super().__init__()
        self.parent_window = parent_window
        self.parsed = parsed
        self.time_str = time_str
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        is_dark = getattr(parent_window, "theme", "dark") == "dark"
        strategy = parsed.get("strategy", "T1")
        is_custom = parsed.get("source") == "CUSTOM"
        card_bg = "#0D1117" if is_dark else "#FFFFFF"
        text_white = "#FFFFFF" if is_dark else "#0F172A"

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        card = QFrame(self)

        if is_custom:
            glow_color = "#38BDF8"
            channel_name = parsed.get("channel_name") or f"频道 {parsed.get('channel_id', '')}"
            author_name = parsed.get("author_name") or "未知作者"
            snippet = (parsed.get("content") or "").replace("\n", " ").strip()
            if not snippet and parsed.get("attachments"):
                snippet = "发送了附件"
            if len(snippet) > 58:
                snippet = snippet[:58] + "..."
            ts = format_card_timestamps(parsed.get("timestamp") or time_str)
            card.setStyleSheet(f"QFrame {{ background-color: {card_bg}; border: 1.5px solid {glow_color}; border-radius: 8px; }}")
            c_layout = QVBoxLayout(card)
            c_layout.setContentsMargins(12, 8, 12, 8)
            c_layout.setSpacing(4)
            row1 = QHBoxLayout()
            title = QLabel(f"📡 频道消息 · {channel_name}")
            title.setStyleSheet(f"color: {glow_color}; font-size: 11px; font-weight: 800;")
            row1.addWidget(title)
            row1.addStretch()
            time_label = QLabel(f"🕒 {ts['bj_compact']}")
            time_label.setStyleSheet("color: #64748B; font-size: 10px; font-family: Menlo, monospace;")
            row1.addWidget(time_label)
            c_layout.addLayout(row1)
            row2 = QHBoxLayout()
            author_label = QLabel(author_name)
            author_label.setTextFormat(Qt.TextFormat.PlainText)
            author_label.setStyleSheet(f"color: {'#CBD5E1' if is_dark else '#334155'}; font-size: 10px; font-weight: 700;")
            row2.addWidget(author_label)
            preview = QLabel(snippet)
            preview.setTextFormat(Qt.TextFormat.PlainText)
            preview.setStyleSheet(f"color: {'#CBD5E1' if is_dark else '#334155'}; font-size: 10px;")
            row2.addWidget(preview)
            row2.addStretch()
            c_layout.addLayout(row2)
        else:
            action = parsed.get("action", "OPEN")
            ticker = parsed.get("ticker", "TICKER")
            direction = parsed.get("direction", "CALL")
            contract = parsed.get("contract", "")
            pnl_pct = parsed.get("pnl_pct") or ""

            is_open = action == "OPEN"
            is_call = direction == "CALL"

            if is_open:
                glow_color = "#10B981"
                title_text = f"⚡ {strategy} 买入开仓"
                time_label = f"买入 {time_str.split(' ')[-1] if ' ' in time_str else time_str}"
            else:
                is_profit = pnl_pct.startswith("+")
                if is_profit:
                    glow_color = "#10B981"
                    title_text = f"🎯 获利了结 ({pnl_pct})"
                else:
                    glow_color = "#FB7185"
                    title_text = f"🛡️ 止损出局 ({pnl_pct})"
                time_label = f"卖出 {time_str.split(' ')[-1] if ' ' in time_str else time_str}"

            dir_color = "#34D399" if is_call else "#FB7185"

            card.setStyleSheet(f"""
                QFrame {{
                    background-color: {card_bg};
                    border: 1.5px solid {glow_color};
                    border-radius: 8px;
                }}
            """)
            c_layout = QVBoxLayout(card)
            c_layout.setContentsMargins(12, 8, 12, 8)
            c_layout.setSpacing(4)

            # 行 1: 标题 + 标的 + 方向 + 时间
            row1 = QHBoxLayout()
            row1.setSpacing(6)

            t_lbl = QLabel(title_text)
            t_lbl.setStyleSheet(f"color: {glow_color}; font-size: 11px; font-weight: 800;")
            row1.addWidget(t_lbl)

            tick_lbl = QLabel(f"${ticker}")
            tick_lbl.setStyleSheet(f"color: {text_white}; font-size: 13px; font-weight: 800;")
            row1.addWidget(tick_lbl)

            dir_lbl = QLabel(direction)
            dir_lbl.setStyleSheet(f"color: {dir_color}; font-size: 10px; font-weight: 700;")
            row1.addWidget(dir_lbl)

            row1.addStretch()

            tm_lbl = QLabel(time_label)
            tm_lbl.setStyleSheet("color: #64748B; font-size: 10px; font-family: Menlo, monospace;")
            row1.addWidget(tm_lbl)
            c_layout.addLayout(row1)

            # 行 2: 合约 + 入场/出场信息
            row2 = QHBoxLayout()
            row2.setSpacing(6)

            c_lbl = QLabel(contract)
            c_lbl.setStyleSheet(f"color: {'#E2E8F0' if is_dark else '#1E293B'}; font-size: 11px; font-weight: 700; font-family: Menlo, monospace;")
            row2.addWidget(c_lbl)

            if is_open:
                entry_p = parsed.get("entry_price") or ""
                size = parsed.get("size") or ""
                info_lbl = QLabel(f"入场 {entry_p} · {size}")
                info_lbl.setStyleSheet("color: #CBD5E1; font-size: 11px;")
                row2.addWidget(info_lbl)
            else:
                exit_p = parsed.get("exit_price") or ""
                pnl_dol = parsed.get("pnl_dollar") or ""
                pnl_c = "#34D399" if pnl_pct.startswith("+") else "#FB7185"
                info_lbl = QLabel(f"出场 {exit_p} · {pnl_pct} ({pnl_dol})")
                info_lbl.setStyleSheet(f"color: {pnl_c}; font-size: 11px; font-weight: 700;")
                row2.addWidget(info_lbl)

            row2.addStretch()
            c_layout.addLayout(row2)

        main_layout.addWidget(card)

        # 阴影
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(18)
        shadow.setColor(QColor(0, 0, 0, 180))
        shadow.setOffset(0, 4)
        card.setGraphicsEffect(shadow)

        self.setFixedSize(360, 76)
        self.position_near_parent()
        self.setup_cocoa_level()

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(15000)
        self.timer.timeout.connect(self.close)
        self.timer.start()

    def position_near_parent(self):
        screen = self.parent_window.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else QRect(0, 0, 1440, 900)
        is_left = self.parent_window.is_left_dock()
        if getattr(self.parent_window, "_compact", True):
            if is_left:
                x = geo.left() + 36
            else:
                x = geo.right() - 28 - self.width() - 8
        else:
            if is_left:
                x = min(self.parent_window.x() + self.parent_window.width() + 10, geo.right() - self.width() - 10)
            else:
                x = max(geo.left() + 10, self.parent_window.x() - self.width() - 10)
        y = max(geo.top() + 10, min(self.parent_window.y(), geo.bottom() - self.height() - 10))
        self.move(x, y)

    def setup_cocoa_level(self):
        if not HAS_COCOA:
            return
        def _apply():
            try:
                ns_view = objc.objc_object(c_void_p=int(self.winId()))
                ns_window = ns_view.window()
                if ns_window:
                    behavior = (
                        NSWindowCollectionBehaviorCanJoinAllSpaces |
                        NSWindowCollectionBehaviorFullScreenAuxiliary
                    )
                    ns_window.setCollectionBehavior_(behavior)
                    ns_window.setLevel_(NSScreenSaverWindowLevel + 2)
                    ns_window.setHidesOnDeactivate_(False)
                    ns_window.orderFrontRegardless()
            except Exception:
                pass
        QTimer.singleShot(20, _apply)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if getattr(self.parent_window, "_compact", False):
                self.parent_window.expand_panel()
            else:
                self.parent_window.show()
                self.parent_window.raise_()
                self.parent_window.activateWindow()
            self.close()

    def enterEvent(self, event):
        self.timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.timer.start(5000)
        super().leaveEvent(event)


class DiscordChannelMonitorWindow(QWidget):
    """
    Discord 多频道监听主窗口浮窗
    """
    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.theme = self.config.get("theme", "dark")
        self._unread = 0
        self._compact = False
        self._pinned = self.config.get("fixed_expanded", False)
        self._dragging = False
        self._drag_start_pos = QPoint()
        self._settings_open = False
        self._changing_geometry = False
        self._closing = False
        self.dc_client = None
        self.seen_message_ids = set()
        self._active_toast = None
        self.active_positions = {}
        self.history = HistoryStore(HISTORY_DB_PATH)
        self.history.import_legacy(HISTORY_CACHE_PATH)
        self._history_executor = ThreadPoolExecutor(max_workers=1)
        self._history_cancel = threading.Event()
        self._generation = 0
        self._sync_id = 0
        self._loading_history = False
        self._history_ok = False
        self._monitoring_halted = False
        self._history_guard = {"reason": ""}
        self._pending_messages = []
        self._visible_days = recent_trading_days()

        self.bridge = Bridge()
        self.bridge.new_signal.connect(self.handle_incoming_signal)
        self.bridge.status_update.connect(self.update_status)
        self.bridge.connection_status.connect(self._connection_status)
        self.bridge.history_ready.connect(self._history_ready)

        self.init_ui()

        # 贴边自动收起 / 悬停展开计时器
        self._expand_timer = QTimer(self)
        self._expand_timer.setSingleShot(True)
        self._expand_timer.setInterval(300)
        self._expand_timer.timeout.connect(self._expand_if_hovered)

        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.setInterval(800)
        self._collapse_timer.timeout.connect(self._collapse_if_outside)

        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(80)
        self._hover_timer.timeout.connect(self.check_cursor_hover)
        self._hover_timer.start()

        # 美盘时段检查定时器
        self.market_check_timer = QTimer(self)
        self.market_check_timer.timeout.connect(self.check_market_status)
        self.market_check_timer.start(60000)

        # 本地模拟测试信号检查定时器
        self.mock_timer = QTimer(self)
        self.mock_timer.timeout.connect(self._check_mock_trigger)
        self.mock_timer.start(500)

        # 启动贴边状态与监控服务：启动时先完整展示 4 秒让用户看到监控就绪，随后若鼠标未在面板内再自动收起
        if not self._pinned:
            QTimer.singleShot(4000, self._collapse_if_outside)
        self.start_monitoring()

    def init_ui(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        init_w = self.config.get("width", 380)
        init_h = self.config.get("height", 620)
        self.setMinimumWidth(320)
        self.setMaximumWidth(550)
        self.resize(init_w, init_h)
        self.setWindowOpacity(1.0)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)

        # 奇犽主题卡片底图
        self.card = KilluaBackgroundCard(self, theme=self.theme)

        # 弥散柔和外框阴影
        self.card_shadow = QGraphicsDropShadowEffect(self)
        self.card_shadow.setBlurRadius(24)
        if self.theme == "dark":
            self.card_shadow.setColor(QColor(0, 0, 0, 220))
        else:
            self.card_shadow.setColor(QColor(15, 23, 42, 50))
        self.card_shadow.setOffset(0, 6)
        self.card.setGraphicsEffect(self.card_shadow)

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(14, 12, 14, 10)
        card_layout.setSpacing(8)

        # 标题栏
        title_bar = QHBoxLayout()
        title_bar.setSpacing(8)

        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color: #10B981; font-size: 10px;")

        self.title_lbl = QLabel("📡 DISCORD MONITOR")
        self.title_lbl.setStyleSheet(f"""
            color: {'#F8FAFC' if self.theme == 'dark' else '#0F172A'};
            font-weight: 800;
            font-size: 13px;
            font-family: 'SF Pro Display', sans-serif;
            letter-spacing: 0.5px;
        """)

        self.status_text = QLabel("正在连接...")
        self.status_text.setStyleSheet("color: #64748B; font-size: 10px;")

        # 主题切换
        self.theme_btn = QPushButton("☀️" if self.theme == "dark" else "🌙")
        self.theme_btn.setFixedSize(22, 22)
        self.theme_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.theme_btn.setToolTip("切换黑白模式")
        self.theme_btn.setStyleSheet("QPushButton { background: transparent; border: none; font-size: 12px; }")
        self.theme_btn.clicked.connect(self.toggle_theme)

        # 常驻锁定
        self.pin_btn = QPushButton("📌" if self._pinned else "◇")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setChecked(self._pinned)
        self.pin_btn.setFixedSize(22, 22)
        self.pin_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.pin_btn.setToolTip("固定展开常驻桌面 (不自动收起)")
        self.pin_btn.setStyleSheet("QPushButton { color: #94A3B8; background: transparent; border: none; font-size: 12px; }")
        self.pin_btn.toggled.connect(self.set_pinned)

        # 设置按钮
        cfg_btn = QPushButton("⚙")
        cfg_btn.setFixedSize(22, 22)
        cfg_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        cfg_btn.setStyleSheet("QPushButton { background: transparent; color: #64748B; border: none; font-size: 14px; }")
        cfg_btn.clicked.connect(self.show_settings)

        # 关闭按钮
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(22, 22)
        close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        close_btn.setStyleSheet("""
            QPushButton { background: transparent; color: #64748B; border: none; font-size: 11px; font-weight: bold; }
            QPushButton:hover { background-color: rgba(244, 63, 94, 0.85); color: white; border-radius: 11px; }
        """)
        close_btn.clicked.connect(self.quit_app)

        title_bar.addWidget(self.status_dot)
        title_bar.addWidget(self.title_lbl)
        title_bar.addWidget(self.status_text)
        title_bar.addStretch()
        title_bar.addWidget(self.theme_btn)
        title_bar.addWidget(self.pin_btn)
        title_bar.addWidget(cfg_btn)
        title_bar.addWidget(close_btn)

        card_layout.addLayout(title_bar)

        self.connection_notice = QLabel()
        self.connection_notice.setWordWrap(True)
        self.connection_notice.setStyleSheet('color: #F43F5E; font-size: 11px;')
        self.connection_notice.hide()
        card_layout.addWidget(self.connection_notice)
        self.recover_btn = QPushButton('恢复连接')
        self.recover_btn.setToolTip('手动恢复当前配置的连接；历史补齐仍遵守共享冷却时间')
        self.recover_btn.clicked.connect(lambda: self.start_monitoring(reset_safety=True))
        self.recover_btn.hide()
        card_layout.addWidget(self.recover_btn)

        # 筛选分段标签栏容器
        is_dark = self.theme == "dark"
        filters_box = QVBoxLayout()
        filters_box.setSpacing(3)
        filters_box.setContentsMargins(0, 0, 0, 3)

        # 1. 来源分段标签 [全部] [Dennis ⚡] [频道组 📡]
        row_src = QHBoxLayout()
        row_src.setSpacing(5)
        row_src.setContentsMargins(0, 0, 0, 0)
        lbl_src = QLabel("来源:")
        lbl_src.setStyleSheet(f"color: {'#64748B' if is_dark else '#94A3B8'}; font-size: 10px; font-weight: 600;")
        row_src.addWidget(lbl_src)

        self.source_filter = self.config.get("source_filter", "ALL")
        self.btn_filter_all = QPushButton("全部")
        self.btn_filter_dennis = QPushButton("Dennis ⚡")
        self.btn_filter_custom = QPushButton("频道组 📡")

        for btn, mode in [(self.btn_filter_all, "ALL"), (self.btn_filter_dennis, "DENNIS"), (self.btn_filter_custom, "CUSTOM")]:
            btn.setFixedHeight(20)
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.clicked.connect(lambda checked, m=mode: self.set_source_filter(m))
            row_src.addWidget(btn)

        row_src.addStretch()
        filters_box.addLayout(row_src)

        # 2. 多空立场分段标签 [全部] [🟢 做多] [🔴 做空] [🎯 平仓]
        row_stc = QHBoxLayout()
        row_stc.setSpacing(5)
        row_stc.setContentsMargins(0, 0, 0, 0)
        lbl_stc = QLabel("立场:")
        lbl_stc.setStyleSheet(f"color: {'#64748B' if is_dark else '#94A3B8'}; font-size: 10px; font-weight: 600;")
        row_stc.addWidget(lbl_stc)

        self.stance_filter = self.config.get("stance_filter", "ALL")
        self.btn_stance_all = QPushButton("全部")
        self.btn_stance_bull = QPushButton("🟢 做多")
        self.btn_stance_bear = QPushButton("🔴 做空")
        self.btn_stance_close = QPushButton("🎯 平仓")

        for btn, mode in [(self.btn_stance_all, "ALL"), (self.btn_stance_bull, "BULL"), (self.btn_stance_bear, "BEAR"), (self.btn_stance_close, "CLOSE")]:
            btn.setFixedHeight(20)
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.clicked.connect(lambda checked, m=mode: self.set_stance_filter(m))
            row_stc.addWidget(btn)

        row_stc.addStretch()
        filters_box.addLayout(row_stc)

        card_layout.addLayout(filters_box)
        self.update_filter_buttons_style()

        # 滚动区域
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical { width: 4px; background: transparent; margin: 0px; }
            QScrollBar::handle:vertical { background: rgba(255, 255, 255, 0.15); min-height: 24px; border-radius: 2px; }
            QScrollBar::handle:vertical:hover { background: rgba(255, 255, 255, 0.30); }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        """)

        self.signals_container = QWidget()
        self.signals_container.setStyleSheet("background: transparent;")
        self.signals_layout = QVBoxLayout(self.signals_container)
        self.signals_layout.setContentsMargins(0, 0, 0, 0)
        self.signals_layout.setSpacing(6)
        self.signals_layout.addStretch()

        self.scroll.setWidget(self.signals_container)
        card_layout.addWidget(self.scroll)

        # 底部状态栏 + 调整大小拉手
        footer = QHBoxLayout()
        footer.setContentsMargins(2, 0, 2, 0)
        self.market_lbl = QLabel(get_market_status_text(not self.config.get("only_market_hours", True)))
        self.market_lbl.setStyleSheet("color: #64748B; font-size: 10px; font-family: sans-serif;")
        footer.addWidget(self.market_lbl)
        footer.addStretch()

        size_grip = QSizeGrip(self)
        size_grip.setFixedSize(14, 14)
        footer.addWidget(size_grip)

        card_layout.addLayout(footer)
        main_layout.addWidget(self.card)

        # 贴边折叠胶囊标签
        self.edge_label = QLabel(self)
        self.edge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.edge_label.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.edge_label.hide()

        self.unread_dot = QLabel("●", self.edge_label)
        self.unread_dot.setStyleSheet("color: #F43F5E; font-size: 8px;")
        self.unread_dot.move(14, 4)
        self.unread_dot.hide()

        self.setup_cocoa_level()

    def setup_cocoa_level(self):
        if not HAS_COCOA:
            return
        def _apply():
            try:
                ns_view = objc.objc_object(c_void_p=int(self.winId()))
                ns_window = ns_view.window()
                if ns_window:
                    behavior = (
                        NSWindowCollectionBehaviorCanJoinAllSpaces |
                        NSWindowCollectionBehaviorFullScreenAuxiliary
                    )
                    ns_window.setCollectionBehavior_(behavior)
                    ns_window.setLevel_(NSScreenSaverWindowLevel + 2)
                    ns_window.setHidesOnDeactivate_(False)
                    ns_window.orderFrontRegardless()
            except Exception:
                pass
        QTimer.singleShot(50, _apply)

    def check_market_status(self):
        force_run = not self.config.get("only_market_hours", True)
        self.market_lbl.setText(get_market_status_text(force_run))
        days = recent_trading_days()
        if days != self._visible_days:
            self._visible_days = days
            self._rebuild_history()
        active, desc = is_market_monitoring_time(force_run)
        if not active and self.dc_client:
            self._stop_monitoring()
            self.update_status(desc)
        elif active and self.dc_client is None:
            self.start_monitoring()

    def update_status(self, text: str):
        halted = '已停止' in text
        waiting = '等待网络恢复' in text
        self.connection_notice.setText(text if halted or waiting else '')
        self.connection_notice.setVisible(halted or waiting)
        self.recover_btn.setVisible(halted)
        self.update_edge_label()
        self.status_dot.setToolTip(text)
        self.status_text.setToolTip(text)
        if "已停止" in text:
            self.status_dot.setStyleSheet("color: #F43F5E; font-size: 10px;")
            self.status_text.setText("已停止自动监听")
            self.status_text.setStyleSheet("color: #F43F5E; font-size: 10px; font-weight: 600;")
        elif "🟢" in text or "已连接" in text or "实时监控中" in text:
            self.status_dot.setStyleSheet("color: #10B981; font-size: 10px;")
            self.status_text.setText("已连接")
            self.status_text.setStyleSheet("color: #10B981; font-size: 10px; font-weight: 600;")
        elif "鉴权失败" in text or "Token" in text:
            self.status_dot.setStyleSheet("color: #F43F5E; font-size: 10px;")
            self.status_text.setText("Token无效")
            self.status_text.setStyleSheet("color: #F43F5E; font-size: 10px; font-weight: 600;")
        elif waiting or "断开" in text or "重试" in text or "重连" in text:
            self.status_dot.setStyleSheet("color: #F59E0B; font-size: 10px;")
            self.status_text.setText("等待恢复" if waiting else "重连中")
            self.status_text.setStyleSheet("color: #F59E0B; font-size: 10px; font-weight: 600;")
        else:
            self.status_dot.setStyleSheet("color: #64748B; font-size: 10px;")
            self.status_text.setText("休眠中" if "休市" in text or "非交易" in text else "连接中")
            self.status_text.setStyleSheet("color: #64748B; font-size: 10px;")

    def set_pinned(self, pinned: bool):
        self._pinned = pinned
        self.pin_btn.setText("📌" if pinned else "◇")
        self.pin_btn.setChecked(pinned)
        self.config["fixed_expanded"] = pinned
        save_config(self.config)
        if pinned and self._compact:
            self.expand_panel()

    def get_channel_group_ids(self) -> list:
        value = self.config.get("channel_ids") or self.config.get("additional_channels", "")
        return parse_channel_ids(value)

    def get_monitored_channel_ids(self) -> list:
        channel_ids = []
        dennis_id = str(self.config.get("channel_id", "")).strip()
        if dennis_id.isdigit():
            channel_ids.append(dennis_id)
        channel_ids.extend(self.get_channel_group_ids())
        return list(dict.fromkeys(channel_ids))

    def set_source_filter(self, mode: str):
        self.source_filter = mode
        self.config["source_filter"] = mode
        save_config(self.config)
        self.update_filter_buttons_style()
        self.apply_source_filter()

    def set_stance_filter(self, mode: str):
        self.stance_filter = mode
        self.config["stance_filter"] = mode
        save_config(self.config)
        self.update_filter_buttons_style()
        self.apply_source_filter()

    def update_filter_buttons_style(self):
        is_dark = self.theme == "dark"
        normal_bg = "rgba(255, 255, 255, 0.05)" if is_dark else "rgba(0, 0, 0, 0.04)"
        normal_text = "#94A3B8" if is_dark else "#64748B"
        normal_border = "rgba(255, 255, 255, 0.08)" if is_dark else "rgba(0, 0, 0, 0.08)"

        style_inactive = f"""
            QPushButton {{
                background: {normal_bg};
                color: {normal_text};
                border: 1px solid {normal_border};
                border-radius: 4px;
                font-size: 10px;
                font-weight: 600;
                padding: 0 7px;
            }}
            QPushButton:hover {{
                background: {'rgba(255, 255, 255, 0.10)' if is_dark else 'rgba(0, 0, 0, 0.08)'};
                color: {'#F8FAFC' if is_dark else '#0F172A'};
            }}
        """

        style_active_cyan = f"""
            QPushButton {{
                background: rgba(0, 240, 255, 0.16);
                color: #00F0FF;
                border: 1px solid rgba(0, 240, 255, 0.6);
                border-radius: 4px;
                font-size: 10px;
                font-weight: 700;
                padding: 0 7px;
            }}
        """

        style_active_amber = f"""
            QPushButton {{
                background: rgba(245, 158, 11, 0.18);
                color: #F59E0B;
                border: 1px solid rgba(245, 158, 11, 0.6);
                border-radius: 4px;
                font-size: 10px;
                font-weight: 700;
                padding: 0 7px;
            }}
        """

        style_active_green = f"""
            QPushButton {{
                background: rgba(16, 185, 129, 0.18);
                color: #10B981;
                border: 1px solid rgba(16, 185, 129, 0.6);
                border-radius: 4px;
                font-size: 10px;
                font-weight: 700;
                padding: 0 7px;
            }}
        """

        style_active_red = f"""
            QPushButton {{
                background: rgba(244, 63, 94, 0.18);
                color: #F43F5E;
                border: 1px solid rgba(244, 63, 94, 0.6);
                border-radius: 4px;
                font-size: 10px;
                font-weight: 700;
                padding: 0 7px;
            }}
        """

        style_active_blue = f"""
            QPushButton {{
                background: rgba(56, 189, 248, 0.18);
                color: #38BDF8;
                border: 1px solid rgba(56, 189, 248, 0.6);
                border-radius: 4px;
                font-size: 10px;
                font-weight: 700;
                padding: 0 7px;
            }}
        """

        if hasattr(self, "btn_filter_all"):
            self.btn_filter_all.setStyleSheet(style_active_cyan if self.source_filter == "ALL" else style_inactive)
            self.btn_filter_dennis.setStyleSheet(style_active_cyan if self.source_filter == "DENNIS" else style_inactive)
            self.btn_filter_custom.setStyleSheet(style_active_green if self.source_filter == "CUSTOM" else style_inactive)

        if hasattr(self, "btn_stance_all"):
            stc = getattr(self, "stance_filter", "ALL")
            self.btn_stance_all.setStyleSheet(style_active_cyan if stc == "ALL" else style_inactive)
            self.btn_stance_bull.setStyleSheet(style_active_green if stc == "BULL" else style_inactive)
            self.btn_stance_bear.setStyleSheet(style_active_red if stc == "BEAR" else style_inactive)
            self.btn_stance_close.setStyleSheet(style_active_blue if stc == "CLOSE" else style_inactive)

    def apply_source_filter(self):
        src_f = self.source_filter
        stc_f = getattr(self, "stance_filter", "ALL")
        for i in range(self.signals_layout.count()):
            item = self.signals_layout.itemAt(i)
            if not item:
                continue
            w = item.widget()
            if not w:
                continue
            source = getattr(w, "source", None)
            if not source:
                continue

            show = True
            # 1. 来源过滤
            if src_f == "DENNIS" and source != "DENNIS":
                show = False
            elif src_f == "CUSTOM" and source != "CUSTOM":
                show = False

            # 2. 多空立场过滤
            if show and stc_f != "ALL":
                if source == "DENNIS":
                    dir_d = getattr(w, "direction", "CALL")
                    is_closed = (getattr(w, "close_data", None) is not None) or (getattr(w, "status", "OPEN") in ("PROFIT", "LOSS", "FLAT"))
                    if stc_f == "BULL":
                        show = (dir_d == "CALL")
                    elif stc_f == "BEAR":
                        show = (dir_d == "PUT")
                    elif stc_f == "CLOSE":
                        show = is_closed
                elif source == "CUSTOM":
                    show = False

            w.setVisible(show)

    def toggle_theme(self):
        self.theme = "light" if self.theme == "dark" else "dark"
        self.theme_btn.setText("☀️" if self.theme == "dark" else "🌙")
        self.config["theme"] = self.theme
        save_config(self.config)
        self.card.set_theme(self.theme)
        self.update_edge_label()
        self.update_filter_buttons_style()
        for i in range(self.signals_layout.count()):
            item = self.signals_layout.itemAt(i)
            if item and item.widget() and hasattr(item.widget(), "set_theme"):
                item.widget().set_theme(self.theme)

    def show_settings(self):
        self._settings_open = True
        self._collapse_timer.stop()
        self._expand_timer.stop()
        dlg = SettingsDialog(self, self.config)
        dlg.center_on_screen()
        if dlg.exec():
            new_cfg = dlg.get_settings()
            self.config.update(new_cfg)
            save_config(self.config)
            self.setWindowOpacity(self.config.get("opacity", 0.92))
            self.set_pinned(self.config.get("fixed_expanded", False))
            self.start_monitoring(reset_safety=True)
        self._settings_open = False
        if not self._pinned and not self.cursor_inside_panel():
            self._collapse_timer.start()

    def cursor_inside_panel(self) -> bool:
        return self.frameGeometry().contains(QCursor.pos())

    def check_cursor_hover(self):
        if self._closing or not self.isVisible() or self._changing_geometry:
            return
        if self._settings_open or self._dragging:
            self._expand_timer.stop()
            self._collapse_timer.stop()
            return
        if self.cursor_inside_panel():
            self._collapse_timer.stop()
            if self._compact and not self._expand_timer.isActive():
                self._expand_timer.start()
        else:
            self._expand_timer.stop()
            if not self._compact and not self._pinned and not self._collapse_timer.isActive():
                self._collapse_timer.start()

    def _expand_if_hovered(self):
        if self.cursor_inside_panel() and not self._settings_open:
            self.expand_panel()

    def _collapse_if_outside(self):
        if not self.cursor_inside_panel() and not self._dragging:
            self.collapse_panel()

    def is_left_dock(self) -> bool:
        return self.config.get("dock_edge", "left") == "left"

    def update_edge_label(self):
        count_str = str(self._unread) if self._unread < 100 else "99+"
        paused = self._monitoring_halted or bool(self._history_guard['reason'])
        self.edge_label.setText(f"{'⛔' if paused else '📡'}\nD\nI\nS\nC\nO\nR\nD\n·\n{count_str}")
        self.edge_label.setToolTip('监控已暂停，请展开查看原因并恢复连接' if paused else self.status_text.toolTip())
        self.unread_dot.setVisible(self._unread > 0)
        is_dark = self.theme == "dark"
        is_left = self.is_left_dock()
        edge_bg = "#07090E" if is_dark else "#F8FAFC"
        edge_color = "#00F0FF" if is_dark else "#0284C7"
        border_color = "#00F0FF" if (is_dark and self._unread > 0) else ("rgba(0, 240, 255, 0.45)" if is_dark else "rgba(2, 132, 199, 0.45)")
        if paused:
            edge_color = border_color = '#F43F5E'

        if is_left:
            radius_style = "border-top-right-radius: 8px; border-bottom-right-radius: 8px; border-top-left-radius: 0px; border-bottom-left-radius: 0px; border-left: none;"
            self.unread_dot.move(14, 4)
        else:
            radius_style = "border-top-left-radius: 8px; border-bottom-left-radius: 8px; border-top-right-radius: 0px; border-bottom-right-radius: 0px; border-right: none;"
            self.unread_dot.move(6, 4)

        self.edge_label.setStyleSheet(f"""
            background: {edge_bg};
            color: {edge_color};
            border: 1.5px solid {border_color};
            {radius_style}
            font-size: 10px;
            font-weight: 800;
            font-family: 'SF Pro Display', sans-serif;
        """)

    def collapse_panel(self):
        if self._compact or self._pinned or self._settings_open or self._closing:
            return
        geo = self.screen().availableGeometry()
        self._changing_geometry = True
        self._compact = True
        self.card.hide()
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.setMinimumSize(0, 0)
        self.setMaximumSize(28, 128)
        self.resize(28, 128)
        y = max(geo.top(), min(self.y(), geo.bottom() - 127))

        if self.is_left_dock():
            self.move(geo.left(), y)
        else:
            self.move(geo.right() - 27, y)

        self.edge_label.setGeometry(0, 0, 28, 128)
        self.update_edge_label()
        self.edge_label.show()
        self._changing_geometry = False
        self._expand_timer.stop()
        self._collapse_timer.stop()

    def expand_panel(self):
        if self._dragging or self._closing:
            return
        if not self._compact:
            self.setup_cocoa_level()
            self.activateWindow()
            self.raise_()
            return
        geo = self.screen().availableGeometry()
        self._changing_geometry = True
        self.edge_label.hide()
        self.setMaximumSize(520, 16777215)
        self.setMinimumSize(300, 0)
        self.layout().setContentsMargins(10, 10, 10, 10)
        self.card.show()
        target_w = self.config.get("width", 360)
        target_h = self.config.get("height", 600)

        if self.is_left_dock():
            x = max(geo.left(), geo.left() - 10)
        else:
            x = max(geo.left(), geo.right() - target_w + 10)

        y = max(geo.top(), min(self.y(), geo.bottom() - target_h + 10))
        self.setGeometry(x, y, target_w, target_h)
        self._compact = False
        self._unread = 0
        self.update_edge_label()
        self._changing_geometry = False
        self._expand_timer.stop()
        self._collapse_timer.stop()
        self.setup_cocoa_level()
        self.activateWindow()
        self.raise_()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._compact:
                self.expand_panel()
                event.accept()
                return
            self._dragging = True
            self._drag_start_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging and (event.buttons() & Qt.MouseButton.LeftButton):
            new_pos = event.globalPosition().toPoint() - self._drag_start_pos
            if self._compact:
                geo = self.screen().availableGeometry()
                if self.is_left_dock():
                    new_pos.setX(geo.left())
                else:
                    new_pos.setX(geo.right() - 27)
                new_pos.setY(max(geo.top(), min(new_pos.y(), geo.bottom() - 127)))
            self.move(new_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._dragging = False
        if not self._compact:
            self.config["width"] = self.width()
            self.config["height"] = self.height()
            save_config(self.config)

    def handle_incoming_signal(self, data: dict):
        if self._closing:
            return
        if '_generation' in data and data['_generation'] != self._generation:
            return

        incoming_cid = str(data.get('channel_id', '')).strip()
        dennis_cid = str(self.config.get('channel_id', '')).strip()
        channel_group_ids = self.get_channel_group_ids()
        all_monitored_cids = set(self.get_monitored_channel_ids())

        if incoming_cid not in all_monitored_cids and not data.get('force_toast'):
            return

        is_dennis_sig = incoming_cid == dennis_cid or data.get('force_toast')
        is_custom_sig = incoming_cid in channel_group_ids and incoming_cid != dennis_cid

        if not data.get('_history_load') and not data.get('force_toast'):
            if self._loading_history:
                self._pending_messages.append(data)
                return
            self.history.save(incoming_cid, [data], advance=self._history_ok)

        if not data.get('force_toast') and not in_history_window(data):
            return

        msg_id = data.get('id')
        if msg_id and msg_id in self.seen_message_ids:
            return

        ts_raw = data.get('timestamp')
        if ts_raw:
            try:
                dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                time_str = dt.astimezone(NY_TZ).strftime("%m-%d %H:%M:%S")
            except Exception:
                time_str = datetime.now(NY_TZ).strftime("%m-%d %H:%M:%S")
        else:
            time_str = datetime.now(NY_TZ).strftime("%m-%d %H:%M:%S")

        is_history = data.get("_history_load", False)
        force_toast = data.get("force_toast", False)
        kw_filter = self.config.get("filter_keyword", "").strip()
        keywords = [k.strip().upper() for k in re.split(r"[,| ]+", kw_filter) if k.strip()] if kw_filter else []

        if is_custom_sig:
            if not message_matches_keywords(data, keywords):
                return
            if msg_id:
                self.seen_message_ids.add(msg_id)
            card = DiscordMessageCard(data, parent=self.signals_container, theme=self.theme)
            self.signals_layout.insertWidget(0, card)
            card.show()
            self.apply_source_filter()
            if not is_history:
                if self._compact:
                    self._unread += 1
                    self.update_edge_label()
                self.show_toast_popup(custom_message_toast(data), time_str, force=force_toast)
            return

        if not is_dennis_sig:
            return
        parsed = parse_tape_signal(content=data.get("content", ""), embeds=data.get("embeds", []))
        if not parsed:
            return
        if keywords and parsed["ticker"].upper() not in keywords:
            return
        if msg_id:
            self.seen_message_ids.add(msg_id)

        ticker = parsed["ticker"]
        contract = parsed["contract"]
        action = parsed["action"]
        direction = parsed["direction"]
        strategy = parsed.get("strategy", "T1")
        position_key = f"{strategy}_{ticker}_{contract}"

        if action == "OPEN":
            if position_key in self.active_positions:
                card = self.active_positions[position_key]
                card.set_open(parsed, time_str)
            else:
                card = DennisTradeCard(ticker, contract, direction, strategy, parent=self.signals_container, theme=self.theme)
                card.set_open(parsed, time_str)
                self.active_positions[position_key] = card
                self.signals_layout.insertWidget(0, card)
                card.show()
        elif action == "CLOSE":
            if position_key in self.active_positions:
                card = self.active_positions.pop(position_key)
                card.set_close(parsed, time_str)
                self.signals_layout.removeWidget(card)
                self.signals_layout.insertWidget(0, card)
            else:
                card = DennisTradeCard(ticker, contract, direction, strategy, parent=self.signals_container, theme=self.theme)
                card.set_close(parsed, time_str)
                self.signals_layout.insertWidget(0, card)
                card.show()

        self.apply_source_filter()
        if not is_history:
            if self._compact:
                self._unread += 1
                self.update_edge_label()
            self.show_toast_popup(parsed, time_str, force=force_toast)

    def show_toast_popup(self, parsed: dict, time_str: str, force: bool = False):
        if self._closing:
            return
        try:
            if getattr(self, '_active_toast', None):
                try:
                    self._active_toast.close()
                except Exception:
                    pass
            self._active_toast = SignalToastPopup(self, parsed, time_str)
            self._active_toast.show()
        except Exception as e:
            print(f"[DiscordChannelMonitor] Failed to show toast: {e}", file=sys.stderr)

    def _check_mock_trigger(self):
        """检查是否有本地注入的测试信号"""
        if os.path.exists(MOCK_TRIGGER_PATH):
            try:
                with open(MOCK_TRIGGER_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                os.remove(MOCK_TRIGGER_PATH)
                self.bridge.new_signal.emit(data)
            except Exception:
                pass

    def _stop_monitoring(self):
        self._history_cancel.set()
        self._generation += 1
        if self.dc_client:
            self.dc_client.stop()
            self.dc_client = None
        # Preserve any received messages, without moving the successful watermark.
        for message in self._pending_messages:
            ch = str(message.get('channel_id', ''))
            if ch:
                self.history.save(ch, [message])
        self._pending_messages = []
        self._loading_history = False
        self._history_ok = False

    def _rebuild_history(self):
        self.seen_message_ids.clear()
        self.active_positions.clear()
        self.signals_container.setUpdatesEnabled(False)
        try:
            for i in reversed(range(self.signals_layout.count())):
                item = self.signals_layout.itemAt(i)
                if item and item.widget():
                    widget = self.signals_layout.takeAt(i).widget()
                    widget.hide()
                    widget.deleteLater()

            messages = []
            for channel_id in self.get_monitored_channel_ids():
                if channel_id:
                    messages.extend(self.history.visible(channel_id))
            messages.sort(key=lambda message: int(message.get("id", 0)))

            for message in messages:
                self.handle_incoming_signal(dict(message, _history_load=True))
            self.apply_source_filter()
        finally:
            self.signals_container.setUpdatesEnabled(True)

    def start_monitoring(self, reset_safety=False):
        if reset_safety:
            self._monitoring_halted = False
            self._history_guard = {"reason": ""}
        if self._monitoring_halted:
            return
        self._stop_monitoring()
        self._rebuild_history()
        if hasattr(self, 'title_lbl'):
            self.title_lbl.setText("📡 DISCORD MONITOR")
        active, desc = is_market_monitoring_time(not self.config.get('only_market_hours', True))
        if not active:
            self.update_status(desc)
            return
        token = self.config.get('token', '')
        target_channels = self.get_monitored_channel_ids()
        if not token or not target_channels:
            self.update_status('Token 或频道配置无效')
            return

        generation = self._generation
        self._history_cancel = threading.Event()
        # Connect first and buffer events until the chronological history is ready.
        self._loading_history = True
        self.dc_client = DiscordGatewayClient(
            token=token, target_channel_ids=target_channels,
            should_connect=lambda: is_market_monitoring_time(not self.config.get('only_market_hours', True))[0],
            on_message_callback=lambda msg: self.bridge.new_signal.emit(dict(msg, _generation=generation)),
            on_status_callback=lambda st: self.bridge.connection_status.emit(generation, st))
        self.dc_client.start()

    def _connection_status(self, generation, text):
        if self._closing or generation != self._generation:
            return
        if '已停止自动监听' in text:
            self._monitoring_halted = True
            self._stop_monitoring()
            self._rebuild_history()
            self.update_status(text)
            return
        self.update_status(text)
        if '🟢' in text:
            if self._history_guard['reason']:
                self._history_ready(generation, self._sync_id, False, self._history_guard['reason'])
                return
            # READY and RESUMED both perform one catch-up, never periodic polling.
            self._loading_history = True
            self._history_ok = False
            self._history_cancel.set()
            self._history_cancel = threading.Event()
            self._sync_id += 1
            self._history_executor.submit(
                self._fetch_history, self.config['token'],
                generation, self._sync_id, self._history_cancel, self._history_guard)
        elif '断开' in text or '重连' in text or '恢复连接' in text or '等待网络恢复' in text:
            self._history_ok = False
            self._loading_history = True
            self._sync_id += 1
            self._history_cancel.set()

    def _fetch_history(self, token, generation, sync_id, cancelled, history_guard=None):
        guard = self._history_guard if history_guard is None else history_guard

        def allowed():
            return not cancelled.is_set() and not guard["reason"]

        success, error = False, guard["reason"]
        try:
            if hasattr(self, "get_monitored_channel_ids"):
                channel_ids = self.get_monitored_channel_ids()
            else:
                channel_ids = [str(self.config.get("channel_id", ""))]
                configured_ids = self.config.get("channel_ids") or self.config.get("additional_channels", "")
                channel_ids.extend(parse_channel_ids(configured_ids))
                channel_ids = list(dict.fromkeys(cid for cid in channel_ids if cid))

            all_ok = False
            for channel_id in channel_ids:
                if not allowed():
                    break
                ok = self.history.sync(
                    channel_id,
                    discord_page_fetcher(token, channel_id, allowed),
                    cancelled=lambda: not allowed(),
                    filter_fn=None,
                )
                if not ok:
                    break
            else:
                all_ok = True
            success = all_ok
        except HistorySyncStopped as exc:
            error = str(exc) + "；自动补齐已暂停，检查后在设置中保存以重试"
            guard["reason"] = error
        except Exception as exc:
            error = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
        if not cancelled.is_set():
            self.bridge.history_ready.emit(generation, sync_id, success, error)

    def _history_ready(self, generation, sync_id, success, error):
        if self._closing or generation != self._generation or sync_id != self._sync_id:
            return
        pending, self._pending_messages = self._pending_messages, []
        previously_seen = set(self.seen_message_ids)
        dennis_channel_id = str(self.config.get("channel_id", "")).strip()
        channel_group_ids = set(self.get_channel_group_ids())

        by_channel = {}
        for message in pending:
            channel_id = str(message.get("channel_id", ""))
            if channel_id:
                by_channel.setdefault(channel_id, []).append(message)
        for channel_id, messages in by_channel.items():
            self.history.save(channel_id, messages, advance=success)

        self._history_ok = success
        self._loading_history = False
        self._rebuild_history()

        for message in pending:
            if not in_history_window(message):
                continue
            message_id = message.get("id")
            if message_id in previously_seen:
                continue
            previously_seen.add(message_id)
            channel_id = str(message.get("channel_id", ""))
            is_custom = channel_id in channel_group_ids and channel_id != dennis_channel_id
            if is_custom:
                keyword_filter = self.config.get("filter_keyword", "").strip()
                keywords = [k.strip().upper() for k in re.split(r"[,| ]+", keyword_filter) if k.strip()] if keyword_filter else []
                parsed = custom_message_toast(message) if message_matches_keywords(message, keywords) else None
            else:
                parsed = parse_tape_signal(message.get("content", ""), message.get("embeds", []))

            if parsed and str(message_id) in self.seen_message_ids:
                if self._compact:
                    self._unread += 1
                    self.update_edge_label()
                timestamp = message.get("timestamp")
                try:
                    time_str = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(NY_TZ).strftime("%m-%d %H:%M:%S") if timestamp else datetime.now(NY_TZ).strftime("%m-%d %H:%M:%S")
                except Exception:
                    time_str = datetime.now(NY_TZ).strftime("%m-%d %H:%M:%S")
                self.show_toast_popup(parsed, time_str)

        if error:
            self.status_text.setText("补齐已暂停" if self._history_guard["reason"] else "历史待补齐")
            self.status_text.setToolTip(error)
            self.connection_notice.setText(error)
            self.connection_notice.show()
            self.recover_btn.show()

    def quit_app(self):
        self._closing = True
        self._stop_monitoring()
        try:
            self._history_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        self.close()
        app = QApplication.instance()
        if app:
            app.quit()
        sys.exit(0)

    def closeEvent(self, event):
        self._closing = True
        self._stop_monitoring()
        try:
            self._history_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        event.accept()
        app = QApplication.instance()
        if app:
            app.quit()


# Preserve imports from the former Dennis-specific class name.
DennisTapeMonitorWindow = DiscordChannelMonitorWindow

IPC_SERVER_NAME = "dennis_tape_monitor_ipc"

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("DiscordChannelMonitor")
    app.setApplicationDisplayName("Discord Channel Monitor")
    app.setQuitOnLastWindowClosed(True)

    # 1. 单实例检查：尝试连接正在运行的实例
    socket = QLocalSocket()
    socket.connectToServer(IPC_SERVER_NAME)
    if socket.waitForConnected(400):
        socket.write(b"EXPAND\n")
        socket.waitForBytesWritten(800)
        socket.disconnectFromServer()
        # 已有实例在运行，并已通知其展开，当前二次启动进程直接退出
        sys.exit(0)

    # 2. 主实例创建本地 IPC 服务
    server = QLocalServer()
    server.removeServer(IPC_SERVER_NAME)
    server.listen(IPC_SERVER_NAME)
    app.aboutToQuit.connect(lambda: server.removeServer(IPC_SERVER_NAME))

    window = DiscordChannelMonitorWindow()
    window.show()

    def handle_ipc_connection():
        client = server.nextPendingConnection()
        if client:
            if client.waitForReadyRead(400):
                _ = bytes(client.readAll())
            client.disconnectFromServer()
            if window._closing or not window.isVisible():
                window._closing = False
                window.show()
                window.start_monitoring()
            window.expand_panel()
            window.setup_cocoa_level()

    server.newConnection.connect(handle_ipc_connection)

    # 3. macOS 原生 Delegate 捕获 Dock/Finder 重复打开事件 (applicationShouldHandleReopen)
    if HAS_COCOA:
        try:
            from Cocoa import NSObject, NSApplication
            class ReopenDelegate(NSObject):
                def applicationShouldHandleReopen_hasVisibleWindows_(self, ns_app, flag):
                    if window._closing or not window.isVisible():
                        window._closing = False
                        window.show()
                        window.start_monitoring()
                    window.expand_panel()
                    window.setup_cocoa_level()
                    return True
            reopen_delegate = ReopenDelegate.alloc().init()
            NSApplication.sharedApplication().setDelegate_(reopen_delegate)
        except Exception:
            pass

    # 4. 全局事件过滤器捕获 ApplicationActivate
    class ActivationFilter(QObject):
        def eventFilter(self, obj, event):
            if event.type() in (QEvent.Type.ApplicationActivate, QEvent.Type.FileOpen):
                if window._closing or not window.isVisible():
                    window._closing = False
                    window.show()
                    window.start_monitoring()
                window.expand_panel()
                window.setup_cocoa_level()
            return super().eventFilter(obj, event)

    activation_filter = ActivationFilter()
    app.installEventFilter(activation_filter)

    sys.exit(app.exec())

if __name__ == "__main__":
    main()
