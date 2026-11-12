from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Final

from scrapy.settings import BaseSettings, Settings

SERVER_PRIORITY: Final = 20
"""Priority at or above which values in the server settings are reapplied.

It matches the ``project`` priority, so values that the server set in a project
settings module cannot be silently overridden by an upload.
"""

_LOCK_PRIORITY: Final = float("inf")
"""Priority used for values that cannot be overridden by a finite priority."""

_ALLOWED_SETTINGS: Final[dict[str, Callable[[Any], bool]]] = {}
# Values that a restricted configuration can never change, applied after
# user values at a priority higher than every standard setting source.
_FORCED_SETTINGS: Final[dict[str, Any]] = {
    "REMOTE_CONTROL_ENABLED": False,
    "TELNETCONSOLE_ENABLED": False,
}

# Default components that open local code-execution or local-file surfaces.
_DISABLED_DEFAULT_COMPONENTS: Final[dict[str, tuple[str, ...]]] = {
    "DOWNLOAD_HANDLERS": ("data", "file"),
    "EXTENSIONS": (
        "scrapy.extensions.remote_control.RemoteControl",
        "scrapy.extensions.telnet.TelnetConsole",
    ),
}

# Settings that would name credentials or storage providers.
_DENIED_PREFIXES: Final[tuple[str, ...]] = (
    "AWS_",
    "AZURE_",
    "FTP_",
    "GCS_",
    "GOOGLE_",
    "HTTPAUTH_",
    "MAIL_",
    "S3_",
)

# Settings that name a module, class, function or network listener, plus
# the values fixed by _FORCED_SETTINGS. Values found here in the server
# settings are locked at _LOCK_PRIORITY, so a crawl cannot change them later.
_SERVER_CONTROLLED_SETTINGS: Final[frozenset[str]] = frozenset(
    {
        "ASYNCIO_EVENT_LOOP",
        "DOWNLOADER_CLIENTCONTEXTFACTORY",
        "DOWNLOAD_HANDLERS_BASE",
        "DOWNLOAD_HANDLERS_BY_NAME",
        "EDITOR",
        "FEED_EXPORT_FIELDS",
        "FEEDS",
        "FEED_STORAGES_BASE",
        "FEED_URI",
        "FEED_URI_PARAMS",
        "HTTPCACHE_DBM_MODULE",
        "HTTPCACHE_POLICY",
        "HTTPCACHE_STORAGE",
        "ITEM_PIPELINES_BASE",
        "ITEM_PROCESSOR",
        "JOBDIR",
        "LOG_FILE",
        "LOG_FORMATTER",
        "LOG_ROOT_FORMATTER",
        "REFERRER_POLICIES",
        "REFERRER_POLICY",
        "REMOTE_CONTROL_JOBS_DIR",
        "RETRY_EXCEPTIONS",
        "SCHEDULER",
        "SCHEDULER_DISK_QUEUE",
        "SCHEDULER_MEMORY_QUEUE",
        "SCHEDULER_PRIORITY_QUEUE",
        "SCHEDULER_START_DISK_QUEUE",
        "SCHEDULER_START_MEMORY_QUEUE",
        "SPIDER_CONTRACTS",
        "SPIDER_LOADER_CLASS",
        "SPIDER_MODULES",
        "TEMPLATES_DIR",
        "TWISTED_DNS_RESOLVER",
        "TWISTED_REACTOR_ENABLED",
        "TWISTED_REACTOR",
    }
)


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_optional_str(value: Any) -> bool:
    return value is None or _is_str(value)


def _is_int_list(value: Any) -> bool:
    return isinstance(value, list) and all(_is_int(item) for item in value)


def _is_str_dict(value: Any) -> bool:
    return isinstance(value, dict) and all(
        _is_str(key) and _is_str(item) for key, item in value.items()
    )


