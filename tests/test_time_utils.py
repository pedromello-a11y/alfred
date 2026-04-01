"""Testa time_utils: timezone BRT correto."""
from datetime import timezone

from app.services.time_utils import now_brt, now_brt_naive, today_brt


def test_now_brt_is_aware():
    dt = now_brt()
    assert dt.tzinfo is not None


def test_now_brt_naive_is_naive():
    dt = now_brt_naive()
    assert dt.tzinfo is None


def test_today_brt_is_date():
    import datetime
    d = today_brt()
    assert isinstance(d, datetime.date)
