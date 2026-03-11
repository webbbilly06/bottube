import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import syndication_config
from syndication_config import ConfigValidationError, SyndicationConfigManager


@pytest.fixture(autouse=True)
def reset_global_manager():
    syndication_config._config_manager = None
    yield
    syndication_config._config_manager = None


def test_load_yaml_config_supports_agent_overrides_and_env_expansion(tmp_path):
    config_path = tmp_path / "syndication.yaml"
    config_path.write_text(
        """
enabled: true
poll_interval: 120
platforms:
  moltbook:
    enabled: true
    priority: 10
    rate_limit: 30
    base_url: ${MOLTBOOK_BASE_URL}
    api_key: ${MOLTBOOK_API_KEY}
  twitter:
    enabled: true
    priority: 5
agents:
  quiet-bot:
    jitter_seconds: 12
    platforms:
      twitter:
        enabled: false
      moltbook:
        priority: 25
schedule:
  enabled: true
  cron_expression: "*/5 * * * *"
  jitter_seconds: 3
""",
        encoding="utf-8",
    )

    with patch.dict(
        os.environ,
        {
            "MOLTBOOK_BASE_URL": "https://moltbook.example",
            "MOLTBOOK_API_KEY": "secret-key",
        },
        clear=False,
    ):
        manager = SyndicationConfigManager(str(tmp_path))
        config = manager.load("syndication.yaml")

    assert config.poll_interval == 120
    assert config.platforms["moltbook"].config["base_url"] == "https://moltbook.example"
    assert config.platforms["moltbook"].config["api_key"] == "secret-key"
    assert config.schedule.jitter_seconds == 3

    quiet_bot = config.get_agent_override(agent_name="quiet-bot")
    assert quiet_bot is not None
    assert quiet_bot.jitter_seconds == 12
    assert quiet_bot.platforms["twitter"].enabled is False
    assert quiet_bot.platforms["moltbook"].priority == 25


def test_env_override_stores_unknown_platform_keys_under_config():
    with patch.dict(
        os.environ,
        {
            "SYNDICATION_PLATFORMS": "moltbook",
            "BOTTUBE_SYNDICATION_PLATFORM_MOLTBOOK_BASE_URL": "https://moltbook.example",
            "BOTTUBE_SYNDICATION_PLATFORM_MOLTBOOK_API_KEY": "abc123",
        },
        clear=False,
    ):
        manager = SyndicationConfigManager()
        config = manager.load()

    assert config.platforms["moltbook"].config["base_url"] == "https://moltbook.example"
    assert config.platforms["moltbook"].config["api_key"] == "abc123"


def test_env_override_handles_platform_names_with_underscores():
    with patch.dict(
        os.environ,
        {
            "SYNDICATION_PLATFORMS": "rss_feed",
            "BOTTUBE_SYNDICATION_PLATFORM_RSS_FEED_SITE_URL": "https://bottube.ai",
        },
        clear=False,
    ):
        manager = SyndicationConfigManager()
        config = manager.load()

    assert "rss_feed" in config.platforms
    assert config.platforms["rss_feed"].config["site_url"] == "https://bottube.ai"


def test_find_config_file_prefers_config_dir_for_relative_paths(tmp_path):
    config_path = tmp_path / "syndication.yaml"
    config_path.write_text("enabled: true\n", encoding="utf-8")

    manager = SyndicationConfigManager(str(tmp_path))
    resolved = manager._find_config_file("syndication.yaml")

    assert resolved == config_path.resolve()


def test_invalid_agent_override_rate_limit_fails_validation(tmp_path):
    config_path = tmp_path / "syndication.yaml"
    config_path.write_text(
        """
platforms:
  moltbook:
    enabled: true
agents:
  quiet-bot:
    platforms:
      moltbook:
        rate_limit: 0
""",
        encoding="utf-8",
    )

    manager = SyndicationConfigManager(str(tmp_path))
    with pytest.raises(ConfigValidationError):
        manager.load("syndication.yaml")
