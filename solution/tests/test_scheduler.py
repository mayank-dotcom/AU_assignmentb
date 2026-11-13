"""
Unit tests for scheduling logic, window math, next eligible time, and priority rules.
Uses FakeClock; zero dependencies on external provider.
"""
from datetime import datetime, timedelta

import pytest

from solution.clock import IST
from solution.config import RetryPolicy, WindowConfig
from solution.scheduler import (
    calculate_next_eligible_time,
    in_window,
    is_callable_now,
    next_combined_open,
    next_window_open,
)


def test_in_window_basic():
    # 2026-11-12 is Thursday
    thu_noon = datetime(2026, 11, 12, 12, 0, tzinfo=IST)
    assert in_window(thu_noon, "10:00", "19:00", ["mon", "tue", "wed", "thu", "fri", "sat"])
    assert not in_window(thu_noon, "10:00", "19:00", ["mon", "tue", "wed", "fri", "sat"])  # Thursday excluded


def test_in_window_boundaries():
    # Exactly at start
    start_t = datetime(2026, 11, 12, 10, 0, 0, tzinfo=IST)
    assert in_window(start_t, "10:00", "19:00", ["thu"])

    # Exactly at end (exclusive [start, end))
    end_t = datetime(2026, 11, 12, 19, 0, 0, tzinfo=IST)
    assert not in_window(end_t, "10:00", "19:00", ["thu"])

    # 1 second before end
    just_before_end = datetime(2026, 11, 12, 18, 59, 59, tzinfo=IST)
    assert in_window(just_before_end, "10:00", "19:00", ["thu"])


def test_is_callable_now_safety_buffer():
    camp_win = WindowConfig(start="10:00", end="19:00", days=["thu"])
    reg_win = WindowConfig(start="09:00", end="21:00", days=None)

    # 10:30 is well inside
    assert is_callable_now(datetime(2026, 11, 12, 10, 30, tzinfo=IST), camp_win, reg_win)

    # 18:59:55 is within 15 seconds of 19:00 closing -> should return False to prevent race
    almost_close = datetime(2026, 11, 12, 18, 59, 55, tzinfo=IST)
    assert not is_callable_now(almost_close, camp_win, reg_win, buffer_seconds=15.0)


def test_next_window_open_same_day():
    # Current time 08:30 Thursday, window opens at 10:00
    early_t = datetime(2026, 11, 12, 8, 30, tzinfo=IST)
    expected = datetime(2026, 11, 12, 10, 0, tzinfo=IST)
    res = next_window_open(early_t, "10:00", "19:00", ["thu", "fri"])
    assert res == expected


def test_next_window_open_next_day():
    # Current time 20:00 Thursday, window was 10:00-19:00
    late_t = datetime(2026, 11, 12, 20, 0, tzinfo=IST)
    expected = datetime(2026, 11, 13, 10, 0, tzinfo=IST)  # Friday 10:00
    res = next_window_open(late_t, "10:00", "19:00", ["thu", "fri"])
    assert res == expected


def test_next_window_open_weekend_skip():
    # Current time Friday 19:30 (2026-11-13). Campaign is Mon-Fri only.
    # Should skip Saturday and Sunday and return Monday 09:30 (2026-11-16).
    fri_late = datetime(2026, 11, 13, 19, 30, tzinfo=IST)
    expected_mon = datetime(2026, 11, 16, 9, 30, tzinfo=IST)
    res = next_window_open(fri_late, "09:30", "18:00", ["mon", "tue", "wed", "thu", "fri"])
    assert res == expected_mon


def test_next_combined_open_intersection():
    camp_win = WindowConfig(start="10:00", end="19:00", days=["mon", "tue", "wed", "thu", "fri"])
    reg_win = WindowConfig(start="09:00", end="21:00", days=None)

    # Thursday 08:00 -> both open at 10:00
    t1 = datetime(2026, 11, 12, 8, 0, tzinfo=IST)
    assert next_combined_open(t1, camp_win, reg_win) == datetime(2026, 11, 12, 10, 0, tzinfo=IST)

    # Thursday 19:30 -> campaign closed, regulatory open. Next mutual open is Friday 10:00.
    t2 = datetime(2026, 11, 12, 19, 30, tzinfo=IST)
    assert next_combined_open(t2, camp_win, reg_win) == datetime(2026, 11, 13, 10, 0, tzinfo=IST)


def test_calculate_next_eligible_time_retry_policy():
    retry_policy = RetryPolicy(no_answer_minutes=60, busy_minutes=60, failed_minutes=15)
    camp_win = WindowConfig(start="10:00", end="19:00", days=["thu", "fri"])
    reg_win = WindowConfig(start="09:00", end="21:00", days=None)

    # 1. No answer at 14:00 Thursday -> next eligible is 15:00 (+60 min)
    ended_1 = datetime(2026, 11, 12, 14, 0, tzinfo=IST)
    next_1 = calculate_next_eligible_time("no_answer", None, ended_1, None, retry_policy, camp_win, reg_win)
    assert next_1 == datetime(2026, 11, 12, 15, 0, tzinfo=IST)

    # 2. Busy at 14:00 Thursday -> next eligible is 15:00 (+60 min)
    next_busy = calculate_next_eligible_time("busy", None, ended_1, None, retry_policy, camp_win, reg_win)
    assert next_busy == datetime(2026, 11, 12, 15, 0, tzinfo=IST)

    # 3. Failed at 14:00 Thursday -> next eligible is 14:15 (+15 min)
    next_failed = calculate_next_eligible_time("failed", None, ended_1, None, retry_policy, camp_win, reg_win)
    assert next_failed == datetime(2026, 11, 12, 14, 15, tzinfo=IST)

    # 4. No answer at 18:30 Thursday -> 18:30 + 60 min = 19:30 (outside campaign window [10:00-19:00))
    # Must shift to Friday 10:00 opening!
    ended_late = datetime(2026, 11, 12, 18, 30, tzinfo=IST)
    next_late = calculate_next_eligible_time("no_answer", None, ended_late, None, retry_policy, camp_win, reg_win)
    assert next_late == datetime(2026, 11, 13, 10, 0, tzinfo=IST)


def test_calculate_next_eligible_time_callback():
    retry_policy = RetryPolicy()
    camp_win = WindowConfig(start="10:00", end="19:00", days=["thu", "fri"])
    reg_win = WindowConfig(start="09:00", end="21:00", days=None)

    ended_at = datetime(2026, 11, 12, 15, 0, tzinfo=IST)

    # Callback at 16:30 (within window)
    cb_in_window = datetime(2026, 11, 12, 16, 30, tzinfo=IST)
    res_in = calculate_next_eligible_time(
        "completed", "callback_requested", ended_at, cb_in_window, retry_policy, camp_win, reg_win
    )
    assert res_in == cb_in_window

    # Callback at 20:00 (outside campaign window) -> shifts to Friday 10:00
    cb_out_window = datetime(2026, 11, 12, 20, 0, tzinfo=IST)
    res_out = calculate_next_eligible_time(
        "completed", "callback_requested", ended_at, cb_out_window, retry_policy, camp_win, reg_win
    )
    assert res_out == datetime(2026, 11, 13, 10, 0, tzinfo=IST)

