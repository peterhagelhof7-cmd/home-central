from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")


def now() -> datetime:
    return datetime.now(TZ)


def today_iso() -> str:
    return now().strftime("%Y-%m-%d")