_ALLOWED_SETTINGS.update(
    {
        "BOT_NAME": _is_str,
        "CONCURRENT_ITEMS": _is_int,
        "CONCURRENT_REQUESTS": _is_int,
        "CONCURRENT_REQUESTS_PER_DOMAIN": _is_int,
        "COOKIES_ENABLED": _is_bool,
        "DEFAULT_REQUEST_HEADERS": _is_str_dict,
        "DEPTH_LIMIT": _is_int,
        "DEPTH_PRIORITY": _is_int,
        "DOWNLOAD_DELAY": _is_number,
        "DOWNLOAD_MAXSIZE": _is_int,
        "DOWNLOAD_TIMEOUT": _is_number,
        "DOWNLOAD_WARNSIZE": _is_int,
        "FEED_EXPORT_BATCH_ITEM_COUNT": _is_int,
        "FEED_EXPORT_ENCODING": _is_optional_str,
        "FEED_EXPORT_INDENT": _is_int,
        "FEED_MODE": _is_optional_str,
        "HTTPERROR_ALLOW_ALL": _is_bool,
        "HTTPERROR_ALLOWED_CODES": _is_int_list,
        "LOG_ENABLED": _is_bool,
        "LOG_LEVEL": _is_str,
        "LOGSTATS_INTERVAL": _is_number,
        "MEMUSAGE_ENABLED": _is_bool,
        "MEMUSAGE_LIMIT_MB": _is_int,
        "MEMUSAGE_WARNING_MB": _is_int,
        "RETRY_ENABLED": _is_bool,
        "RETRY_HTTP_CODES": _is_int_list,
        "RETRY_TIMES": _is_int,
        "ROBOTSTXT_OBEY": _is_bool,
        "USER_AGENT": _is_str,
    }
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate setting name: {key!r}")
        result[key] = value
    return result


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (TypeError, ValueError) as exception:
        raise ValueError(
            f"Configuration is not valid JSON: {exception}"
        ) from exception
    if not isinstance(value, dict):
        raise ValueError("Configuration must be a JSON object.")
    return value


def _read_json_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    p = Path(path)
    if p.suffix.lower() != ".json":
        raise ValueError("Configuration file must have a .json extension.")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exception:
        raise ValueError(
            f"Cannot read configuration file {p}: {exception}"
        ) from exception
    return _parse_json_object(text)


def _check_setting(name: str) -> None:
    if name in _FORCED_SETTINGS:
        raise ValueError(f"Setting {name!r} is controlled by the server.")
    if name in _SERVER_CONTROLLED_SETTINGS or name.startswith(
        _DENIED_PREFIXES
    ):
        raise ValueError(
            f"Setting {name!r} is not available in restricted configuration."
        )


def _check_value(name: str, value: Any) -> None:
    checker = _ALLOWED_SETTINGS.get(name)
    if checker is None:
        raise ValueError(
            f"Setting {name!r} is not available in restricted configuration."
        )
    if not checker(value):
        raise ValueError(f"Invalid value for setting {name!r}: {value!r}")


def _plain_value(value: Any) -> Any:
    return dict(value) if isinstance(value, BaseSettings) else value


def _validated_pairs(config: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in config.items():
        if not isinstance(name, str) or not name:
            raise ValueError("Setting names must be non-empty strings.")
        _check_setting(name)
        _check_value(name, value)
        result[name] = value
    return result


def apply_restricted_settings(
    config: Mapping[str, Any] | str,
    *,
    settings: Settings | None = None,
) -> Settings:
    """Return *settings* with the JSON configuration from *config* applied.

    *config* is a JSON object, as a string or a mapping, that maps global
    Scrapy setting names to JSON values. Only setting names in the
    server-side allowlist are accepted, each value must match the JSON type
    declared for that setting, and settings that load code, credentials,
    local files or network listeners are rejected.

    When *settings* is given, the configuration is applied on top of it at a
    priority above every standard source, so that server-side values and the
    locked security values cannot be overridden by a spider or a command-line
    option. Settings at :data:`SERVER_PRIORITY` or above in *settings* are
    treated as server values and kept at that lock priority. When *settings*
    is omitted, Scrapy defaults are used.

    The returned object stays mutable, because
    :class:`~scrapy.crawler.Crawler` applies spider settings to a copy during
    construction rather than to the returned object. Do not pass it to code
    that keeps accepting setting changes, such as a command that also handles
    ``-s``.

    This function validates data. It does not sandbox code that the server
    enables through its own settings, and it does not make arbitrary
    third-party extension code safe to run in the Scrapy process.
    """
    if isinstance(config, str):
        parsed = _parse_json_object(config)
    elif isinstance(config, Mapping):
        try:
            parsed = _parse_json_object(json.dumps(config))
        except (TypeError, ValueError) as exception:
            raise ValueError(
                f"Configuration is not JSON-serializable: {exception}"
            ) from exception
    else:
        raise TypeError("config must be a JSON string or a mapping.")

    values = _validated_pairs(parsed)
    server_settings = settings if settings is not None else Settings()
    server_values = {
        name: _plain_value(server_settings[name])
        for name in server_settings
        if (server_settings.getpriority(name) or 0) >= SERVER_PRIORITY
    }
    result = server_settings.copy()
    for name, value in values.items():
        result.set(name, value, priority=_LOCK_PRIORITY)
    for name, value in server_values.items():
        result.set(name, value, priority=_LOCK_PRIORITY)
    for name, value in _FORCED_SETTINGS.items():
        result.set(name, value, priority=_LOCK_PRIORITY)
    for name in _SERVER_CONTROLLED_SETTINGS:
        if name == "FEEDS" or result[name] is None:
            continue
        result.set(name, result[name], priority=_LOCK_PRIORITY)
    # Disable the named defaults without discarding the other entries, which
    # may come from the server configuration. Setting more entries to None
    # would silently disable server components.
    for name, disabled in _DISABLED_DEFAULT_COMPONENTS.items():
        overrides = dict(result[name] or {})
        overrides.update(dict.fromkeys(disabled, None))
        result.set(name, overrides, priority=_LOCK_PRIORITY)
    return result


def apply_restricted_settings_file(
    path: str | os.PathLike[str],
    *,
    settings: Settings | None = None,
) -> Settings:
    """Return *settings* with the JSON file at *path* applied as restricted
    configuration. See :func:`apply_restricted_settings`."""
    return apply_restricted_settings(_read_json_file(path), settings=settings)