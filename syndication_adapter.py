#!/usr/bin/env python3
"""
Adapter interface for outbound BoTTube syndication targets.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import requests

log = logging.getLogger("bottube-syndication-adapter")


@dataclass
class SyndicationResult:
    success: bool
    external_id: Optional[str] = None
    external_url: Optional[str] = None
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "external_id": self.external_id,
            "external_url": self.external_url,
            "error_message": self.error_message,
            "metadata": self.metadata,
        }


@dataclass
class SyndicationPayload:
    video_id: str
    video_title: str
    video_description: str
    video_url: str
    thumbnail_url: Optional[str]
    agent_id: int
    agent_name: str
    tags: list[str]
    metadata: Dict[str, Any] = field(default_factory=dict)


class SyndicationAdapter(ABC):
    platform_name = "base"

    def __init__(self, config: Dict[str, Any]):
        self.config = dict(config)
        self.timeout = int(self.config.get("timeout", 30))
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "BoTTube-Syndication/1.0"})

    @abstractmethod
    def validate_config(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def syndicate(self, payload: SyndicationPayload) -> SyndicationResult:
        raise NotImplementedError

    def close(self) -> None:
        self._session.close()


class MoltbookAdapter(SyndicationAdapter):
    """Adapter for Moltbook posting."""

    platform_name = "moltbook"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.base_url = str(config.get("base_url", "")).rstrip("/")
        self.api_key = str(config.get("api_key", ""))
        self.submolt = str(config.get("submolt", "m/general"))

    def validate_config(self) -> bool:
        if not self.base_url or not self.api_key:
            return False
        self._session.headers.update({"Authorization": f"Bearer {self.api_key}"})
        return True

    def syndicate(self, payload: SyndicationPayload) -> SyndicationResult:
        body = {
            "content": f"{payload.video_title}\n\n{payload.video_description}\n\n{payload.video_url}".strip(),
            "submolt": self.submolt,
            "metadata": {
                "source": "bottube",
                "video_id": payload.video_id,
                "agent_name": payload.agent_name,
                "tags": payload.tags,
            },
        }
        if payload.thumbnail_url:
            body["thumbnail_url"] = payload.thumbnail_url
        try:
            response = self._session.post(
                f"{self.base_url}/api/v1/posts",
                json=body,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload_json = response.json() if response.content else {}
            return SyndicationResult(
                success=True,
                external_id=str(payload_json.get("id") or payload_json.get("post_id") or ""),
                external_url=payload_json.get("url") or payload_json.get("post_url"),
                metadata={"response": payload_json},
            )
        except requests.RequestException as exc:
            return SyndicationResult(success=False, error_message=str(exc))


class TwitterAdapter(SyndicationAdapter):
    """Scaffold adapter for Twitter/X posting."""

    platform_name = "twitter"

    def validate_config(self) -> bool:
        required = ("api_key", "api_secret", "access_token", "access_token_secret")
        return all(self.config.get(key) for key in required)

    def syndicate(self, payload: SyndicationPayload) -> SyndicationResult:
        hashtags = " ".join(f"#{tag}" for tag in payload.tags[:3] if tag)
        text = f"{payload.video_title}\n\n{payload.video_url}".strip()
        if hashtags:
            text = f"{text}\n\n{hashtags}"
        return SyndicationResult(
            success=True,
            external_id=f"twitter:{payload.video_id}",
            external_url="https://twitter.com/i/web/status/mock",
            metadata={"tweet_text": text, "dry_run": True},
        )


class RSSFeedAdapter(SyndicationAdapter):
    """Adapter for static RSS feed updates."""

    platform_name = "rss_feed"

    def validate_config(self) -> bool:
        return bool(self.config.get("site_url"))

    def syndicate(self, payload: SyndicationPayload) -> SyndicationResult:
        return SyndicationResult(
            success=True,
            external_id=payload.video_id,
            external_url=payload.video_url,
            metadata={
                "feed_file": self.config.get("feed_file", "feed.xml"),
                "site_url": self.config.get("site_url"),
            },
        )


class PartnerAPIAdapter(SyndicationAdapter):
    """Generic JSON webhook adapter."""

    platform_name = "partner_api"

    def validate_config(self) -> bool:
        return bool(self.config.get("endpoint_url"))

    def syndicate(self, payload: SyndicationPayload) -> SyndicationResult:
        endpoint_url = str(self.config.get("endpoint_url", ""))
        headers = {}
        auth_header = self.config.get("auth_header")
        auth_value = self.config.get("auth_value")
        if auth_header and auth_value:
            headers[str(auth_header)] = str(auth_value)

        request_payload = {
            "title": payload.video_title,
            "description": payload.video_description,
            "url": payload.video_url,
            "thumbnail_url": payload.thumbnail_url,
            "tags": payload.tags,
            "source": "bottube",
            "video_id": payload.video_id,
            "agent_name": payload.agent_name,
        }

        try:
            response = self._session.post(
                endpoint_url,
                json=request_payload,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload_json = response.json() if response.content else {}
            return SyndicationResult(
                success=True,
                external_id=str(payload_json.get("id") or ""),
                external_url=payload_json.get("url"),
                metadata={"response": payload_json},
            )
        except requests.RequestException as exc:
            return SyndicationResult(success=False, error_message=str(exc))


ADAPTER_REGISTRY = {
    "moltbook": MoltbookAdapter,
    "twitter": TwitterAdapter,
    "rss_feed": RSSFeedAdapter,
    "partner_api": PartnerAPIAdapter,
}


def get_adapter(platform_name: str, config: Dict[str, Any]) -> SyndicationAdapter:
    adapter_class = ADAPTER_REGISTRY.get(platform_name)
    if adapter_class is None:
        raise ValueError(f"Unknown syndication platform: {platform_name}")
    return adapter_class(config)


def list_adapters() -> list[str]:
    return sorted(ADAPTER_REGISTRY.keys())
