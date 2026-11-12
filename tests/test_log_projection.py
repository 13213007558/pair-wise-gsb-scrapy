from __future__ import annotations

import logging
import traceback
from typing import Any, cast

import pytest
from twisted.python.failure import Failure

from scrapy import Request, Spider
from scrapy.core.scheduler import Scheduler
from scrapy.exceptions import DownloadFailedError
from scrapy.logformatter import LogFormatter
from scrapy.utils.log import log_with_projection
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

SECRET = "temporary-credential"
SENSITIVE_HEADER = "X-Session-Credential"
SENSITIVE_QUERY_PARAM = "session"


def _settings(**kwargs: Any) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "LOG_LEVEL": "DEBUG",
        "LOG_SENSITIVE_HEADERS": [SENSITIVE_HEADER],
        "LOG_SENSITIVE_QUERY_PARAMS": [SENSITIVE_QUERY_PARAM],
    }
    settings.update(kwargs)
    return settings


def _request(url: str) -> Request:
    return Request(
        url,
        headers={
            "Authorization": f"Bearer {SECRET}",
            SENSITIVE_HEADER: SECRET,
            "Accept": "text/plain",
        },
    )


def _assert_projected(text: str) -> None:
    assert SECRET not in text
    assert "Bearer" not in text


class _ProjectionSpider(Spider):
    name = "projection"

    async def start(self):
        yield Request(
            f"data:text/plain,body?{SENSITIVE_QUERY_PARAM}={SECRET}",
            headers={
                "Authorization": f"Bearer {SECRET}",
                SENSITIVE_HEADER: SECRET,
            },
            callback=self.parse,
            errback=self.on_error,
        )

    def parse(self, response):
        return None

    def on_error(self, failure: Failure):
        return None


class _ErrbackRaisesSpider(_ProjectionSpider):
    name = "projection_errback_raises"

    def on_error(self, failure: Failure):
        if failure.request is None:
            return None
        raise ValueError(
            f"errback failed for {failure.request.url} "
            f"with Authorization: Bearer {SECRET}"
        )


class _FailingMiddleware:
    def __init__(self, crawler: Any) -> None:
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler: Any):
        return cls(crawler)

    def process_request(self, request: Request) -> None:
        raise DownloadFailedError(
            f"failed {request.url} with Authorization: Bearer {SECRET}"
        )


def _crawl_settings(**kwargs: Any) -> dict[str, Any]:
    settings = _settings(RETRY_ENABLED=False, **kwargs)
    settings["DOWNLOADER_MIDDLEWARES"] = {
        "tests.test_log_projection._FailingMiddleware": 543,
        **settings.get("DOWNLOADER_MIDDLEWARES", {}),
    }
    return settings


def _rendered_text(records: list[logging.LogRecord]) -> str:
    formatter = logging.Formatter("%(message)s")
    return "\n".join(formatter.format(record) for record in records)


@coroutine_test
async def test_download_error_log_is_projected_and_keeps_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    crawler = get_crawler(_ErrbackRaisesSpider, _crawl_settings())
    with caplog.at_level(logging.DEBUG):
        await crawler.crawl_async()
    rendered = _rendered_text(list(caplog.records))
    errors = [
        record
        for record in caplog.records
        if record.getMessage().startswith("Error downloading")
    ]
    assert errors
    for record in errors:
        message = record.getMessage()
        _assert_projected(message)
        assert f"{SENSITIVE_QUERY_PARAM}=[redacted]" in message
        assert "<GET data:text/plain" in message
    _assert_projected(rendered)
    assert "DownloadFailedError" in rendered
    assert "[redacted]" in rendered


@coroutine_test
async def test_retry_give_up_log_is_projected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    crawler = get_crawler(
        _ProjectionSpider,
        _settings(RETRY_ENABLED=True, RETRY_TIMES=0, RETRY_HTTP_CODES=[]),
    )
    crawler.spider = crawler._create_spider()
    from scrapy.downloadermiddlewares.retry import get_retry_request

    request = _request(f"data:text/plain,body?{SENSITIVE_QUERY_PARAM}={SECRET}")
    with caplog.at_level(logging.DEBUG):
        assert (
            get_retry_request(request, spider=crawler.spider, max_retry_times=0)
            is None
        )
    messages = [
        record.getMessage()
        for record in caplog.records
        if "Gave up retrying" in record.getMessage()
    ]
    assert messages
    for message in messages:
        _assert_projected(message)
        assert "(failed 1 times)" in message
        assert f"{SENSITIVE_QUERY_PARAM}=[redacted]" in message


