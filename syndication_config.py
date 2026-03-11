#!/usr/bin/env python3
"""
Configuration management for the BoTTube syndication pipeline.
"""

from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

log = logging.getLogger("bottube-syndication-config")

_PLATFORM_FIELDS = {
    "enabled",
    "priority",
    "rate_limit",
    "rate_limit_window",
    "retry_count",
    "retry_backoff",
    "timeout",
    "config",
}
_PLATFORM_DEFAULT_PRIORITIES = {
    "moltbook": 10,
    "twitter": 5,
    "rss_feed": 0,
    "partner_api": 0,
}


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_days_of_week(value: Any) -> list[int]:
    if value is None:
        return [0, 1, 2, 3, 4, 5, 6]
    if isinstance(value, (list, tuple)):
        return [int(day) for day in value]
    if isinstance(value, str):
        return [int(part.strip()) for part in value.split(",") if part.strip()]
    return [0, 1, 2, 3, 4, 5, 6]


def _expand_env_values(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {key: _expand_env_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env_values(item) for item in value]
    return value


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _normalize_platform_dict(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    payload = dict(data or {})
    normalized: Dict[str, Any] = {}
    config_blob = payload.get("config", {})
    if not isinstance(config_blob, dict):
        config_blob = {}

    for key, value in payload.items():
        if key == "config":
            continue
        if key in _PLATFORM_FIELDS:
            normalized[key] = value
        else:
            config_blob[key] = value

    if config_blob:
        normalized["config"] = config_blob
    return normalized


def _platform_from_dict(data: Optional[Dict[str, Any]], *, default_priority: int = 0) -> "PlatformConfig":
    payload = _normalize_platform_dict(data)
    return PlatformConfig(
        enabled=_parse_bool(payload.get("enabled", True)),
        priority=_parse_int(payload.get("priority", default_priority), default_priority),
        rate_limit=_parse_int(payload.get("rate_limit", 60), 60),
        rate_limit_window=_parse_int(payload.get("rate_limit_window", 60), 60),
        retry_count=_parse_int(payload.get("retry_count", 3), 3),
        retry_backoff=_parse_float(payload.get("retry_backoff", 2.0), 2.0),
        timeout=_parse_int(payload.get("timeout", 30), 30),
        config=dict(payload.get("config", {})),
    )


def _platform_override_from_dict(data: Optional[Dict[str, Any]]) -> "PlatformOverrideConfig":
    payload = _normalize_platform_dict(data)
    return PlatformOverrideConfig(
        enabled=payload.get("enabled"),
        priority=_parse_int(payload["priority"], 0) if "priority" in payload else None,
        rate_limit=_parse_int(payload["rate_limit"], 0) if "rate_limit" in payload else None,
        rate_limit_window=_parse_int(payload["rate_limit_window"], 0) if "rate_limit_window" in payload else None,
        retry_count=_parse_int(payload["retry_count"], 0) if "retry_count" in payload else None,
        retry_backoff=_parse_float(payload["retry_backoff"], 0.0) if "retry_backoff" in payload else None,
        timeout=_parse_int(payload["timeout"], 0) if "timeout" in payload else None,
        config=dict(payload.get("config", {})),
    )


@dataclass
class PlatformConfig:
    """Configuration for a single syndication platform."""

    enabled: bool = True
    priority: int = 0
    rate_limit: int = 60
    rate_limit_window: int = 60
    retry_count: int = 3
    retry_backoff: float = 2.0
    timeout: int = 30
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PlatformOverrideConfig:
    """Per-agent overrides for a platform."""

    enabled: Optional[bool] = None
    priority: Optional[int] = None
    rate_limit: Optional[int] = None
    rate_limit_window: Optional[int] = None
    retry_count: Optional[int] = None
    retry_backoff: Optional[float] = None
    timeout: Optional[int] = None
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentOverrideConfig:
    """Per-agent syndication overrides."""

    enabled: bool = True
    jitter_seconds: Optional[int] = None
    platforms: Dict[str, PlatformOverrideConfig] = field(default_factory=dict)


@dataclass
class ScheduleConfig:
    """Scheduling configuration for the poller."""

    enabled: bool = True
    cron_expression: str = "* * * * *"
    timezone: str = "UTC"
    batch_size: int = 10
    batch_delay: int = 5
    jitter_seconds: int = 0
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    days_of_week: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])


