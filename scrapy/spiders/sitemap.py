from __future__ import annotations

import logging
import inspect
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

# Iterable is needed at the run time for the SitemapSpider._parse_sitemap() annotation
from collections.abc import AsyncIterator, Iterable, Sequence  # noqa: TC003
from typing import TYPE_CHECKING, Any, cast

from scrapy import signals
from scrapy.exceptions import DontCloseSpider
from scrapy.http import Request, Response, XmlResponse
from scrapy.spiders import Spider
from scrapy.utils._compression import (
    _DecompressionMaxSizeExceeded,
    gunzip,
    gzip_magic_number,
)
from scrapy.utils._sitemap import Sitemap, sitemap_urls_from_robots
from scrapy.utils.defer import maybe_deferred_to_future
from scrapy.utils.misc import arg_to_iter

if TYPE_CHECKING:
    # typing.Self requires Python 3.11
    from typing_extensions import Self

    from scrapy.crawler import Crawler
    from scrapy.http.request import CallbackT
    from twisted.python.failure import Failure

logger = logging.getLogger(__name__)

_SITEMAP_KEY = "_sitemap"
_SITEMAP_PARENT_KEY = "_sitemap_parent"
_LASTMOD_KEY = "_sitemap_lastmod"
_LASTMODS_STATE_KEY = "_sitemap_lastmods"
_LASTMOD_DATE = "date"
_LASTMOD_DATETIME = "datetime"


@dataclass(slots=True)
class _Lastmod:
    precision: str
    value: date

    def as_state(self) -> tuple[str, str]:
        return self.precision, self.value.isoformat()


@dataclass(slots=True)
class _SitemapPageCandidate:
    callback: CallbackT
    versions: list[_Lastmod | None]


@dataclass(slots=True)
class _PendingSitemapPage:
    url: str
    version: _Lastmod | None
    items: int = 0
    finished_items: int = 0
    callback_finished: bool = False
    failed: bool = False


def _lastmod_from_state(value: Any) -> _Lastmod | None:
    if not isinstance(value, tuple | list) or len(value) != 2:
        return None
    precision, raw_value = value
    if precision not in {_LASTMOD_DATE, _LASTMOD_DATETIME} or not isinstance(
        raw_value, str
    ):
        return None
    try:
        parsed_value = (
            date.fromisoformat(raw_value)
            if precision == _LASTMOD_DATE
            else datetime.fromisoformat(raw_value)
        )
    except ValueError:
        return None
    if precision == _LASTMOD_DATETIME and (
        not isinstance(parsed_value, datetime) or parsed_value.tzinfo is None
    ):
        return None
    if isinstance(parsed_value, datetime):
        parsed_value = parsed_value.astimezone(timezone.utc)
    return _Lastmod(precision, parsed_value)


def _parse_lastmod(value: Any) -> _Lastmod | None:
    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return _Lastmod(_LASTMOD_DATE, date.fromisoformat(value))
        if re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            value,
        ):
            parsed = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            return _Lastmod(_LASTMOD_DATETIME, parsed)
    except ValueError:
        return None
    return None


def _select_lastmod(values: Iterable[_Lastmod | None]) -> _Lastmod | None:
    versions = list(values)
    valid_versions = [version for version in versions if version is not None]
    if not valid_versions or len(valid_versions) != len(versions):
        return None
    precision = valid_versions[0].precision
    if any(version.precision != precision for version in valid_versions):
        return None
    return max(valid_versions, key=lambda version: version.value)


