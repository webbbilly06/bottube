#!/usr/bin/env python3
"""
Scheduling helpers for the BoTTube syndication pipeline.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Optional, Set

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from syndication_config import PlatformConfig, SyndicationConfig


@dataclass
class RateLimitState:
    tokens: float = 0.0
    last_update: float = field(default_factory=time.time)


class CronParser:
    """Simple 5-field cron parser."""

    def __init__(self, expression: str):
        self.expression = expression
        self.fields = self._parse(expression)

    def _parse(self, expression: str) -> list[Set[int]]:
        parts = expression.strip().split()
        if len(parts) != 5:
            raise ValueError(f"Invalid cron expression: {expression} (expected 5 fields)")
        ranges = [
            (0, 59),
            (0, 23),
            (1, 31),
            (1, 12),
            (0, 6),
        ]
        return [self._parse_field(part, minimum, maximum) for part, (minimum, maximum) in zip(parts, ranges)]

    def _parse_field(self, field: str, minimum: int, maximum: int) -> Set[int]:
        values: Set[int] = set()
        for part in field.split(","):
            if part == "*":
                values.update(range(minimum, maximum + 1))
                continue
            if part.startswith("*/"):
                step = int(part[2:])
                if step < 1:
                    raise ValueError(f"Invalid step value: {step}")
                values.update(range(minimum, maximum + 1, step))
                continue
            if "-" in part:
                start, end = map(int, part.split("-", 1))
                if start > end or start < minimum or end > maximum:
                    raise ValueError(f"Invalid range: {part}")
                values.update(range(start, end + 1))
                continue
            value = int(part)
            if value < minimum or value > maximum:
                raise ValueError(f"Value {value} out of range [{minimum}, {maximum}]")
            values.add(value)
        return values

    def matches(self, dt: Optional[datetime] = None) -> bool:
        current = dt or datetime.now()
        cron_weekday = (current.weekday() + 1) % 7
        return (
            current.minute in self.fields[0]
            and current.hour in self.fields[1]
            and current.day in self.fields[2]
            and current.month in self.fields[3]
            and cron_weekday in self.fields[4]
        )

    def next_run(self, after: Optional[datetime] = None) -> datetime:
        probe = (after or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(366 * 24 * 60 * 4):
            if self.matches(probe):
                return probe
            probe += timedelta(minutes=1)
        raise ValueError("Could not find next run time within 4 years")


class RateLimiter:
    """Token bucket rate limiter."""

    def __init__(self, rate: int, window: int = 60):
        self.rate = rate
        self.window = window
        self._buckets: Dict[str, RateLimitState] = {}
        self._lock = threading.Lock()

    def _get_bucket(self, key: str) -> RateLimitState:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = RateLimitState(tokens=float(self.rate))
            self._buckets[key] = bucket
        return bucket

    def _refill(self, bucket: RateLimitState) -> None:
        now = time.time()
        elapsed = now - bucket.last_update
        bucket.tokens = min(float(self.rate), bucket.tokens + (elapsed * (self.rate / self.window)))
        bucket.last_update = now

    def acquire(self, key: str = "default", tokens: int = 1) -> bool:
        with self._lock:
            bucket = self._get_bucket(key)
            self._refill(bucket)
            if bucket.tokens >= tokens:
                bucket.tokens -= tokens
                return True
            return False

    def refund(self, key: str = "default", tokens: int = 1) -> None:
        with self._lock:
            bucket = self._get_bucket(key)
            bucket.tokens = min(float(self.rate), bucket.tokens + tokens)

    def get_wait_time(self, key: str = "default", tokens: int = 1) -> float:
        with self._lock:
            bucket = self._get_bucket(key)
            self._refill(bucket)
            if bucket.tokens >= tokens:
                return 0.0
            missing = tokens - bucket.tokens
            return missing * (self.window / self.rate)

    def wait_for_token(
        self,
        key: str = "default",
        tokens: int = 1,
        timeout: Optional[float] = None,
    ) -> bool:
        start = time.time()
        while True:
            if self.acquire(key, tokens=tokens):
                return True
            if timeout is not None and (time.time() - start) >= timeout:
                return False
            time.sleep(min(self.get_wait_time(key, tokens=tokens), 0.1))


class SyndicationScheduler:
    """Cron, quiet-hours, and rate-limit controls."""

    def __init__(self, config: SyndicationConfig):
        self.config = config
        self.schedule_config = config.schedule
        self._cron = CronParser(self.schedule_config.cron_expression)
        self._global_limiter = RateLimiter(config.global_rate_limit, window=60)
        self._platform_limiters: Dict[str, RateLimiter] = {}
        for platform_name, platform_config in config.platforms.items():
            self.sync_platform(platform_name, platform_config)

    def _now(self) -> datetime:
        if ZoneInfo is None:
            return datetime.now()
        try:
            return datetime.now(ZoneInfo(self.schedule_config.timezone))
        except Exception:
            return datetime.now()

    def should_run(self, dt: Optional[datetime] = None) -> bool:
        if not self.config.enabled:
            return False
        if not self.schedule_config.enabled:
            return True
        current = dt or self._now()
        if not self._cron.matches(current):
            return False
        if self._is_quiet_hours(current):
            return False
        if not self._is_valid_day(current):
            return False
        return True

    def _is_quiet_hours(self, dt: datetime) -> bool:
        start = self.schedule_config.quiet_hours_start
        end = self.schedule_config.quiet_hours_end
        if not start or not end:
            return False
        try:
            start_hour, start_minute = map(int, start.split(":"))
            end_hour, end_minute = map(int, end.split(":"))
        except (TypeError, ValueError):
            return False

        current_minutes = dt.hour * 60 + dt.minute
        start_minutes = start_hour * 60 + start_minute
        end_minutes = end_hour * 60 + end_minute

        if start_minutes == end_minutes:
            return True
        if start_minutes < end_minutes:
            return start_minutes <= current_minutes < end_minutes
        return current_minutes >= start_minutes or current_minutes < end_minutes

    def _is_valid_day(self, dt: datetime) -> bool:
        days = self.schedule_config.days_of_week
        if not days:
            return True
        cron_weekday = (dt.weekday() + 1) % 7
        return cron_weekday in days

    def sync_platform(self, platform_name: str, platform_config: PlatformConfig) -> None:
        self._platform_limiters[platform_name] = RateLimiter(
            platform_config.rate_limit,
            window=platform_config.rate_limit_window,
        )

    def acquire_rate_limit(self, platform_name: str) -> bool:
        if not self._global_limiter.acquire("global"):
            return False
        limiter = self._platform_limiters.get(platform_name)
        if limiter and not limiter.acquire(platform_name):
            self._global_limiter.refund("global")
            return False
        return True

    def wait_for_rate_limit(self, platform_name: str, timeout: Optional[float] = None) -> bool:
        if not self._global_limiter.wait_for_token("global", timeout=timeout):
            return False
        limiter = self._platform_limiters.get(platform_name)
        if limiter and not limiter.wait_for_token(platform_name, timeout=timeout):
            self._global_limiter.refund("global")
            return False
        return True

    def get_next_run_time(self, after: Optional[datetime] = None) -> datetime:
        return self._cron.next_run(after or self._now())

    def get_rate_limit_wait_time(self, platform_name: str) -> float:
        wait_time = self._global_limiter.get_wait_time("global")
        limiter = self._platform_limiters.get(platform_name)
        if limiter:
            wait_time = max(wait_time, limiter.get_wait_time(platform_name))
        return wait_time


class BatchProcessor:
    """Spread processing across small batches."""

    def __init__(self, batch_size: int = 10, batch_delay: float = 5.0):
        self.batch_size = batch_size
        self.batch_delay = batch_delay
        self._processed_count = 0
        self._batch_start_time: Optional[float] = None

    def should_process(self) -> bool:
        if self._processed_count < self.batch_size:
            return True
        if self._batch_start_time is None:
            return True
        return (time.time() - self._batch_start_time) >= self.batch_delay

    def wait_if_needed(self) -> None:
        if self._processed_count >= self.batch_size and self._batch_start_time is not None:
            elapsed = time.time() - self._batch_start_time
            if elapsed < self.batch_delay:
                time.sleep(self.batch_delay - elapsed)
        self._processed_count = 0
        self._batch_start_time = time.time()

    def record_processed(self) -> None:
        if self._batch_start_time is None:
            self._batch_start_time = time.time()
        self._processed_count += 1

    def reset(self) -> None:
        self._processed_count = 0
        self._batch_start_time = None


def create_scheduler(config: SyndicationConfig) -> SyndicationScheduler:
    return SyndicationScheduler(config)


def create_batch_processor(config: SyndicationConfig) -> BatchProcessor:
    return BatchProcessor(
        batch_size=config.schedule.batch_size,
        batch_delay=config.schedule.batch_delay,
    )
