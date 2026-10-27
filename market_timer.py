import zoneinfo
from datetime import datetime, time, timedelta
from functools import lru_cache
import exchange_calendars as xcals

NY_TZ = zoneinfo.ZoneInfo("America/New_York")

@lru_cache(maxsize=4)
def market_calendar(year: int):
    return xcals.get_calendar("XNYS", start=f"{year - 1}-01-01", end=f"{year + 1}-12-31")

def recent_trading_days(now=None):
    """The current NY session (if any) and the preceding sessions, three total."""
    now = (now or datetime.now(NY_TZ)).astimezone(NY_TZ)
    sessions = market_calendar(now.year).sessions_in_range(
        now.date() - timedelta(days=30), now.date())
    return tuple(s.date() for s in sessions[-3:])

def history_window_start(now=None):
    return datetime.combine(recent_trading_days(now)[0], time.min, NY_TZ)

def is_market_monitoring_time(force_run: bool = False) -> tuple[bool, str]:
    """
    检查当前是否处于美股监控窗口期：
    常规开盘时间：美东时间 09:30 - 16:00 (周一至周五)
    监控时间窗口：盘前 5 分钟 (09:25) 至 盘后 15 分钟 (16:15)

    返回：(是否应拉取监控, 状态描述字符串)
    """
    if force_run:
        return True, "24H全天监控"

    now_ny = datetime.now(NY_TZ)
    calendar = market_calendar(now_ny.year)
    if not calendar.is_session(now_ny.date()):
        return False, "休市中 (周末/节假日)"

    cur_time = now_ny.time()
    # 监控窗口：09:25:00 - 16:15:00
    start_window = time(9, 25, 0)
    close_time = calendar.session_close(now_ny.date()).to_pydatetime().astimezone(NY_TZ)
    end_window = (close_time + timedelta(minutes=15)).time()

    if start_window <= cur_time <= end_window:
        if cur_time < time(9, 30, 0):
            return True, "盘前准备 (09:25-09:30)"
        elif cur_time <= close_time.time():
            return True, "交易进行中 (美东)"
        else:
            return True, "盘后整理 (收盘后15分钟)"
    else:
        return False, f"非交易时段 ({now_ny.strftime('%H:%M %Z')})"

def get_market_status_text(force_run: bool = False) -> str:
    now_ny = datetime.now(NY_TZ)
    is_active, desc = is_market_monitoring_time(force_run)
    return f"{desc} | 美东 {now_ny.strftime('%H:%M')}"