class SitemapSpider(Spider):
    sitemap_urls: Sequence[str] = ()
    sitemap_rules: Sequence[tuple[re.Pattern[str] | str, str | CallbackT]] = [
        ("", "parse")
    ]
    sitemap_follow: Sequence[re.Pattern[str] | str] = [""]
    sitemap_alternate_links: bool = False
    sitemap_incremental: bool = False
    sitemap_refresh: bool = False
    _max_size: int
    _warn_size: int

    @classmethod
    def from_crawler(cls, crawler: Crawler, *args: Any, **kwargs: Any) -> Self:
        spider = super().from_crawler(crawler, *args, **kwargs)
        spider._max_size = getattr(
            spider, "download_maxsize", spider.settings.getint("DOWNLOAD_MAXSIZE")
        )
        spider._warn_size = getattr(
            spider, "download_warnsize", spider.settings.getint("DOWNLOAD_WARNSIZE")
        )
        return spider

    def __init__(
        self,
        *a: Any,
        sitemap_incremental: bool | str | None = None,
        sitemap_refresh: bool | str | None = None,
        **kw: Any,
    ):
        super().__init__(*a, **kw)
        if sitemap_incremental is not None:
            self.sitemap_incremental = self._get_bool(sitemap_incremental)
        if sitemap_refresh is not None:
            self.sitemap_refresh = self._get_bool(sitemap_refresh)
        if self.sitemap_refresh:
            self.sitemap_incremental = True
        self._cbs: list[tuple[re.Pattern[str], CallbackT]] = []
        for r, c in self.sitemap_rules:
            if isinstance(c, str):
                c = cast("CallbackT", getattr(self, c))  # noqa: PLW2901
            self._cbs.append((regex(r), c))
        self._follow: list[re.Pattern[str]] = [regex(x) for x in self.sitemap_follow]
        self._pending_sitemaps = 0
        self._sitemap_candidates: dict[str, _SitemapPageCandidate] = {}
        self._pending_sitemap_pages: dict[Response, _PendingSitemapPage] = {}

    @staticmethod
    def _get_bool(value: bool | str) -> bool:
        if isinstance(value, bool):
            return value
        return value.lower() == "true" or value == "1"

    def _set_crawler(self, crawler: Crawler) -> None:
        super()._set_crawler(crawler)
        if self.sitemap_incremental:
            crawler.signals.connect(
                self._spider_idle, signal=signals.spider_idle
            )
            crawler.signals.connect(
                self._item_completed, signal=signals.item_scraped
            )
            crawler.signals.connect(
                self._item_completed, signal=signals.item_dropped
            )
            crawler.signals.connect(self._item_error, signal=signals.item_error)

    async def start(self) -> AsyncIterator[Any]:
        if self.sitemap_refresh:
            self._lastmods.clear()
        self._pending_sitemaps += len(self.sitemap_urls)
        for url in self.sitemap_urls:
            yield self._sitemap_request(url)

    @property
    def _lastmods(self) -> dict[str, tuple[str, str]]:
        state = getattr(self, "state", None)
        if state is None:
            state = {}
            self.state = state
        lastmods = state.get(_LASTMODS_STATE_KEY)
        if not isinstance(lastmods, dict):
            lastmods = {}
            state[_LASTMODS_STATE_KEY] = lastmods
        invalid_urls = [
            url
            for url, version in lastmods.items()
            if not isinstance(url, str) or _lastmod_from_state(version) is None
        ]
        for url in invalid_urls:
            lastmods.pop(url)
        return cast("dict[str, tuple[str, str]]", lastmods)

    def _sitemap_request(self, url: str, *, parent: bool = False) -> Request:
        if not self.sitemap_incremental:
            return Request(url, self._parse_sitemap)
        return Request(
            url,
            self._parse_sitemap,
            dont_filter=True,
            errback=self._sitemap_error,
            meta={_SITEMAP_KEY: True, _SITEMAP_PARENT_KEY: parent},
        )

    def _sitemap_error(self, failure: Failure) -> None:
        request = failure.request
        if request.meta.get(_SITEMAP_KEY):
            self._finish_sitemap(request)

    def _spider_idle(self) -> None:
        if self._pending_sitemaps:
            raise DontCloseSpider

    def _item_completed(self, *, response: Response | None, **kwargs: Any) -> None:
        if response is None:
            return
        pending = self._pending_sitemap_pages.get(response)
        if pending is None:
            return
        pending.finished_items += 1
        self._finish_sitemap_page_if_ready(response, pending)

    def _item_error(self, *, response: Response | None, **kwargs: Any) -> None:
        if response is None:
            return
        pending = self._pending_sitemap_pages.get(response)
        if pending is not None:
            pending.failed = True
            pending.finished_items += 1
            self._finish_sitemap_page_if_ready(response, pending)

    def _is_item(self, output: Any) -> bool:
        return output is not None and not isinstance(output, Request)

    def sitemap_filter(
        self, entries: Iterable[dict[str, Any]]
    ) -> Iterable[dict[str, Any]]:
        """This method can be used to filter sitemap entries by their
        attributes, for example, you can filter locs with lastmod greater
        than a given date (see docs).
        """
        yield from entries

    def _parse_sitemap(self, response: Response) -> Iterable[Request]:
        try:
            if response.url.endswith("/robots.txt"):
                urls = list(
                    sitemap_urls_from_robots(response.body, base_url=response.url)
                )
                return self._follow_sitemaps(urls, response)

            body = self._get_sitemap_body(response)
            if not body:
                logger.warning(
                    "Ignoring invalid sitemap: %(response)s",
                    {"response": response},
                    extra={"spider": self},
                )
                return ()

            sitemap = Sitemap(body)

            if sitemap.type == "sitemapindex":
                urls = list(
                    self._get_urls_from_sitemapindex(self.sitemap_filter(sitemap))
                )
                return self._follow_sitemaps(urls, response)

            if sitemap.type == "urlset":
                if self.sitemap_incremental:
                    self._collect_sitemap_pages(self.sitemap_filter(sitemap))
                    return ()
                return (
                    Request(loc, callback=callback)
                    for loc, callback in self._get_urls_and_callbacks_from_urlset(
                        self.sitemap_filter(sitemap)
                    )
                )

            logger.warning(
                "Ignoring invalid sitemap: %(response)s",
                {"response": response},
                extra={"spider": self},
            )
            return ()
        finally:
            if (
                self.sitemap_incremental
                and response.request is not None
                and response.request.meta.get(_SITEMAP_KEY)
            ):
                self._finish_sitemap(response.request)

    def _follow_sitemaps(
        self, urls: Iterable[str], response: Response
    ) -> Iterable[Request]:
        if not self.sitemap_incremental or not response.meta.get(_SITEMAP_KEY):
            return (Request(url, callback=self._parse_sitemap) for url in urls)
        requests = [self._sitemap_request(url, parent=True) for url in urls]
        self._pending_sitemaps += len(requests)
        return requests

    def _finish_sitemap(self, request: Request) -> None:
        if self._pending_sitemaps:
            self._pending_sitemaps -= 1
        if self._pending_sitemaps == 0:
            self._schedule_sitemap_pages()

    def _collect_sitemap_pages(
        self, entries: Iterable[dict[str, Any]]
    ) -> None:
        for entry in entries:
            version = _parse_lastmod(entry.get("lastmod"))
            for loc in self._iter_matching_locs(entry):
                callback = self._callback_for_url(loc)
                if callback is None:
                    continue
                candidate = self._sitemap_candidates.setdefault(
                    loc, _SitemapPageCandidate(callback, [])
                )
                candidate.versions.append(version)
        if self._pending_sitemaps == 0:
            self._schedule_sitemap_pages()

    def _schedule_sitemap_pages(self) -> None:
        candidates = self._sitemap_candidates
        self._sitemap_candidates = {}
        for url, candidate in candidates.items():
            version = _select_lastmod(candidate.versions)
            stored_version = _lastmod_from_state(self._lastmods.get(url))
            if not self.sitemap_refresh and (
                version is not None
                and stored_version is not None
                and version.precision == stored_version.precision
                and version.value <= stored_version.value
            ):
                continue
            self.crawler.engine.crawl(
                self._page_request(url, candidate.callback, version)
            )

    def _callback_for_url(self, url: str) -> CallbackT | None:
        for pattern, callback in self._cbs:
            if pattern.search(url):
                return callback
        return None

    def _page_request(
        self, url: str, callback: CallbackT, version: _Lastmod | None
    ) -> Request:
        callback_data: CallbackT | str = callback
        if inspect.ismethod(callback):
            for name in dir(self):
                method = getattr(self, name)
                if inspect.ismethod(method) and method.__func__ is callback.__func__:
                    callback_data = name
                    break
        return Request(
            url,
            self._parse_sitemap_page,
            dont_filter=True,
            meta={_LASTMOD_KEY: None if version is None else version.as_state()},
            cb_kwargs={"callback": callback_data},
        )

    def _iter_matching_locs(self, entry: dict[str, Any]) -> Iterable[str]:
        for loc in iterloc((entry,), self.sitemap_alternate_links):
            if self._callback_for_url(loc) is not None:
                yield loc

    async def _parse_sitemap_page(
        self, response: Response, callback: CallbackT | str
    ) -> AsyncIterator[Any]:
        page_callback = (
            cast("CallbackT", getattr(self, callback))
            if isinstance(callback, str)
            else callback
        )
        version = _lastmod_from_state(response.meta.get(_LASTMOD_KEY))
        pending = _PendingSitemapPage(response.url, version)
        self._pending_sitemap_pages[response] = pending
        try:
            result = page_callback(response)
            if hasattr(result, "__aiter__"):
                async for output in result:
                    if self._is_item(output):
                        pending.items += 1
                    yield output
            elif hasattr(result, "__await__") or hasattr(result, "addCallback"):
                result = await maybe_deferred_to_future(result)
                for output in arg_to_iter(result):
                    if self._is_item(output):
                        pending.items += 1
                    yield output
            else:
                for output in arg_to_iter(result):
                    if self._is_item(output):
                        pending.items += 1
                    yield output
        except Exception:
            pending.failed = True
            raise
        finally:
            pending.callback_finished = True
            self._finish_sitemap_page_if_ready(response, pending)

    def _finish_sitemap_page_if_ready(
        self, response: Response, pending: _PendingSitemapPage
    ) -> None:
        if not pending.callback_finished or pending.finished_items != pending.items:
            return
        self._pending_sitemap_pages.pop(response, None)
        if not pending.failed and pending.version is not None:
            self._lastmods[pending.url] = pending.version.as_state()

    def _get_urls_from_sitemapindex(
        self, it: Iterable[dict[str, Any]]
    ) -> Iterable[str]:
        for loc in iterloc(it, self.sitemap_alternate_links):
            if any(x.search(loc) for x in self._follow):
                yield loc

    def _get_urls_and_callbacks_from_urlset(
        self, it: Iterable[dict[str, Any]]
    ) -> Iterable[tuple[str, CallbackT]]:
        for loc in iterloc(it, self.sitemap_alternate_links):
            for r, c in self._cbs:
                if r.search(loc):
                    yield loc, c
                    break

    def _get_sitemap_body(self, response: Response) -> bytes | None:
        """Return the sitemap body contained in the given response,
        or None if the response is not a sitemap.
        """
        if isinstance(response, XmlResponse):
            return response.body
        if gzip_magic_number(response):
            uncompressed_size = len(response.body)
            max_size = response.meta.get("download_maxsize", self._max_size)
            warn_size = response.meta.get("download_warnsize", self._warn_size)
            try:
                body = gunzip(response.body, max_size=max_size)
            except _DecompressionMaxSizeExceeded:
                return None
            if uncompressed_size < warn_size <= len(body):
                logger.warning(
                    f"{response} body size after decompression ({len(body)} B) "
                    f"is larger than the download warning size ({warn_size} B)."
                )
            return body
        # actual gzipped sitemap files are decompressed above ;
        # if we are here (response body is not gzipped)
        # and have a response for .xml.gz,
        # it usually means that it was already gunzipped
        # by HttpCompression middleware,
        # the HTTP response being sent with "Content-Encoding: gzip"
        # without actually being a .xml.gz file in the first place,
        # merely XML gzip-compressed on the fly,
        # in other word, here, we have plain XML
        if response.url.endswith(".xml") or response.url.endswith(".xml.gz"):
            return response.body
        return None


def regex(x: re.Pattern[str] | str) -> re.Pattern[str]:
    if isinstance(x, str):
        return re.compile(x)
    return x


def iterloc(it: Iterable[dict[str, Any]], alt: bool = False) -> Iterable[str]:
    for d in it:
        if loc := d["loc"]:
            yield loc

        # Also consider alternate URLs (xhtml:link rel="alternate")
        if alt and (alt_list := d.get("alternate")):
            yield from alt_list
