"""
Simulated Clock utilities for the Campaign Dispatcher.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


class Clock(ABC):
    """Abstract clock interface."""

    @abstractmethod
    def now(self) -> datetime:
        """Returns the current simulated time (IST timezone-aware)."""
        raise NotImplementedError

    @abstractmethod
    def get_speed(self) -> float:
        """Returns the simulated-to-real speed ratio."""
        raise NotImplementedError

    def sim_to_real_seconds(self, sim_seconds: float) -> float:
        """Converts simulated seconds to real seconds."""
        speed = self.get_speed()
        if speed <= 0:
            return 0.0
        return max(0.0, sim_seconds / speed)

    def real_to_sim_seconds(self, real_seconds: float) -> float:
        """Converts real seconds to simulated seconds."""
        return max(0.0, real_seconds * self.get_speed())

    async def sleep_sim(self, sim_seconds: float) -> None:
        """Asynchronously sleep for a given number of simulated seconds."""
        real_sec = self.sim_to_real_seconds(sim_seconds)
        if real_sec > 0:
            await asyncio.sleep(real_sec)


class ProviderClock(Clock):
    """
    Clock synchronized with the mock telephony provider's clock.
    Uses formula: sim_now = sim_epoch + (time.time() - real_epoch) * speed.
    """

    def __init__(self, sim_epoch: datetime, real_epoch: float, speed: float):
        if sim_epoch.tzinfo is None:
            sim_epoch = sim_epoch.replace(tzinfo=IST)
        else:
            sim_epoch = sim_epoch.astimezone(IST)
        self.sim_epoch = sim_epoch
        self.real_epoch = real_epoch
        self.speed = speed

    @classmethod
    def from_clock_dict(cls, data: dict) -> ProviderClock:
        sim_epoch = datetime.fromisoformat(data["sim_epoch"])
        real_epoch = float(data["real_epoch"])
        speed = float(data["speed"])
        return cls(sim_epoch=sim_epoch, real_epoch=real_epoch, speed=speed)

    def update_from_dict(self, data: dict) -> None:
        sim_epoch = datetime.fromisoformat(data["sim_epoch"])
        if sim_epoch.tzinfo is None:
            sim_epoch = sim_epoch.replace(tzinfo=IST)
        else:
            sim_epoch = sim_epoch.astimezone(IST)
        self.sim_epoch = sim_epoch
        self.real_epoch = float(data["real_epoch"])
        self.speed = float(data["speed"])

    def now(self) -> datetime:
        elapsed_real = time.time() - self.real_epoch
        return self.sim_epoch + timedelta(seconds=elapsed_real * self.speed)

    def get_speed(self) -> float:
        return self.speed


class FakeClock(Clock):
    """
    Deterministic controllable clock for unit tests.
    """

    def __init__(self, current_time: datetime, speed: float = 1.0):
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=IST)
        else:
            current_time = current_time.astimezone(IST)
        self._current_time = current_time
        self._speed = speed

    def now(self) -> datetime:
        return self._current_time

    def set_time(self, new_time: datetime) -> None:
        if new_time.tzinfo is None:
            new_time = new_time.replace(tzinfo=IST)
        else:
            new_time = new_time.astimezone(IST)
        self._current_time = new_time

    def advance(self, duration: timedelta) -> None:
        self._current_time += duration

    def advance_seconds(self, seconds: float) -> None:
        self._current_time += timedelta(seconds=seconds)

    def get_speed(self) -> float:
        return self._speed

    async def sleep_sim(self, sim_seconds: float) -> None:
        # In unit tests, sleeping advances the fake clock immediately
        self.advance_seconds(sim_seconds)

