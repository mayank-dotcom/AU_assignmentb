"""
Tests for simulated clock implementations.
"""
from datetime import datetime, timedelta, timezone

import pytest

from solution.clock import FakeClock, ProviderClock, IST


def test_fake_clock_advance():
    start = datetime(2026, 11, 12, 10, 0, tzinfo=IST)
    clock = FakeClock(start, speed=600.0)

    assert clock.now() == start
    assert clock.get_speed() == 600.0

    clock.advance(timedelta(minutes=30))
    assert clock.now() == datetime(2026, 11, 12, 10, 30, tzinfo=IST)

    clock.advance_seconds(120)
    assert clock.now() == datetime(2026, 11, 12, 10, 32, tzinfo=IST)


def test_clock_sim_to_real_conversions():
    start = datetime(2026, 11, 12, 10, 0, tzinfo=IST)
    clock = FakeClock(start, speed=600.0)

    # At 600x speed, 600 sim seconds = 1 real second
    assert clock.sim_to_real_seconds(600) == 1.0
    assert clock.sim_to_real_seconds(60) == 0.1
    assert clock.real_to_sim_seconds(1.0) == 600.0


def test_provider_clock_sync():
    data = {
        "sim_now": "2026-11-12T17:30:00+05:30",
        "speed": 600.0,
        "sim_epoch": "2026-11-12T17:30:00+05:30",
        "real_epoch": 1000.0,
    }
    pclock = ProviderClock.from_clock_dict(data)
    assert pclock.get_speed() == 600.0
    assert pclock.sim_epoch == datetime(2026, 11, 12, 17, 30, tzinfo=IST)

