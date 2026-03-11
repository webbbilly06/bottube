#!/usr/bin/env python3
"""
BoTTube syndication queue poller.
"""

from __future__ import annotations

import logging
import os
import random
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from syndication_adapter import SyndicationPayload, get_adapter
from syndication_config import (
    AgentOverrideConfig,
    PlatformConfig,
    PlatformOverrideConfig,
    SyndicationConfig,
    SyndicationConfigManager,
)
from syndication_queue import SyndicationQueue
from syndication_scheduler import create_batch_processor, create_scheduler


BOTTUBE_URL = os.environ.get("BOTTUBE_URL", "http://localhost:8097")
BOTTUBE_API_KEY = os.environ.get("BOTTUBE_API_KEY", "")
BOTTUBE_DB_PATH = os.environ.get(
    "BOTTUBE_DB_PATH",
    os.environ.get("BOTTUBE_BASE_DIR", str(ROOT)) + "/bottube.db",
)
CONFIG_FILE = os.environ.get("BOTTUBE_SYNDICATION_CONFIG", "")
LOG_LEVEL = os.environ.get(
    "BOTTUBE_SYNDICATION_LOG_LEVEL",
    os.environ.get("LOG_LEVEL", "INFO"),
)

INITIAL_BACKOFF_SEC = 5
MAX_BACKOFF_SEC = 300
BACKOFF_MULTIPLIER = 2.0
JITTER_FACTOR = 0.1

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bottube-syndication-poller")


@dataclass
class VideoInfo:
    video_id: str
    title: str
    agent_id: int
    agent_name: str
    created_at: float


