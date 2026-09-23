from __future__ import annotations

import logging
import pickle
import re

# Iterable is needed at the run time for the SitemapSpider._parse_sitemap() annotation
from collections.abc import AsyncIterator, Iterable, Sequence  # noqa: TC003
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from twisted.internet.defer import Deferred

from scrapy import signals
from scrapy.http import Request, Response, XmlResponse
from scrapy.spiders import Spider
from scrapy.utils._compression import (
    _DecompressionMaxSizeExceeded,
    gunzip,
    gzip_magic_number,
)
from scrapy.utils._sitemap import Sitemap, sitemap_urls_from_robots
from scrapy.utils.asyncgen import as_async_generator
from scrapy.utils.defer import maybe_deferred_to_future
from scrapy.utils.spider import iterate_spider_output

if TYPE_CHECKING:
    # typing.Self requires Python 3.11
    from typing_extensions import Self

    from twisted.python.failure import Failure

    from scrapy.crawler import Crawler
    from scrapy.http.request import CallbackT

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

#: Name of the file inside the JOBDIR directory where processed URL versions
#: are stored when SitemapSpider.sitemap_incremental is enabled.
_STATE_FILE_NAME = "sitemap_incremental.state"

#: Request.meta key that marks page requests scheduled in incremental mode
#: and maps their items back to the sitemap URL they were scheduled for.
_META_KEY = "_sitemap_incremental"


class _PageState:
    """Processing state of a single sitemap URL scheduled in incremental
    mode."""

    __slots__ = ("callback_done", "failed", "items_left", "url")

    def __init__(self, url: str) -> None:
        self.url = url
        self.items_left = 0
        self.callback_done = False
        self.failed = False