@coroutine_test
async def test_retry_log_is_projected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    crawler = get_crawler(
        _ProjectionSpider,
        _settings(RETRY_ENABLED=True, RETRY_TIMES=2, RETRY_HTTP_CODES=[]),
    )
    crawler.spider = crawler._create_spider()
    from scrapy.downloadermiddlewares.retry import get_retry_request

    request = _request(
        f"data:text/plain,body?{SENSITIVE_QUERY_PARAM}={SECRET}"
    )
    with caplog.at_level(logging.DEBUG):
        new_request = get_retry_request(request, spider=crawler.spider)
    assert new_request is not None
    messages = [
        record.getMessage()
        for record in caplog.records
        if "Retrying" in record.getMessage()
    ]
    assert messages
    for message in messages:
        _assert_projected(message)
        assert message.startswith("Retrying ")
    # The live request keeps its credential for the next hop.
    assert new_request.headers["Authorization"] == f"Bearer {SECRET}".encode()


def test_dupefilter_log_is_projected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    crawler = get_crawler(Spider, _settings())
    crawler.spider = crawler._create_spider("projection")
    spider = crawler.spider
    scheduler = build_from_crawler(Scheduler, crawler)
    request = _request(
        f"data:text/plain,body?{SENSITIVE_QUERY_PARAM}={SECRET}"
    )
    with caplog.at_level(logging.DEBUG):
        scheduler.df.log(request, spider)
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "scrapy.dupefilters"
    ]
    assert messages
    for message in messages:
        _assert_projected(message)
        assert "Filtered duplicate request" in message
        assert f"{SENSITIVE_QUERY_PARAM}=[redacted]" in message


@coroutine_test
async def test_scheduler_unserializable_log_is_projected(
    caplog: pytest.LogCaptureFixture, tmp_path: Any
) -> None:
    class UnserializableSpider(Spider):
        name = "unserializable"

        async def start(self):
            yield Request(
                f"data:text/plain,body?{SENSITIVE_QUERY_PARAM}={SECRET}",
                headers={"Authorization": f"Bearer {SECRET}"},
                meta={"callable": lambda: None},
            )

    crawler = get_crawler(
        UnserializableSpider,
        _settings(JOBDIR=str(tmp_path), SCHEDULER_DEBUG=True),
    )
    with caplog.at_level(logging.WARNING):
        await crawler.crawl_async()
    messages = [
        record.getMessage()
        for record in caplog.records
        if "Unable to serialize request" in record.getMessage()
    ]
    assert messages
    for message in messages:
        _assert_projected(message)
        assert f"{SENSITIVE_QUERY_PARAM}=[redacted]" in message


@coroutine_test
async def test_custom_logformatter_log_is_projected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class VerboseFormatter(LogFormatter):
        def crawled(self, request, response, spider):
            return {
                "level": logging.DEBUG,
                "msg": "Verbose crawled %(request)s headers=%(headers)s",
                "args": {"request": request, "headers": request.headers},
            }

    crawler = get_crawler(
        _ProjectionSpider,
        _settings(LOG_FORMATTER=VerboseFormatter),
    )
    with caplog.at_level(logging.DEBUG):
        await crawler.crawl_async()
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Verbose crawled")
    ]
    assert messages
    for message in messages:
        _assert_projected(message)
        assert f"{SENSITIVE_QUERY_PARAM}=[redacted]" in message
        assert f"{SENSITIVE_HEADER}: [redacted]" in message
        assert "User-Agent: " in message


@coroutine_test
async def test_spider_error_traceback_is_projected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    crawler = get_crawler(
        _ErrbackRaisesSpider, _crawl_settings()
    )
    with caplog.at_level(logging.DEBUG):
        await crawler.crawl_async()
    rendered = _rendered_text(list(caplog.records))
    assert "Spider error processing" in rendered
    assert "ValueError" in rendered
    _assert_projected(rendered)