@dataclass
class SyndicationConfig:
    """Top-level syndication configuration."""

    enabled: bool = True
    poll_interval: int = 60
    platforms: Dict[str, PlatformConfig] = field(default_factory=dict)
    agents: Dict[str, AgentOverrideConfig] = field(default_factory=dict)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    global_rate_limit: int = 100
    global_timeout: int = 300
    log_level: str = "INFO"
    config_file: Optional[str] = None

    def get_platform(self, name: str) -> Optional[PlatformConfig]:
        return self.platforms.get((name or "").strip().lower())

    def get_enabled_platforms(self) -> list[str]:
        return [
            name for name, platform in self.platforms.items()
            if platform.enabled
        ]

    def get_agent_override(
        self,
        *,
        agent_name: Optional[str] = None,
        agent_id: Optional[int] = None,
    ) -> Optional[AgentOverrideConfig]:
        candidates = []
        if agent_name:
            candidates.extend([
                agent_name,
                agent_name.lower(),
                f"agent:{agent_name}",
                f"agent:{agent_name.lower()}",
            ])
        if agent_id is not None:
            candidates.extend([str(agent_id), f"id:{agent_id}"])
        for key in candidates:
            override = self.agents.get(key)
            if override:
                return override
        return None


class ConfigValidationError(Exception):
    """Raised when syndication configuration is invalid."""