class SitemapSpider(Spider):
    sitemap_urls: Sequence[str] = ()
    sitemap_rules: Sequence[tuple[re.Pattern[str] | str, str | CallbackT]] = [
        ("", "parse")
    ]
    sitemap_follow: Sequence[re.Pattern[str] | str] = [""]
    sitemap_alternate_links: bool = False
    sitemap_incremental: bool = False
    sitemap_incremental_force: bool = False
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
        if spider.sitemap_incremental:
            spider._setup_incremental(crawler)
        return spider

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self._cbs: list[tuple[re.Pattern[str], CallbackT]] = []
        for r, c in self.sitemap_rules:
            if isinstance(c, str):
                c = cast("CallbackT", getattr(self, c))  # noqa: PLW2901
            self._cbs.append((regex(r), c))
        self._follow: list[re.Pattern[str]] = [regex(x) for x in self.sitemap_follow]
        self._versions: dict[str, date | datetime] = {}
        self._candidates: dict[str, date | datetime | None] = {}
        self._scheduled: set[str] = set()
        self._pages: dict[str, _PageState] = {}
        self._completed: set[str] = set()
        self._state_file: Path | None = None

    def _setup_incremental(self, crawler: Crawler) -> None:
        # circular import
        from scrapy.utils.conf import _job_dir  # noqa: PLC0415

        force: object = self.sitemap_incremental_force
        if isinstance(force, str):
            force = force.strip().lower() not in ("", "0", "false", "no", "off")
        jobdir = _job_dir(crawler.settings)
        if jobdir is None:
            logger.warning(
                "sitemap_incremental is enabled but the JOBDIR setting is not "
                "set, processed URL versions will not be persisted."
            )
        else:
            self._state_file = Path(jobdir, _STATE_FILE_NAME)
            if force:
                logger.info(
                    "sitemap_incremental_force is set, ignoring previously "
                    "stored sitemap URL versions."
                )
            else:
                self._load_versions()
        crawler.signals.connect(self._on_item_scraped, signal=signals.item_scraped)
        crawler.signals.connect(self._on_item_dropped, signal=signals.item_dropped)
        crawler.signals.connect(self._on_item_error, signal=signals.item_error)
        crawler.signals.connect(self._on_spider_closed, signal=signals.spider_closed)

    def _load_versions(self) -> None:
        assert self._state_file is not None
        if not self._state_file.exists():
            return
        try:
            with self._state_file.open("rb") as f:
                data = pickle.load(f)  # noqa: S301
        except (OSError, EOFError, ValueError, pickle.PickleError) as e:
            logger.warning(
                f"Ignoring unreadable sitemap versions file {self._state_file}: {e}"
            )
            return
        if not isinstance(data, dict):
            logger.warning(
                f"Ignoring invalid sitemap versions file {self._state_file}."
            )
            return
        for url, raw in data.items():
            if not isinstance(url, str) or not isinstance(raw, str):
                continue
            parsed = _parse_lastmod(raw)
            if parsed is not None:
                self._versions[url] = parsed
        logger.debug(
            f"Loaded {len(self._versions)} sitemap URL versions "
            f"from {self._state_file}."
        )

    def _on_spider_closed(self, spider: Spider) -> None:
        for url in self._completed:
            candidate = self._candidates.get(url)
            if candidate is not None:
                self._versions[url] = candidate
        if self._state_file is None:
            return
        data = {url: version.isoformat() for url, version in self._versions.items()}
        try:
            with self._state_file.open("wb") as f:
                pickle.dump(data, f, protocol=4)
        except OSError as e:
            logger.warning(
                f"Could not write sitemap versions file {self._state_file}: {e}"
            )

    async def start(self) -> AsyncIterator[Any]:
        for url in self.sitemap_urls:
            yield Request(url, self._parse_sitemap)

    def sitemap_filter(
        self, entries: Iterable[dict[str, Any]]
    ) -> Iterable[dict[str, Any]]:
        """This method can be used to filter sitemap entries by their
        attributes, for example, you can filter locs with lastmod greater
        than a given date (see docs).
        """
        yield from entries

    def _parse_sitemap(self, response: Response) -> Iterable[Request]:
        if response.url.endswith("/robots.txt"):
            urls = list(sitemap_urls_from_robots(response.body, base_url=response.url))
            return (Request(url, callback=self._parse_sitemap) for url in urls)

        body = self._get_sitemap_body(response)
        if not body:
            logger.warning(
                "Ignoring invalid sitemap: %(response)s",
                {"response": response},
                extra={"spider": self},
            )
            return ()

        s = Sitemap(body)

        if s.type == "sitemapindex":
            urls = list(self._get_urls_from_sitemapindex(self.sitemap_filter(s)))
            return (Request(loc, callback=self._parse_sitemap) for loc in urls)

        if s.type == "urlset":
            url_callback_pairs = list(
                self._get_urls_and_callbacks_from_urlset(self.sitemap_filter(s))
            )
            return (self._page_request(loc, c) for loc, c in url_callback_pairs)

        logger.warning(
            "Ignoring invalid sitemap: %(response)s",
            {"response": response},
            extra={"spider": self},
        )

        return ()

    def _get_urls_from_sitemapindex(
        self, it: Iterable[dict[str, Any]]
    ) -> Iterable[str]:
        for loc in iterloc(it, self.sitemap_alternate_links):
            if any(x.search(loc) for x in self._follow):
                yield loc

    def _get_urls_and_callbacks_from_urlset(
        self, it: Iterable[dict[str, Any]]
    ) -> Iterable[tuple[str, CallbackT]]:
        for loc, lastmod in _iterloc(it, self.sitemap_alternate_links):
            for r, c in self._cbs:
                if r.search(loc):
                    if self._should_schedule(loc, lastmod):
                        yield loc, c
                    break

    def _should_schedule(self, loc: str, lastmod: str | None) -> bool:
        """Decide whether a URL from a sitemap should be scheduled,
        merging its *lastmod* into the candidates seen for it in this run
        when incremental mode is enabled."""
        if not self.sitemap_incremental:
            return True
        self._merge_candidate(loc, lastmod)
        if loc in self._scheduled:
            return False
        candidate = self._candidates[loc]
        stored = self._versions.get(loc)
        if (
            stored is None
            or candidate is None
            or type(stored) is not type(candidate)
            or candidate > stored
        ):
            self._scheduled.add(loc)
            return True
        logger.debug(f"Skipping {loc}, its sitemap lastmod did not change.")
        return False

    def _merge_candidate(self, loc: str, lastmod: str | None) -> None:
        """Merge the *lastmod* of a sitemap entry into the candidate
        version of *loc* for this run, keeping the newest comparable value
        and falling back to ``None`` (always recrawl, never recorded) when
        values cannot be compared."""
        parsed = _parse_lastmod(lastmod)
        if loc not in self._candidates:
            self._candidates[loc] = parsed
            return
        previous = self._candidates[loc]
        if previous is None or parsed is None or type(previous) is not type(parsed):
            self._candidates[loc] = None
        else:
            self._candidates[loc] = max(previous, parsed)

    def _page_request(self, loc: str, callback: CallbackT) -> Request:
        if not self.sitemap_incremental:
            return Request(loc, callback=callback)
        return Request(
            loc,
            callback=self._make_page_callback(loc, callback),
            meta={_META_KEY: loc},
        )

    def _make_page_callback(self, url: str, callback: CallbackT) -> CallbackT:
        state = _PageState(url)
        self._pages[url] = state

        def tracked_callback(response: Response, **cb_kwargs: Any) -> AsyncIterator[Any]:
            output = callback(response, **cb_kwargs)
            return self._track_page_output(state, output)

        return tracked_callback

    async def _track_page_output(
        self, state: _PageState, output: Any
    ) -> AsyncIterator[Any]:
        """Yield the callback output, counting items so that the URL is
        only marked as completed once the callback has finished and all its
        items have gone through the item pipelines."""
        result: Any = iterate_spider_output(output)
        if isinstance(result, Deferred):
            result = await maybe_deferred_to_future(result)
        async for item_or_request in as_async_generator(result):
            if item_or_request is None or isinstance(item_or_request, Request):
                yield item_or_request
            else:
                state.items_left += 1
                yield item_or_request
        state.callback_done = True
        self._maybe_complete_page(state)

    def _maybe_complete_page(self, state: _PageState) -> None:
        if state.failed or not state.callback_done or state.items_left:
            return
        self._completed.add(state.url)
        self._pages.pop(state.url, None)

    def _on_item_scraped(self, item: Any, response: Response, spider: Spider) -> None:
        self._item_processed(response)

    def _on_item_dropped(
        self, item: Any, response: Response, exception: Exception, spider: Spider
    ) -> None:
        # Items dropped with DropItem count as processed.
        self._item_processed(response)

    def _on_item_error(
        self, item: Any, response: Response, spider: Spider, failure: Failure
    ) -> None:
        self._item_processed(response, failed=True)

    def _item_processed(self, response: Response | None, *, failed: bool = False) -> None:
        request = getattr(response, "request", None)
        if request is None:
            return
        url = request.meta.get(_META_KEY)
        if url is None:
            return
        state = self._pages.get(url)
        if state is None:
            return
        state.items_left -= 1
        if failed:
            state.failed = True
        self._maybe_complete_page(state)

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


def _parse_lastmod(value: str | None) -> date | datetime | None:
    """Parse a sitemap ``lastmod`` value.

    Return a :class:`~datetime.date` for date-only values and a
    timezone-aware :class:`~datetime.datetime` for values with time and
    timezone information, so that values of different precision are never
    compared to each other. Return ``None`` for missing, invalid or
    timezone-naive values.
    """
    if not value:
        return None
    value = value.strip()
    if _DATE_RE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(None) is None:
        return None
    return parsed


def iterloc(it: Iterable[dict[str, Any]], alt: bool = False) -> Iterable[str]:
    for loc, _lastmod in _iterloc(it, alt):
        yield loc


def _iterloc(
    it: Iterable[dict[str, Any]], alt: bool = False
) -> Iterable[tuple[str, str | None]]:
    for d in it:
        lastmod = d.get("lastmod")
        if loc := d["loc"]:
            yield loc, lastmod

        # Also consider alternate URLs (xhtml:link rel="alternate")
        if alt and (alt_list := d.get("alternate")):
            for link in alt_list:
                yield link, lastmod