def test_projection_does_not_modify_request() -> None:
    from scrapy.utils._log_projection import _LogProjection

    projection = _LogProjection(
        sensitive_headers=[SENSITIVE_HEADER],
        sensitive_query_params=[SENSITIVE_QUERY_PARAM],
    )
    request = _request(
        f"https://example.example/path?{SENSITIVE_QUERY_PARAM}={SECRET}&keep=1"
    )
    before_url = request.url
    before_headers = dict(request.headers.items())
    before_meta = dict(request.meta)
    projected = projection.project_request(request)
    assert SECRET not in projected.url
    assert projected.headers["Authorization"] == b"[redacted]"
    assert projected.headers[SENSITIVE_HEADER] == b"[redacted]"
    assert request.url == before_url
    assert dict(request.headers.items()) == before_headers
    assert dict(request.meta) == before_meta
    assert request.headers["Authorization"] == f"Bearer {SECRET}".encode()
    assert request.headers[SENSITIVE_HEADER] == SECRET.encode()


def test_exception_fallback_does_not_bypass_rules() -> None:
    from scrapy.utils._log_projection import _LogProjection

    projection = _LogProjection(
        sensitive_query_params=[SENSITIVE_QUERY_PARAM],
    )

    class BrokenTextError(Exception):
        def __str__(self):
            raise RuntimeError(f"cannot stringify {SECRET}")

    exception = ValueError(
        f"failed for https://example.example/?{SENSITIVE_QUERY_PARAM}={SECRET} "
        f"Authorization: Bearer {SECRET}"
    )
    projected = projection.project_exception(exception)
    assert projected is not None
    assert type(projected).__name__ == "ValueError"
    assert SECRET not in str(projected)
    assert SECRET not in repr(projected)

    projected_broken = projection.project_exception(BrokenTextError(SECRET))
    assert projected_broken is not None
    assert SECRET not in str(projected_broken)
    assert SECRET not in repr(projected_broken)


@pytest.mark.parametrize(
    "make_exception",
    [
        lambda: OSError(f"failed https://example.example/?{SENSITIVE_QUERY_PARAM}={SECRET}"),
        lambda: ExceptionGroup(
            f"failed https://example.example/?{SENSITIVE_QUERY_PARAM}={SECRET}",
            [
                ValueError(
                    f"inner https://example.example/"
                    f"?{SENSITIVE_QUERY_PARAM}={SECRET}"
                )
            ],
        ),
        lambda: SyntaxError(
            f"bad https://example.example/?{SENSITIVE_QUERY_PARAM}={SECRET}"
        ),
        lambda: UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
    ],
)
def test_special_exception_types_are_projected(make_exception: Any) -> None:
    from scrapy.utils._log_projection import _LogProjection

    exception = make_exception()
    projection = _LogProjection(sensitive_query_params=[SENSITIVE_QUERY_PARAM])
    projected = projection.project_exception(exception)
    assert projected is not None
    assert type(projected).__name__ == type(exception).__name__
    if isinstance(projected, BaseExceptionGroup):
        for child in projected.exceptions:
            assert SECRET not in str(child)
    text = "".join(traceback.format_exception(type(projected), projected, None))
    assert SECRET not in text


def test_exception_without_text_falls_back_to_class_name() -> None:
    from scrapy.utils._log_projection import _FallbackProjectedException, _LogProjection

    class BrokenTextError(Exception):
        def __str__(self):
            raise RuntimeError("cannot stringify")

    exception = BrokenTextError()
    projected = _LogProjection().project_exception(exception)
    assert projected is not None
    assert str(projected) == "BrokenTextError"


