"""
Pure scheduling and window calculations.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from .config import RetryPolicy, WindowConfig

IST = timezone(timedelta(hours=5, minutes=30))
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_hm(s: str) -> Tuple[int, int]:
    parts = s.split(":")
    return int(parts[0]), int(parts[1])


def in_window(
    t: datetime,
    start: str,
    end: str,
    days: Optional[List[str]] = None,
    grace_s: float = 0.0,
) -> bool:
    """
    Checks if datetime t falls within [start, end) on allowed days,
    optionally tolerating grace_s seconds.
    """
    for probe in (t, t - timedelta(seconds=grace_s), t + timedelta(seconds=grace_s)):
        lt = probe.astimezone(IST)
        if days is not None and DAYS[lt.weekday()] not in days:
            continue
        sh, sm = parse_hm(start)
        eh, em = parse_hm(end)
        s = lt.replace(hour=sh, minute=sm, second=0, microsecond=0)
        e = lt.replace(hour=eh, minute=em, second=0, microsecond=0)
        if s <= lt < e:
            return True
    return False


def is_callable_now(
    t: datetime,
    camp_window: WindowConfig,
    reg_window: WindowConfig,
    buffer_seconds: float = 30.0,
) -> bool:
    """
    Returns True if t is safely inside both the campaign window and regulatory window.
    Applies a safety buffer before window close to avoid edge-of-window race conditions.
    """
    lt = t.astimezone(IST)
    day_name = DAYS[lt.weekday()]
    if camp_window.days is not None and day_name not in camp_window.days:
        return False

    c_sh, c_sm = parse_hm(camp_window.start)
    c_eh, c_em = parse_hm(camp_window.end)
    c_start = lt.replace(hour=c_sh, minute=c_sm, second=0, microsecond=0)
    c_end = lt.replace(hour=c_eh, minute=c_em, second=0, microsecond=0) - timedelta(seconds=buffer_seconds)

    if not (c_start <= lt < c_end):
        return False

    r_sh, r_sm = parse_hm(reg_window.start)
    r_eh, r_em = parse_hm(reg_window.end)
    r_start = lt.replace(hour=r_sh, minute=r_sm, second=0, microsecond=0)
    r_end = lt.replace(hour=r_eh, minute=r_em, second=0, microsecond=0) - timedelta(seconds=buffer_seconds)

    return r_start <= lt < r_end


def next_window_open(
    t: datetime,
    start: str,
    end: str,
    days: Optional[List[str]] = None,
) -> datetime:
    """
    Returns the earliest datetime >= t when the window [start, end) on allowed days is open.
    If t is already inside the window, returns t.
    """
    lt = t.astimezone(IST)
    allowed_days = days if days is not None else DAYS

    sh, sm = parse_hm(start)
    eh, em = parse_hm(end)

    for d in range(0, 14):  # search up to 2 weeks ahead
        day_date = (lt + timedelta(days=d)).replace(second=0, microsecond=0)
        day_name = DAYS[day_date.weekday()]
        if day_name not in allowed_days:
            continue

        s = day_date.replace(hour=sh, minute=sm)
        e = day_date.replace(hour=eh, minute=em)

        if d == 0:
            if lt < s:
                return s
            if lt < e:
                return lt
            # After window today, try next days
            continue
        return s

    return lt


def next_combined_open(
    t: datetime,
    camp_window: WindowConfig,
    reg_window: WindowConfig,
) -> datetime:
    """
    Finds the earliest instant >= t that is simultaneously inside the campaign window
    and the regulatory window.
    """
    current = t.astimezone(IST)
    for _ in range(30):  # At most 30 iterations to find mutual intersection
        cand_camp = next_window_open(current, camp_window.start, camp_window.end, camp_window.days)
        cand_reg = next_window_open(current, reg_window.start, reg_window.end, None)

        if cand_camp == current and cand_reg == current:
            # Both open right now
            return current

        # Advance to whichever is later
        current = max(cand_camp, cand_reg)

        # Check if current is in both
        in_c = in_window(current, camp_window.start, camp_window.end, camp_window.days)
        in_r = in_window(current, reg_window.start, reg_window.end, None)
        if in_c and in_r:
            return current

    return current


def calculate_next_eligible_time(
    last_status: Optional[str],
    last_disposition: Optional[str],
    ended_at: Optional[datetime],
    callback_at: Optional[datetime],
    retry_policy: RetryPolicy,
    camp_window: WindowConfig,
    reg_window: WindowConfig,
) -> datetime:
    """
    Calculates the earliest next dial datetime according to rules R5 and R6,
    aligned to valid calling windows (R1).
    """
    if last_disposition == "callback_requested":
        if callback_at is None:
            raise ValueError("callback_requested disposition requires callback_at")
        target = callback_at.astimezone(IST)
    elif last_status in ("no_answer", "busy"):
        if ended_at is None:
            raise ValueError(f"{last_status} status requires ended_at")
        target = ended_at.astimezone(IST) + timedelta(minutes=retry_policy.no_answer_minutes)
    elif last_status == "failed":
        if ended_at is None:
            raise ValueError("failed status requires ended_at")
        target = ended_at.astimezone(IST) + timedelta(minutes=retry_policy.failed_minutes)
    else:
        # Fresh lead or no previous call
        target = datetime.now(IST) if ended_at is None else ended_at.astimezone(IST)

    return next_combined_open(target, camp_window, reg_window)
