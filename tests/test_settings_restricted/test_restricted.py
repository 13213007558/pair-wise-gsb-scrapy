from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scrapy import Spider
from scrapy.crawler import Crawler
from scrapy.settings import BaseSettings, Settings
from scrapy.settings.restricted import (
    apply_restricted_settings,
    apply_restricted_settings_file,
)
from scrapy.utils.conf import build_component_list


class EnableTelnetAddon:
    def update_settings(self, settings: BaseSettings) -> None:
        settings.set("TELNETCONSOLE_ENABLED", True, priority="addon")


class AllowedAddon:
    def update_settings(self, settings: BaseSettings) -> None:
        pass


def test_allowed_values_are_applied() -> None:
    settings = apply_restricted_settings(
        json.dumps(
            {
                "BOT_NAME": "reports",
                "CONCURRENT_REQUESTS": 8,
                "DOWNLOAD_DELAY": 0.5,
                "ROBOTSTXT_OBEY": True,
            }
        )
    )
    assert settings["BOT_NAME"] == "reports"
    assert settings["CONCURRENT_REQUESTS"] == 8
    assert settings["DOWNLOAD_DELAY"] == 0.5
    assert settings["ROBOTSTXT_OBEY"] is True


def test_server_settings_win_over_user_values() -> None:
    server_settings = Settings({"CONCURRENT_REQUESTS_PER_DOMAIN": 4})
    settings = apply_restricted_settings(
        {"CONCURRENT_REQUESTS_PER_DOMAIN": 64},
        settings=server_settings,
    )
    crawler = Crawler(Spider, settings)
    assert crawler.settings["CONCURRENT_REQUESTS_PER_DOMAIN"] == 4


def test_forced_values_cannot_be_overridden_by_spider() -> None:
    class OverridingSpider(Spider):
        name = "overriding"
        custom_settings = {
            "TELNETCONSOLE_ENABLED": True,
        }

    settings = apply_restricted_settings({"CONCURRENT_REQUESTS": 8})
    crawler = Crawler(OverridingSpider, settings)
    assert crawler.settings["TELNETCONSOLE_ENABLED"] is False


def _unused() -> None:
    settings = apply_restricted_settings({"CONCURRENT_REQUESTS": 8})
    settings = apply_restricted_settings({"CONCURRENT_REQUESTS": 8})

    class OverridingSpider(Spider):
        name = "overriding"
        custom_settings = {
            "DOWNLOAD_HANDLERS": {
                "file": "scrapy.core.downloader.handlers.file.FileDownloadHandler"
            },
            "REMOTE_CONTROL_ENABLED": True,
            "TELNETCONSOLE_ENABLED": True,
        }

    crawler = Crawler(OverridingSpider, settings)
    assert crawler.settings["DOWNLOAD_HANDLERS"]["file"] is None
    assert crawler.settings["REMOTE_CONTROL_ENABLED"] is False
    assert crawler.settings["TELNETCONSOLE_ENABLED"] is False


def test_mapping_must_be_json_serializable() -> None:
    with pytest.raises(ValueError, match="not JSON-serializable"):
        apply_restricted_settings({"DOWNLOAD_DELAY": object()})


def test_string_must_be_a_json_object() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        apply_restricted_settings("[1, 2]")


def test_bad_json_is_rejected() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        apply_restricted_settings("{not json}")


def test_duplicate_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate setting name"):
        apply_restricted_settings('{"DOWNLOAD_DELAY": 1, "DOWNLOAD_DELAY": 2}')


def test_file_requires_json_extension(tmp_path: Path) -> None:
    path = tmp_path / "settings.py"
    path.write_text("DOWNLOAD_DELAY = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="\\.json extension"):
        apply_restricted_settings_file(path)


def test_file_is_loaded(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"DOWNLOAD_DELAY": 1}', encoding="utf-8")
    settings = apply_restricted_settings_file(path)
    assert settings["DOWNLOAD_DELAY"] == 1


def test_unknown_settings_are_rejected() -> None:
    with pytest.raises(ValueError, match="not available"):
        apply_restricted_settings({"NOT_A_SETTING": 1})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DOWNLOAD_DELAY", "1"),
        ("ROBOTSTXT_OBEY", "true"),
        ("CONCURRENT_REQUESTS", 1.5),
        ("CONCURRENT_REQUESTS", True),
        ("DOWNLOAD_DELAY", float("nan")),
        ("DOWNLOAD_DELAY", float("inf")),
        ("DEFAULT_REQUEST_HEADERS", {"Accept": 1}),
        ("HTTPERROR_ALLOWED_CODES", [200, "404"]),
        ("LOG_LEVEL", None),
    ],
)
def test_invalid_types_are_rejected(name: str, value: object) -> None:
    with pytest.raises(ValueError, match="Invalid value"):
        apply_restricted_settings({name: value})