def test_unsubclassable_exception_still_projects_text() -> None:
    from scrapy.utils._log_projection import (
        _FallbackProjectedException,
        _LogProjection,
    )

    class Unsubclassable(Exception):
        def __new__(cls, *args, **kwargs):
            if cls is not Unsubclassable:
                raise TypeError("subclasses are rejected")
            return super().__new__(cls)

    exception = Unsubclassable(
        f"failed https://example.example/?{SENSITIVE_QUERY_PARAM}={SECRET}"
    )
    projected = _LogProjection(
        sensitive_query_params=[SENSITIVE_QUERY_PARAM]
    ).project_exception(exception)
    assert isinstance(projected, _FallbackProjectedException)
    assert SECRET not in str(projected)


def test_exception_chain_and_notes_are_projected() -> None:
    from scrapy.utils._log_projection import _LogProjection

    projection = _LogProjection(
        sensitive_query_params=[SENSITIVE_QUERY_PARAM],
    )
    cause = ValueError(
        f"https://example.example/?{SENSITIVE_QUERY_PARAM}={SECRET}"
    )
    exception = RuntimeError("outer")
    exception.__cause__ = cause
    exception.add_note(f"note {SENSITIVE_QUERY_PARAM}={SECRET}")
    projected = projection.project_exception(exception)
    assert projected is not None
    assert projected.__cause__ is not None
    assert SECRET not in str(projected.__cause__)
    assert projected.__notes__ is not None
    assert all(SECRET not in note for note in projected.__notes__)


def test_text_projection_covers_headers_and_query_pairs() -> None:
    from scrapy.utils._log_projection import _LogProjection

    projection = _LogProjection()
    cases = [
        "Authorization: Bearer token",
        "{'Authorization': 'Bearer token'}",
        "headers={'Authorization': 'Bearer token'}",
        "authorization=Bearer token",
        "Proxy-Authorization: Basic dXNlcjpwYXNz",
    ]
    for text in cases:
        assert "Bearer token" not in projection.project_text(text)
        assert "dXNlcjpwYXNz" not in projection.project_text(text)


def test_text_projection_keeps_safe_parts() -> None:
    from scrapy.utils._log_projection import _LogProjection

    projection = _LogProjection(sensitive_query_params=[SENSITIVE_QUERY_PARAM])
    url = (
        "https://example.example/path?keep=1"
        f"&{SENSITIVE_QUERY_PARAM}={SECRET}&second=2"
    )
    projected = projection.project_url(url)
    assert "keep=1" in projected
    assert "second=2" in projected
    assert f"{SENSITIVE_QUERY_PARAM}=[redacted]" in projected
    assert projection.project_url("https://example.example/path") == (
        "https://example.example/path"
    )
    assert projection.project_text("plain text") == "plain text"


def test_projected_response_headers_and_values() -> None:
    from scrapy.http import Response
    from scrapy.utils._log_projection import _LogProjection

    projection = _LogProjection(
        sensitive_headers=[SENSITIVE_HEADER],
        sensitive_query_params=[SENSITIVE_QUERY_PARAM],
    )
    request = _request(
        f"https://example.example/?{SENSITIVE_QUERY_PARAM}={SECRET}"
    )
    response = Response(
        request.url,
        request=request,
        headers={SENSITIVE_HEADER: SECRET, "Content-Type": "text/plain"},
    )
    projected = projection.project_response(response)
    assert SECRET not in str(projected)
    assert projected.headers[SENSITIVE_HEADER] == b"[redacted]"
    assert projected.headers.get("Content-Type") == b"text/plain"
    assert projected.headers.get("X-Missing") is None
    assert projected.headers.getlist(SENSITIVE_HEADER) == [b"[redacted]"]
    assert projected.headers.getlist("X-Missing") == []
    assert dict(projected.headers.items())[b"Content-Type"] == [b"text/plain"]
    assert b"Content-Type: text/plain" in projected.headers.to_string()
    assert ("Content-Type", "text/plain") in projected.headers.to_tuple_list()
    assert list(projected.headers.keys())
    assert projected.headers.values()
    assert projected.headers
    assert projected.request is not None
    assert projected.request.headers[SENSITIVE_HEADER] == b"[redacted]"
    assert repr(projected) == str(projected)
    assert repr(projected.request) == str(projected.request)


def test_projected_request_without_response_request() -> None:
    from scrapy.http import Response
    from scrapy.utils._log_projection import _LogProjection

    projected = _LogProjection().project_response(Response("https://example.example/"))
    assert projected.request is None