class SyndicationPoller:
    """Poll BoTTube for new videos and push them through the syndication queue."""

    def __init__(
        self,
        bottube_url: str = BOTTUBE_URL,
        api_key: str = BOTTUBE_API_KEY,
        db_path: str = BOTTUBE_DB_PATH,
        config_file: Optional[str] = None,
    ):
        self.bottube_url = bottube_url.rstrip("/")
        self.api_key = api_key
        self.db_path = db_path
        self.config_file = config_file or CONFIG_FILE or None
        self.config_manager = SyndicationConfigManager(
            config_dir=os.environ.get("BOTTUBE_BASE_DIR", str(ROOT))
        )

        self.queue = SyndicationQueue(db_path)
        self.adapters: Dict[str, Any] = {}
        self.running = False
        self.known_video_ids: set[str] = set()
        self.last_poll_time = 0.0
        self.backoff_until = 0.0
        self.consecutive_failures = 0
        self.config_reload_interval = 300
        self._last_config_reload = time.time()

        initial_config = self.config_manager.load(self.config_file)
        self._apply_runtime_config(initial_config)

        signal.signal(signal.SIGTERM, self._shutdown_handler)
        signal.signal(signal.SIGINT, self._shutdown_handler)

    def _shutdown_handler(self, signum, frame) -> None:  # pragma: no cover
        log.info("Shutdown signal received (%s), stopping...", signum)
        self.running = False

    def _api_request(
        self,
        endpoint: str,
        method: str = "GET",
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        timeout: int = 30,
    ) -> Optional[requests.Response]:
        url = f"{self.bottube_url}{endpoint}"
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        try:
            if method == "GET":
                return requests.get(url, headers=headers, params=params, timeout=timeout)
            if method == "POST":
                return requests.post(url, headers=headers, json=data, timeout=timeout)
        except requests.RequestException as exc:
            log.warning("API request failed: %s", exc)
        return None

    def _apply_runtime_config(self, config: SyndicationConfig) -> None:
        self.config = config
        log.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))
        self.scheduler = create_scheduler(config)
        self.batch_processor = create_batch_processor(config)
        self._rebuild_adapters()

    def _rebuild_adapters(self) -> None:
        for adapter in self.adapters.values():
            try:
                adapter.close()
            except Exception:
                pass
        self.adapters = {}
        for platform_name in self.config.get_enabled_platforms():
            platform_config = self.config.get_platform(platform_name)
            if platform_config is None:
                continue
            try:
                adapter_config = dict(platform_config.config)
                adapter_config.setdefault("timeout", platform_config.timeout)
                adapter = get_adapter(platform_name, adapter_config)
            except ValueError:
                continue
            if adapter.validate_config():
                self.adapters[platform_name] = adapter

    def reload_runtime_config(self) -> bool:
        config = self.config_manager.reload()
        if config is self.config:
            return False
        self._apply_runtime_config(config)
        return True

    def _merge_platform_config(
        self,
        base: PlatformConfig,
        override: Optional[PlatformOverrideConfig],
    ) -> PlatformConfig:
        merged = PlatformConfig(
            enabled=base.enabled,
            priority=base.priority,
            rate_limit=base.rate_limit,
            rate_limit_window=base.rate_limit_window,
            retry_count=base.retry_count,
            retry_backoff=base.retry_backoff,
            timeout=base.timeout,
            config=dict(base.config),
        )
        if override is None:
            return merged
        if override.enabled is not None:
            merged.enabled = bool(override.enabled)
        if override.priority is not None:
            merged.priority = override.priority
        if override.rate_limit is not None:
            merged.rate_limit = override.rate_limit
        if override.rate_limit_window is not None:
            merged.rate_limit_window = override.rate_limit_window
        if override.retry_count is not None:
            merged.retry_count = override.retry_count
        if override.retry_backoff is not None:
            merged.retry_backoff = override.retry_backoff
        if override.timeout is not None:
            merged.timeout = override.timeout
        if override.config:
            merged.config.update(override.config)
        return merged

    def _get_agent_override(
        self,
        *,
        agent_name: Optional[str],
        agent_id: Optional[int],
    ) -> Optional[AgentOverrideConfig]:
        return self.config.get_agent_override(agent_name=agent_name, agent_id=agent_id)

    def _resolve_platform_config(
        self,
        platform_name: str,
        *,
        agent_name: Optional[str] = None,
        agent_id: Optional[int] = None,
    ) -> Optional[PlatformConfig]:
        base = self.config.get_platform(platform_name)
        if base is None or not base.enabled:
            return None
        override = self._get_agent_override(agent_name=agent_name, agent_id=agent_id)
        if override is not None and not override.enabled:
            return None
        platform_override = override.platforms.get(platform_name) if override else None
        resolved = self._merge_platform_config(base, platform_override)
        if not resolved.enabled:
            return None
        return resolved

    def _get_jitter_seconds(self, *, agent_name: str, agent_id: int) -> int:
        override = self._get_agent_override(agent_name=agent_name, agent_id=agent_id)
        if override and override.jitter_seconds is not None:
            return override.jitter_seconds
        return self.config.schedule.jitter_seconds

    def fetch_new_videos(self, since: Optional[float] = None) -> List[VideoInfo]:
        params = {"per_page": 50}
        if since:
            params["since"] = str(since)
        response = self._api_request("/api/feed", params=params)
        if not response or response.status_code != 200:
            log.warning("Failed to fetch videos: %s", response.status_code if response else "no response")
            return []

        payload = response.json()
        new_videos = []
        for item in payload.get("videos", []):
            video_id = item.get("video_id")
            if not video_id or video_id in self.known_video_ids:
                continue
            self.known_video_ids.add(video_id)
            new_videos.append(
                VideoInfo(
                    video_id=video_id,
                    title=item.get("title", "Untitled"),
                    agent_id=int(item.get("agent_id", 0) or 0),
                    agent_name=item.get("agent_name", "unknown"),
                    created_at=float(item.get("created_at", time.time()) or time.time()),
                )
            )
        log.info("Fetched %d videos, %d new", len(payload.get("videos", [])), len(new_videos))
        return new_videos

    def _calculate_priority(self, video: VideoInfo, platform_config: PlatformConfig) -> int:
        priority = platform_config.priority
        age_hours = (time.time() - video.created_at) / 3600
        if age_hours < 1:
            priority += 20
        elif age_hours < 6:
            priority += 10
        return priority

    def queue_new_videos(self, videos: List[VideoInfo]) -> int:
        queued_count = 0
        for video in videos:
            for platform_name in self.config.platforms.keys():
                platform_config = self._resolve_platform_config(
                    platform_name,
                    agent_name=video.agent_name,
                    agent_id=video.agent_id,
                )
                if platform_config is None:
                    continue
                priority = self._calculate_priority(video, platform_config)
                metadata = {
                    "queued_by": "syndication_poller",
                    "video_created_at": video.created_at,
                    "platform_priority": platform_config.priority,
                }
                self.queue.enqueue(
                    video_id=video.video_id,
                    video_title=video.title,
                    agent_id=video.agent_id,
                    agent_name=video.agent_name,
                    target_platform=platform_name,
                    priority=priority,
                    metadata=metadata,
                )
                queued_count += 1
                log.info("Queued '%s' for %s (priority=%d)", video.title, platform_name, priority)
        return queued_count

    def _get_video_details(self, video_id: str) -> Dict[str, Any]:
        response = self._api_request(f"/api/videos/{video_id}")
        if response and response.status_code == 200:
            return response.json()
        return {}

    def _build_payload(self, item) -> SyndicationPayload:
        details = self._get_video_details(item.video_id)
        watch_url = details.get("watch_url") or f"/watch/{item.video_id}"
        thumbnail_url = details.get("thumbnail_url") or None
        return SyndicationPayload(
            video_id=item.video_id,
            video_title=details.get("title", item.video_title),
            video_description=details.get("description", ""),
            video_url=urljoin(f"{self.bottube_url}/", watch_url.lstrip("/")),
            thumbnail_url=urljoin(f"{self.bottube_url}/", thumbnail_url.lstrip("/")) if thumbnail_url else None,
            agent_id=item.agent_id,
            agent_name=item.agent_name,
            tags=list(details.get("tags", [])),
            metadata=dict(item.metadata),
        )

    def _apply_item_jitter(self, item) -> None:
        jitter_seconds = self._get_jitter_seconds(agent_name=item.agent_name, agent_id=item.agent_id)
        if jitter_seconds <= 0:
            return
        delay = random.uniform(0, float(jitter_seconds))
        if delay <= 0:
            return
        log.debug("Applying %.2fs syndication jitter for item %s", delay, item.id)
        time.sleep(delay)

    def _process_item_legacy(self, item) -> bool:
        handlers = {
            "moltbook": self._syndicate_to_moltbook,
            "twitter": self._syndicate_to_twitter,
            "rss_feed": self._syndicate_to_rss_feed,
        }
        handler = handlers.get(item.target_platform)
        if handler is None:
            self.queue.mark_completed(item.id, metadata={"skipped": True, "reason": "no handler"})
            return True
        try:
            result = handler(item)
        except Exception as exc:
            self.queue.mark_failed(item.id, str(exc))
            return False
        if result.get("success"):
            self.queue.mark_completed(item.id, metadata=result)
            return True
        self.queue.mark_failed(item.id, result.get("error", "Unknown error"))
        return False

    def _process_item(self, item) -> bool:
        platform_config = self._resolve_platform_config(
            item.target_platform,
            agent_name=item.agent_name,
            agent_id=item.agent_id,
        )
        if platform_config is None:
            self.queue.mark_completed(item.id, metadata={"skipped": True, "reason": "platform disabled"})
            return True

        self._apply_item_jitter(item)
        adapter = self.adapters.get(item.target_platform)
        if adapter is None:
            return self._process_item_legacy(item)

        payload = self._build_payload(item)
        try:
            result = adapter.syndicate(payload)
        except Exception as exc:
            self.queue.mark_failed(item.id, str(exc))
            return False
        if result.success:
            self.queue.mark_completed(item.id, metadata=result.to_dict())
            return True
        self.queue.mark_failed(item.id, result.error_message or "Unknown error")
        return False

    def process_pending_items(self) -> int:
        if not self.scheduler.should_run():
            return 0

        processed_count = 0
        for platform_name in self.config.get_enabled_platforms():
            platform_config = self.config.get_platform(platform_name)
            if platform_config is None:
                continue
            self.scheduler.sync_platform(platform_name, platform_config)
            if not self.scheduler.acquire_rate_limit(platform_name):
                continue
            if not self.batch_processor.should_process():
                self.batch_processor.wait_if_needed()

            item = self.queue.dequeue(target_platform=platform_name)
            if item is None:
                continue

            success = self._process_item(item)
            if success:
                self.batch_processor.record_processed()
                processed_count += 1
        return processed_count

    def _syndicate_to_moltbook(self, item) -> dict:
        time.sleep(0.1)
        return {
            "success": True,
            "platform": "moltbook",
            "external_id": f"moltbook_{item.video_id}",
        }

    def _syndicate_to_twitter(self, item) -> dict:
        time.sleep(0.1)
        return {
            "success": True,
            "platform": "twitter",
            "tweet_id": f"tweet_{item.video_id}",
        }

    def _syndicate_to_rss_feed(self, item) -> dict:
        time.sleep(0.1)
        return {
            "success": True,
            "platform": "rss_feed",
            "feed_entry_id": f"rss_{item.video_id}",
        }

    def apply_backoff(self) -> None:
        backoff_time = min(
            INITIAL_BACKOFF_SEC * (BACKOFF_MULTIPLIER ** self.consecutive_failures),
            MAX_BACKOFF_SEC,
        )
        jitter = backoff_time * JITTER_FACTOR * random.random()
        self.backoff_until = time.time() + backoff_time + jitter
        log.info("Applying backoff: %.1f seconds (failures=%d)", backoff_time + jitter, self.consecutive_failures)

    def _load_known_videos(self) -> None:
        try:
            response = self._api_request("/api/feed", params={"per_page": 100})
            if response and response.status_code == 200:
                for item in response.json().get("videos", []):
                    video_id = item.get("video_id")
                    if video_id:
                        self.known_video_ids.add(video_id)
                log.info("Loaded %d known video IDs", len(self.known_video_ids))
        except Exception as exc:
            log.warning("Could not load known videos: %s", exc)

    def run(self) -> None:  # pragma: no cover
        if not self.api_key:
            log.error("BOTTUBE_API_KEY not set, exiting")
            return

        self.running = True
        log.info("Starting syndication poller")
        log.info("  BoTTube URL: %s", self.bottube_url)
        log.info("  Database: %s", self.db_path)
        log.info("  Poll interval: %ds", self.config.poll_interval)
        log.info("  Enabled platforms: %s", ", ".join(self.config.get_enabled_platforms()))
        log.info("  Schedule: %s", self.config.schedule.cron_expression)

        self._load_known_videos()

        while self.running:
            try:
                if time.time() - self._last_config_reload >= self.config_reload_interval:
                    self.reload_runtime_config()
                    self._last_config_reload = time.time()

                if time.time() < self.backoff_until:
                    time.sleep(min(self.backoff_until - time.time(), 10))
                    continue

                new_videos = self.fetch_new_videos(
                    since=self.last_poll_time if self.last_poll_time > 0 else None
                )
                if new_videos:
                    queued = self.queue_new_videos(new_videos)
                    log.info("Queued %d new syndication items", queued)
                    self.consecutive_failures = 0
                else:
                    self.consecutive_failures = max(0, self.consecutive_failures - 1)

                self.last_poll_time = time.time()

                processed = self.process_pending_items()
                if processed:
                    log.info("Processed %d queue items", processed)

                if random.random() < 0.01:
                    deleted = self.queue.cleanup_old(days=30)
                    if deleted:
                        log.info("Cleaned up %d old queue items", deleted)

                if self.running:
                    time.sleep(self.config.poll_interval)
            except KeyboardInterrupt:
                break
            except Exception as exc:
                log.error("Poller error: %s", exc)
                self.consecutive_failures += 1
                self.apply_backoff()

        for adapter in self.adapters.values():
            try:
                adapter.close()
            except Exception:
                pass
        log.info("Syndication poller stopped")


def main() -> None:  # pragma: no cover
    SyndicationPoller().run()


if __name__ == "__main__":  # pragma: no cover
    main()
