from datetime import date, datetime, timedelta, timezone

BRT = timezone(timedelta(hours=-3), name="BRT")


def now_brt() -> datetime:
    return datetime.now(BRT)


def now_brt_naive() -> datetime:
    return now_brt().replace(tzinfo=None)


def today_brt() -> date:
    return now_brt().date()


def to_brt_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(BRT).replace(tzinfo=None)