def test_projection_of_generic_values() -> None:
    from scrapy.http import Headers
    from scrapy.utils._log_projection import _LogProjection

    projection = _LogProjection()
    request = Request("https://example.example/")
    projected_request = projection.project_request(request)
    assert projection.project_value(projected_request) is projected_request
    projected_headers = projection.project_value(Headers({"A": "b"}))
    assert projected_headers["A"] == b"b"
    assert projection.project_value("https://example.example/?token=secret") != (
        "https://example.example/?token=secret"
    )
    assert projection.project_value(3) == 3
    assert projection.project_args({"c": 1}) == {"c": 1}
    assert projection.project_args(("a", 1)) == ("a", 1)
    assert projection.project_args(None) is None


def test_projection_uses_crawler_settings() -> None:
    from scrapy.http import Response
    from scrapy.utils._log_projection import _LogProjection

    crawler = get_crawler(Spider, _settings())
    projection = _LogProjection.from_crawler(crawler)
    request = _request(
        f"https://example.example/?{SENSITIVE_QUERY_PARAM}={SECRET}",
    )
    assert SECRET not in projection.project_request(request).url
    assert (
        projection.project_response(Response(request.url, request=request)).request
        is not None
    )


def test_adapter_projects_defaults() -> None:
    from scrapy.http import Response
    from scrapy.logformatter import LogFormatter

    crawler = get_crawler(Spider, _settings())
    spider = Spider.from_crawler(crawler, "projection")
    formatter = build_from_crawler(LogFormatter, crawler)
    request = Request(
        f"https://example.example/?{SENSITIVE_QUERY_PARAM}={SECRET}"
    )
    response = Response(request.url, request=request)
    logkws = formatter.crawled(request, response, spider)
    from scrapy.utils.log import logformatter_adapter

    adapter = logformatter_adapter(logkws, crawler=crawler)
    message = cast("str", adapter[1])
    args = cast("dict[str, Any]", adapter[2])
    assert SECRET not in (message % args)


def test_projected_failure_value_and_error_message() -> None:
    from scrapy.utils._log_projection import _LogProjection

    projection = _LogProjection(sensitive_query_params=[SENSITIVE_QUERY_PARAM])
    failure = Failure(
        ValueError(
            f"https://example.example/?{SENSITIVE_QUERY_PARAM}={SECRET}"
        )
    )
    projected = projection.project_failure(failure)
    assert projected.type is not None
    assert projected.value is not None
    assert SECRET not in str(projected.value)
    assert SECRET not in projected.getErrorMessage()
    assert SECRET not in str(projected)
    assert SECRET not in repr(projected)


def test_handler_messages_use_declared_settings() -> None:
    from scrapy.utils._download_handlers import (
        get_dataloss_msg,
        get_maxsize_msg,
        get_warnsize_msg,
    )

    crawler = get_crawler(Spider, _settings())
    request = _request(
        f"https://example.example/?{SENSITIVE_QUERY_PARAM}={SECRET}"
    )
    maxsize = get_maxsize_msg(5, 4, request, expected=True, source=crawler)
    warnsize = get_warnsize_msg(5, 4, request, expected=True, source=crawler)
    dataloss = get_dataloss_msg(request.url, source=crawler)
    for message in (maxsize, warnsize, dataloss):
        _assert_projected(message)
        assert f"{SENSITIVE_QUERY_PARAM}=[redacted]" in message


def test_log_with_projection_projects_message_and_extra(
    caplog: pytest.LogCaptureFixture,
) -> None:
    crawler = get_crawler(Spider, _settings())
    request = _request(
        f"https://example.example/?{SENSITIVE_QUERY_PARAM}={SECRET}"
    )
    with caplog.at_level(logging.DEBUG):
        log_with_projection(
            logging.getLogger("projection-test"),
            logging.DEBUG,
            f"custom {request} Authorization: Bearer {SECRET}",
            {"request": request},
            source=crawler,
            extra={"request": request},
        )
    record = caplog.records[-1]
    assert SECRET not in record.getMessage()
    assert SECRET not in repr(getattr(record, "request", ""))