class SyndicationConfigManager:
    """Load and validate syndication configuration."""

    def __init__(self, config_dir: Optional[str] = None):
        self.config_dir = Path(config_dir) if config_dir else self._default_config_dir()
        self._config_path: Optional[Path] = None
        self._file_mtime = 0.0
        self.config = self._dict_to_config(self._default_config_dict(), None)

    def _default_config_dir(self) -> Path:
        env_dir = os.environ.get("BOTTUBE_BASE_DIR")
        if env_dir:
            return Path(env_dir)
        return Path.cwd()

    def _default_platform_dict(self) -> Dict[str, Dict[str, Any]]:
        legacy_platforms = [
            item.strip().lower()
            for item in os.environ.get("SYNDICATION_PLATFORMS", "moltbook,twitter").split(",")
            if item.strip()
        ]
        defaults: Dict[str, Dict[str, Any]] = {}
        for platform_name in legacy_platforms:
            defaults[platform_name] = {
                "enabled": True,
                "priority": _PLATFORM_DEFAULT_PRIORITIES.get(platform_name, 0),
            }
        return defaults

    def _default_config_dict(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "poll_interval": _parse_int(os.environ.get("POLL_INTERVAL_SEC", 60), 60),
            "platforms": self._default_platform_dict(),
            "agents": {},
            "schedule": {
                "enabled": True,
                "cron_expression": "* * * * *",
                "timezone": "UTC",
                "batch_size": 10,
                "batch_delay": 5,
                "jitter_seconds": 0,
                "days_of_week": [0, 1, 2, 3, 4, 5, 6],
            },
            "global_rate_limit": 100,
            "global_timeout": 300,
            "log_level": os.environ.get(
                "BOTTUBE_SYNDICATION_LOG_LEVEL",
                os.environ.get("LOG_LEVEL", "INFO"),
            ),
        }

    def load(self, config_file: Optional[str] = None) -> SyndicationConfig:
        config_path = self._find_config_file(config_file)
        file_config: Dict[str, Any] = {}

        if config_path:
            file_config = self._load_file(config_path)
            self._config_path = config_path
            self._file_mtime = config_path.stat().st_mtime
        else:
            self._config_path = None
            self._file_mtime = 0.0

        merged = self._merge_configs(file_config)
        self.config = self._dict_to_config(merged, config_path)
        self.validate(self.config)
        log.info("Loaded syndication configuration from %s", config_path or "defaults + environment")
        return self.config

    def reload(self) -> SyndicationConfig:
        if not self._config_path:
            return self.config
        try:
            current_mtime = self._config_path.stat().st_mtime
        except OSError:
            return self.config
        if current_mtime != self._file_mtime:
            return self.load(str(self._config_path))
        return self.config

    def get_config(self) -> SyndicationConfig:
        return self.config

    def validate(self, config: Optional[SyndicationConfig] = None) -> None:
        target = config or self.config
        errors = []

        if target.poll_interval < 1:
            errors.append("poll_interval must be >= 1 second")
        if target.poll_interval > 3600:
            errors.append("poll_interval must be <= 3600 seconds")
        if target.global_rate_limit < 1:
            errors.append("global_rate_limit must be >= 1")
        if target.global_timeout < 1:
            errors.append("global_timeout must be >= 1")

        for name, platform in target.platforms.items():
            if platform.rate_limit < 1:
                errors.append(f"[{name}] rate_limit must be >= 1")
            if platform.rate_limit_window < 1:
                errors.append(f"[{name}] rate_limit_window must be >= 1")
            if platform.retry_count < 0:
                errors.append(f"[{name}] retry_count must be >= 0")
            if platform.retry_backoff < 1.0:
                errors.append(f"[{name}] retry_backoff must be >= 1.0")
            if platform.timeout < 1:
                errors.append(f"[{name}] timeout must be >= 1 second")

        for agent_key, agent_override in target.agents.items():
            if agent_override.jitter_seconds is not None and agent_override.jitter_seconds < 0:
                errors.append(f"[agents.{agent_key}] jitter_seconds must be >= 0")
            for platform_name, platform_override in agent_override.platforms.items():
                if platform_override.rate_limit is not None and platform_override.rate_limit < 1:
                    errors.append(f"[agents.{agent_key}.{platform_name}] rate_limit must be >= 1")
                if (
                    platform_override.rate_limit_window is not None
                    and platform_override.rate_limit_window < 1
                ):
                    errors.append(f"[agents.{agent_key}.{platform_name}] rate_limit_window must be >= 1")
                if platform_override.retry_count is not None and platform_override.retry_count < 0:
                    errors.append(f"[agents.{agent_key}.{platform_name}] retry_count must be >= 0")
                if (
                    platform_override.retry_backoff is not None
                    and platform_override.retry_backoff < 1.0
                ):
                    errors.append(f"[agents.{agent_key}.{platform_name}] retry_backoff must be >= 1.0")
                if platform_override.timeout is not None and platform_override.timeout < 1:
                    errors.append(f"[agents.{agent_key}.{platform_name}] timeout must be >= 1 second")

        schedule = target.schedule
        if schedule.batch_size < 1:
            errors.append("schedule.batch_size must be >= 1")
        if schedule.batch_delay < 0:
            errors.append("schedule.batch_delay must be >= 0")
        if schedule.jitter_seconds < 0:
            errors.append("schedule.jitter_seconds must be >= 0")
        if schedule.days_of_week and not all(0 <= day <= 6 for day in schedule.days_of_week):
            errors.append("schedule.days_of_week must contain values from 0-6")
        try:
            from syndication_scheduler import CronParser

            CronParser(schedule.cron_expression)
        except Exception as exc:
            errors.append(f"schedule.cron_expression is invalid: {exc}")

        if errors:
            raise ConfigValidationError(
                "Configuration validation failed:\n" + "\n".join(f"  - {item}" for item in errors)
            )

    def _find_config_file(self, config_file: Optional[str]) -> Optional[Path]:
        if config_file:
            requested = Path(config_file)
            if requested.is_absolute():
                return requested if requested.exists() else None
            candidate = (self.config_dir / requested).resolve()
            if candidate.exists():
                return candidate
            if requested.exists():
                return requested.resolve()
            return None

        for name in ("syndication.yaml", "syndication.yml", "syndication.json"):
            candidate = self.config_dir / name
            if candidate.exists():
                return candidate.resolve()
        return None

    def _load_file(self, path: Path) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()

        if path.suffix in {".yaml", ".yml"}:
            return yaml.safe_load(content) or {}
        if path.suffix == ".json":
            return json.loads(content) if content.strip() else {}
        raise ConfigValidationError(f"Unsupported config file extension: {path.suffix}")

    def _merge_configs(self, file_config: Dict[str, Any]) -> Dict[str, Any]:
        config = deepcopy(self._default_config_dict())
        _deep_merge(config, _expand_env_values(file_config))
        self._apply_env_overrides(config)
        return config

    def _apply_env_overrides(self, config: Dict[str, Any]) -> None:
        prefix = "BOTTUBE_SYNDICATION_"
        env_map = {
            f"{prefix}ENABLED": ("enabled", _parse_bool),
            f"{prefix}POLL_INTERVAL": ("poll_interval", lambda v: _parse_int(v, config["poll_interval"])),
            f"{prefix}GLOBAL_RATE_LIMIT": ("global_rate_limit", lambda v: _parse_int(v, config["global_rate_limit"])),
            f"{prefix}GLOBAL_TIMEOUT": ("global_timeout", lambda v: _parse_int(v, config["global_timeout"])),
            f"{prefix}LOG_LEVEL": ("log_level", str),
        }
        for env_var, (config_key, converter) in env_map.items():
            value = os.environ.get(env_var)
            if value is None:
                continue
            config[config_key] = converter(value)

        for env_var, value in os.environ.items():
            if env_var.startswith(f"{prefix}PLATFORM_"):
                remainder = env_var[len(f"{prefix}PLATFORM_"):]
                platform_name, config_key = self._split_platform_env_key(
                    remainder,
                    config.get("platforms", {}).keys(),
                )
                if not platform_name or not config_key:
                    continue
                platform = config.setdefault("platforms", {}).setdefault(platform_name, {})
                self._apply_platform_env_override(platform, config_key, value)
            elif env_var.startswith(f"{prefix}SCHEDULE_"):
                schedule_key = env_var[len(f"{prefix}SCHEDULE_"):].lower()
                schedule = config.setdefault("schedule", {})
                if schedule_key == "days_of_week":
                    schedule[schedule_key] = _parse_days_of_week(value)
                elif schedule_key in {"enabled"}:
                    schedule[schedule_key] = _parse_bool(value)
                elif schedule_key in {"batch_size", "batch_delay", "jitter_seconds"}:
                    schedule[schedule_key] = _parse_int(value, 0)
                else:
                    schedule[schedule_key] = value

    def _apply_platform_env_override(
        self,
        platform: Dict[str, Any],
        config_key: str,
        raw_value: str,
    ) -> None:
        if config_key == "enabled":
            platform["enabled"] = _parse_bool(raw_value)
            return
        if config_key in {"priority", "rate_limit", "rate_limit_window", "retry_count", "timeout"}:
            platform[config_key] = _parse_int(raw_value, 0)
            return
        if config_key == "retry_backoff":
            platform[config_key] = _parse_float(raw_value, 0.0)
            return
        if config_key.startswith("config__"):
            nested_key = config_key.split("config__", 1)[1].lower()
            platform.setdefault("config", {})[nested_key] = raw_value
            return
        platform.setdefault("config", {})[config_key] = raw_value

    def _split_platform_env_key(
        self,
        remainder: str,
        platform_names,
    ) -> tuple[Optional[str], Optional[str]]:
        normalized_names = sorted(
            {str(name).strip().lower() for name in platform_names if str(name).strip()},
            key=len,
            reverse=True,
        )
        remainder_upper = remainder.upper()
        for name in normalized_names:
            marker = f"{name.upper()}_"
            if remainder_upper.startswith(marker):
                return name, remainder[len(marker):].lower()
        parts = remainder.split("_", 1)
        if len(parts) == 2:
            return parts[0].lower(), parts[1].lower()
        return None, None

    def _dict_to_config(
        self,
        data: Dict[str, Any],
        config_path: Optional[Path],
    ) -> SyndicationConfig:
        platforms: Dict[str, PlatformConfig] = {}
        for platform_name, platform_data in (data.get("platforms") or {}).items():
            name = str(platform_name).strip().lower()
            platforms[name] = _platform_from_dict(
                platform_data,
                default_priority=_PLATFORM_DEFAULT_PRIORITIES.get(name, 0),
            )

        agents: Dict[str, AgentOverrideConfig] = {}
        for agent_key, agent_data in (data.get("agents") or {}).items():
            if not isinstance(agent_data, dict):
                continue
            overrides: Dict[str, PlatformOverrideConfig] = {}
            for platform_name, platform_data in (agent_data.get("platforms") or {}).items():
                overrides[str(platform_name).strip().lower()] = _platform_override_from_dict(platform_data)
            normalized_agent_key = str(agent_key)
            agents[normalized_agent_key] = AgentOverrideConfig(
                enabled=_parse_bool(agent_data.get("enabled", True)),
                jitter_seconds=(
                    _parse_int(agent_data["jitter_seconds"], 0)
                    if "jitter_seconds" in agent_data
                    else None
                ),
                platforms=overrides,
            )
            lowered_key = normalized_agent_key.lower()
            if lowered_key not in agents:
                agents[lowered_key] = agents[normalized_agent_key]

        schedule_data = data.get("schedule") or {}
        schedule = ScheduleConfig(
            enabled=_parse_bool(schedule_data.get("enabled", True)),
            cron_expression=str(schedule_data.get("cron_expression", "* * * * *")),
            timezone=str(schedule_data.get("timezone", "UTC")),
            batch_size=_parse_int(schedule_data.get("batch_size", 10), 10),
            batch_delay=_parse_int(schedule_data.get("batch_delay", 5), 5),
            jitter_seconds=_parse_int(schedule_data.get("jitter_seconds", 0), 0),
            quiet_hours_start=schedule_data.get("quiet_hours_start"),
            quiet_hours_end=schedule_data.get("quiet_hours_end"),
            days_of_week=_parse_days_of_week(schedule_data.get("days_of_week")),
        )

        return SyndicationConfig(
            enabled=_parse_bool(data.get("enabled", True)),
            poll_interval=_parse_int(data.get("poll_interval", 60), 60),
            platforms=platforms,
            agents=agents,
            schedule=schedule,
            global_rate_limit=_parse_int(data.get("global_rate_limit", 100), 100),
            global_timeout=_parse_int(data.get("global_timeout", 300), 300),
            log_level=str(data.get("log_level", "INFO")).upper(),
            config_file=str(config_path) if config_path else None,
        )


_config_manager: Optional[SyndicationConfigManager] = None


def get_config_manager(config_dir: Optional[str] = None) -> SyndicationConfigManager:
    global _config_manager
    if _config_manager is None:
        _config_manager = SyndicationConfigManager(config_dir=config_dir)
    return _config_manager


def load_config(config_file: Optional[str] = None) -> SyndicationConfig:
    return get_config_manager().load(config_file)


def get_config() -> SyndicationConfig:
    return get_config_manager().get_config()


def reload_config() -> SyndicationConfig:
    return get_config_manager().reload()
