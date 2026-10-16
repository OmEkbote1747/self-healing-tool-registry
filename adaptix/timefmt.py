"""Display times in Indian Standard Time (IST, UTC+05:30). Records are stored in UTC; only display changes."""
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

IST = _tz(_td(hours=5, minutes=30), "IST")   # India has no daylight saving: a fixed offset is exact


def _parse(ts):
    if not ts:
        return None
    try:
        d = _dt.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=_tz.utc) if d.tzinfo is None else d     # stored times are UTC


def ist(ts, seconds: bool = True) -> str:
    """'17 Oct 2026, 00:34:12 IST' - records are stored in UTC; the UI always shows Indian Standard Time."""
    d = _parse(ts)
    if d is None:
        return "—"
    return d.astimezone(IST).strftime("%d %b %Y, %H:%M" + (":%S" if seconds else "")) + " IST"


def ist_time(ts, millis: bool = False) -> str:
    """Time of day only, e.g. '00:34:12.481 IST' (timelines)."""
    d = _parse(ts)
    if d is None:
        return "—"
    s = d.astimezone(IST).strftime("%H:%M:%S.%f")
    return (s[:-3] if millis else s[:-7]) + " IST"