@pytest.mark.parametrize(
    "name",
    [
        "ADDONS",
        "COMMANDS_MODULE",
        "DOWNLOADER",
        "DOWNLOAD_HANDLERS",
        "DOWNLOAD_HANDLERS_BY_NAME",
        "DUPEFILTER_CLASS",
        "EXTENSIONS",
        "FEED_EXPORTERS",
        "FEED_STORAGES",
        "ITEM_PIPELINES",
        "ITEM_PROCESSOR",
        "LOG_FORMATTER",
        "LOG_ROOT_FORMATTER",
        "MEDIA_STORAGES",
        "REQUEST_FINGERPRINTER_CLASS",
        "SCHEDULER",
        "SCHEDULER_DISK_QUEUE",
        "SCHEDULER_MEMORY_QUEUE",
        "SCHEDULER_PRIORITY_QUEUE",
        "SCHEDULER_START_DISK_QUEUE",
        "SCHEDULER_START_MEMORY_QUEUE",
        "SPIDER_CONTRACTS",
        "SPIDER_LOADER_CLASS",
        "SPIDER_MIDDLEWARES",
        "STATS_CLASS",
        "TWISTED_REACTOR",
        "TWISTED_REACTOR_ENABLED",
        "ASYNCIO_EVENT_LOOP",
        "HTTPCACHE_POLICY",
        "HTTPCACHE_STORAGE",
        "HTTPCACHE_DBM_MODULE",
        "RETRY_EXCEPTIONS",
        "REFERRER_POLICY",
        "REFERRER_POLICIES",
    ],
)
def test_code_loading_settings_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="controlled by the server|not available"):
        apply_restricted_settings({name: "tests.test_settings_restricted.Evil"})


@pytest.mark.parametrize(
    "name",
    [
        "EDITOR",
        "FEEDS",
        "FEED_EXPORT_FIELDS",
        "FEED_URI",
        "FEED_URI_PARAMS",
        "FEED_STORAGES_BASE",
        "DOWNLOAD_HANDLERS_BASE",
        "JOBDIR",
        "LOG_FILE",
        "REMOTE_CONTROL_JOBS_DIR",
        "SPIDER_MODULES",
        "TEMPLATES_DIR",
        "TWISTED_DNS_RESOLVER",
        "DOWNLOADER_CLIENTCONTEXTFACTORY",
        "HTTPAUTH_USER",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "FTP_PASSWORD",
        "MAIL_PASS",
        "S3_SECRET_KEY",
        "GCS_PROJECT_ID",
    ],
)
def test_files_credentials_and_listeners_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="not available|controlled by the server"):
        apply_restricted_settings({name: "x"})


def test_no_module_is_imported_when_a_key_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "benign_extension.py"
    marker = tmp_path / "imported.txt"
    module.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(module.stem, None)
    assert not marker.exists()
    with pytest.raises(ValueError, match="not available|controlled by the server"):
        apply_restricted_settings(
            {"SPIDER_MIDDLEWARES": {f"{module.stem}.Extension": 0}}
        )
    assert not marker.exists()
    assert module.stem not in sys.modules


def test_component_validation_does_import_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "benign_component.py"
    marker = tmp_path / "imported.txt"
    module.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported')\n"
        "class Extension:\n"
        "    pass\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(module.stem, None)
    settings = Settings(
        {"DOWNLOADER_MIDDLEWARES": {f"{module.stem}.Extension": 0}}
    )
    assert not marker.exists()
    resolved = settings.get_component_priority_dict_with_base(
        "DOWNLOADER_MIDDLEWARES"
    )
    assert marker.exists()
    assert f"{module.stem}.Extension" in build_component_list(resolved)


def test_server_code_settings_are_locked_for_the_crawl() -> None:
    server_settings = Settings(
        {
            "ADDONS": {"tests.test_settings_restricted.AllowedAddon": 0},
            "LOG_FORMATTER": "scrapy.logformatter.LogFormatter",
        }
    )

    class OverridingSpider(Spider):
        name = "overriding_code"
        custom_settings = {
            "ADDONS": {},
            "LOG_FORMATTER": "tests.test_settings_restricted.OtherFormatter",
        }

    settings = apply_restricted_settings(
        {"DOWNLOAD_DELAY": 1}, settings=server_settings
    )
    crawler = Crawler(OverridingSpider, settings)
    assert crawler.settings["ADDONS"] == {
        "tests.test_settings_restricted.AllowedAddon": 0
    }
    assert crawler.settings["LOG_FORMATTER"] == "scrapy.logformatter.LogFormatter"


def test_project_priority_is_treated_as_server_value() -> None:
    server_settings = Settings({"DOWNLOAD_DELAY": 3})
    result = apply_restricted_settings(
        {"DOWNLOAD_DELAY": 1}, settings=server_settings
    )
    assert result["DOWNLOAD_DELAY"] == 3
    assert server_settings["DOWNLOAD_DELAY"] == 3


def test_lower_priority_server_value_can_be_changed() -> None:
    server_settings = Settings()
    server_settings.set("DOWNLOAD_DELAY", 3, priority="default")
    result = apply_restricted_settings(
        {"DOWNLOAD_DELAY": 1}, settings=server_settings
    )
    assert result["DOWNLOAD_DELAY"] == 1
