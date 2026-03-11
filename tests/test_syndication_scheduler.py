import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from syndication_config import PlatformConfig, ScheduleConfig, SyndicationConfig
from syndication_scheduler import BatchProcessor, CronParser, RateLimiter, SyndicationScheduler


def test_cron_parser_matches_steps_and_next_run():
    cron = CronParser("*/5 * * * *")
    assert cron.matches(datetime(2026, 3, 10, 12, 0, 0)) is True
    assert cron.matches(datetime(2026, 3, 10, 12, 3, 0)) is False
    assert cron.next_run(datetime(2026, 3, 10, 12, 33, 0)) == datetime(2026, 3, 10, 12, 35, 0)


def test_rate_limiter_refills_and_separates_keys():
    limiter = RateLimiter(rate=2, window=1)
    assert limiter.acquire("moltbook") is True
    assert limiter.acquire("moltbook") is True
    assert limiter.acquire("moltbook") is False
    assert limiter.acquire("twitter") is True
    time.sleep(0.6)
    assert limiter.acquire("moltbook") is True


def test_scheduler_respects_quiet_hours_and_days():
    config = SyndicationConfig(
        enabled=True,
        platforms={"moltbook": PlatformConfig(enabled=True)},
        schedule=ScheduleConfig(
            enabled=True,
            cron_expression="* * * * *",
            quiet_hours_start="22:00",
            quiet_hours_end="06:00",
            days_of_week=[1, 2, 3, 4, 5],
        ),
    )
    scheduler = SyndicationScheduler(config)

    assert scheduler.should_run(datetime(2026, 3, 16, 12, 0, 0)) is True
    assert scheduler.should_run(datetime(2026, 3, 16, 23, 0, 0)) is False
    assert scheduler.should_run(datetime(2026, 3, 15, 12, 0, 0)) is False


def test_batch_processor_waits_between_batches():
    processor = BatchProcessor(batch_size=2, batch_delay=0.2)
    processor.record_processed()
    processor.record_processed()
    assert processor.should_process() is False

    start = time.time()
    processor.wait_if_needed()
    assert time.time() - start >= 0.15
    assert processor.should_process() is True
