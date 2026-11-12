from __future__ import annotations

import json
from pathlib import Path

import pytest

from scrapy import Spider
from scrapy.crawler import Crawler
from scrapy.settings import Settings
from scrapy.settings.restricted import apply_restricted_settings


def test_a_startup_path_does_not_load_a_rejected_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "harmless_plugin.py"
    marker = tmp_path / "imported.txt"
    module.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported')\n"
        "class Extension:\n"
        "    pass\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    assert not marker.exists()

    config = tmp_path / "user.json"
    config.write_text(
        json.dumps({"SPIDER_MIDDLEWARES": {f"{module.stem}.Extension": 0}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not available|controlled by the server"):
        apply_restricted_settings(config.read_text(encoding="utf-8"))
    assert not marker.exists()


def test_allowed_configuration_survives_crawler_construction() -> None:
    settings = apply_restricted_settings(
        json.dumps(
            {
                "CONCURRENT_REQUESTS": 4,
                "DOWNLOAD_DELAY": 0.25,
                "ROBOTSTXT_OBEY": True,
                "USER_AGENT": "reports-bot",
            }
        )
    )
    crawler = Crawler(Spider, settings)
    assert crawler.settings["CONCURRENT_REQUESTS"] == 4
    assert crawler.settings["DOWNLOAD_DELAY"] == 0.25
    assert crawler.settings["ROBOTSTXT_OBEY"] is True
    assert crawler.settings["USER_AGENT"] == "reports-bot"


def test_defaults_stay_in_place_for_unmentioned_settings() -> None:
    settings = apply_restricted_settings({"DOWNLOAD_DELAY": 1})
    handlers = dict(settings.getwithbase("DOWNLOAD_HANDLERS"))
    assert handlers["http"] == (
        "scrapy.core.downloader.handlers.http11.HTTP11DownloadHandler"
    )
    assert handlers["file"] is None
    assert handlers["data"] is None


def test_crawler_process_cannot_be_turned_back_on() -> None:
    from scrapy.crawler import AsyncCrawlerProcess

    settings = apply_restricted_settings(json.dumps({"DOWNLOAD_DELAY": 1}))
    process = AsyncCrawlerProcess(settings)
    process.settings.set("REMOTE_CONTROL_ENABLED", True, priority="cmdline")
    process.settings.set("TELNETCONSOLE_ENABLED", True, priority="cmdline")
    process.settings.set("DOWNLOAD_HANDLERS", {"file": "x"}, priority="cmdline")
    assert process.settings["REMOTE_CONTROL_ENABLED"] is False
    assert process.settings["TELNETCONSOLE_ENABLED"] is False
    assert process.settings["DOWNLOAD_HANDLERS"]["file"] is None


def test_dangerous_base_extensions_are_disabled() -> None:
    settings = apply_restricted_settings({"DOWNLOAD_DELAY": 1})
    extensions = dict(settings.getwithbase("EXTENSIONS"))
    assert extensions["scrapy.extensions.telnet.TelnetConsole"] is None
    assert extensions["scrapy.extensions.remote_control.RemoteControl"] is None
    assert extensions["scrapy.extensions.corestats.CoreStats"] == 0


def test_forced_values_also_win_over_addons() -> None:
    from tests.test_settings_restricted.test_restricted import EnableTelnetAddon

    settings = apply_restricted_settings(
        {"DOWNLOAD_DELAY": 1},
        settings=Settings({"ADDONS": {EnableTelnetAddon: 0}}),
    )
    crawler = Crawler(Spider, settings)
    crawler.addons.load_settings(crawler.settings)
    assert crawler.settings["TELNETCONSOLE_ENABLED"] is False


def test_finite_priority_cannot_override_the_lock() -> None:
    settings = apply_restricted_settings({"DOWNLOAD_DELAY": 1})
    settings.set("TELNETCONSOLE_ENABLED", True, priority=10**12)
    settings.set("DOWNLOAD_HANDLERS", {"file": "x"}, priority=10**12)
    assert settings["TELNETCONSOLE_ENABLED"] is False
    assert dict(settings.getwithbase("DOWNLOAD_HANDLERS"))["file"] is None
