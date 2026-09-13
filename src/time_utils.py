"""Application clock: Beijing time, independent of the host timezone."""
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")


def beijing_now():
    return datetime.now(BEIJING)


def beijing_today():
    return beijing_now().date()


def beijing_now_naive():
    """Compatibility with existing SQLite DateTime columns (Beijing wall time)."""
    return beijing_now().replace(tzinfo=None)


def clock_context():
    now = beijing_now()
    return (f"\n[PROGRAM_CLOCK] 当前北京时间：{now.isoformat(timespec='seconds')}，"
            f"星期{['一', '二', '三', '四', '五', '六', '日'][now.weekday()]}。"
            "时区 Asia/Shanghai (UTC+08:00)。今天/昨天均以此为准，禁止从旧对话推测当前日期。"
            "行情时间、抓取时间、财报期和当前时间必须分开；不能把非交易日最近收盘称为今日实时。"
            "缺少行情时间或数据过期时必须说明，禁止声称已取得实时数据。美股交易日仍按交易所日历，"
            "展示时转换为北京时间，不改写原始交易日。[/PROGRAM_CLOCK]\n")
