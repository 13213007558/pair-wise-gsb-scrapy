"""
Depth Spider Middleware

See documentation in docs/topics/spider-middleware.rst
"""

from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scrapy import signals
from scrapy.spidermiddlewares.base import BaseSpiderMiddleware
from scrapy.utils.conf import _job_dir
from scrapy.utils.decorators import _warn_spider_arg
from scrapy.utils.request import request_from_dict

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    # typing.Self requires Python 3.11
    from typing_extensions import Self

    from scrapy import Spider
    from scrapy.crawler import Crawler
    from scrapy.http import Request, Response
    from scrapy.statscollectors import StatsCollector


logger = logging.getLogger(__name__)


class _ShortestPathState:
    """State for the shortest-discovery-depth mode of :class:`DepthMiddleware`.

    Tracks, for every seen request fingerprint, the shallowest depth at
    which the request was discovered, and, for every downloaded response,
    its output requests as serialized templates paired with their depth
    relative to the depth of that response. When a downloaded page is
    discovered again at a strictly lower depth, its recorded outputs are
    scheduled again at depths relative to the new depth, so that links that
    were dropped for exceeding the maximum depth through a longer path are
    still followed, without downloading the page or running its callback
    again.
    """

    def __init__(self, crawler: Crawler):
        self.crawler = crawler
        self.maxdepth = crawler.settings.getint("DEPTH_LIMIT")
        self.prio = crawler.settings.getint("DEPTH_PRIORITY")
        self.min_depth: dict[bytes, int] = {}
        self.outputs: dict[bytes, list[tuple[int, dict[str, Any]]]] = {}
        self._path: Path | None = None
        jobdir = _job_dir(crawler.settings)
        if jobdir is not None:
            self._path = Path(jobdir, "requests.depths")
            if self._path.exists():
                with self._path.open("rb") as f:
                    self.min_depth, self.outputs = pickle.load(f)  # noqa: S301
        crawler.signals.connect(self._save, signals.spider_closed)

    def track(self, request: Request, response: Response, depth: int) -> None:
        """Record *request*, an output of *response* to be scheduled at
        *depth*, and replay recorded outputs if *request* had already been
        downloaded after being discovered at a higher depth."""
        if request.dont_filter:
            # dont_filter requests bypass the dupe filter, so tracking
            # their discovery depth or re-scheduling them makes no sense.
            return
        assert response.request is not None
        parent_fp = self.crawler.request_fingerprinter.fingerprint(response.request)
        relative_depth = depth - response.meta["depth"]
        try:
            template = request.to_dict(spider=self.crawler.spider)
        except ValueError:
            # Requests that cannot be serialized, e.g. because of their
            # callback, cannot be recorded for re-scheduling.
            pass
        else:
            # to_dict() does not copy meta, so copy it here to keep later
            # changes to request.meta out of the recorded template.
            template["meta"] = dict(template["meta"])
            if depth == 0:
                # The request had depth_reset set: its depth must stay 0
                # no matter which depth it is later replayed from.
                template["meta"]["depth_reset"] = True
            self.outputs.setdefault(parent_fp, []).append((relative_depth, template))
        fp = self.crawler.request_fingerprinter.fingerprint(request)
        known = self.min_depth.get(fp)
        if known is None or depth < known:
            self.min_depth[fp] = depth
            if known is not None and fp in self.outputs:
                self._replay(fp, depth)

    def improve(self, response: Response) -> None:
        """Replay the outputs of *response* at a lower depth if its request
        was discovered at a lower depth than the one it was downloaded at,
        e.g. because a shorter path was found while it waited in the
        scheduler queue."""
        assert response.request is not None
        fp = self.crawler.request_fingerprinter.fingerprint(response.request)
        known = self.min_depth.get(fp)
        if known is not None and known < response.meta["depth"] and fp in self.outputs:
            self._replay(fp, known)

    def _replay(self, fp: bytes, depth: int) -> None:
        """Schedule the outputs recorded for *fp* at depths relative to
        *depth*, and do the same recursively for those outputs whose own
        shallowest known depth improves as a result. Depths only ever
        decrease, so replays cannot loop indefinitely."""
        stack = [(fp, depth)]
        while stack:
            fp, depth = stack.pop()
            for relative_depth, template in self.outputs.get(fp, []):
                request = request_from_dict(template, spider=self.crawler.spider)
                if request.meta.pop("depth_reset", False):
                    child_depth = 0
                else:
                    child_depth = depth + relative_depth
                if self.maxdepth and child_depth > self.maxdepth:
                    continue
                request.meta["depth"] = child_depth
                if self.prio:
                    request.priority -= child_depth * self.prio
                child_fp = self.crawler.request_fingerprinter.fingerprint(request)
                known = self.min_depth.get(child_fp)
                if known is None or child_depth < known:
                    self.min_depth[child_fp] = child_depth
                    if child_fp in self.outputs:
                        stack.append((child_fp, child_depth))
                if child_fp not in self.outputs:
                    # Not downloaded yet, so scheduling it is not a duplicate
                    # download; if it turns out to be scheduled already, the
                    # dupe filter drops it.
                    self.crawler.engine.crawl(request)

    def _save(self) -> None:
        if self._path is not None:
            with self._path.open("wb") as f:
                pickle.dump((self.min_depth, self.outputs), f)


class DepthMiddleware(BaseSpiderMiddleware):
    """Track the depth of each request within the site being scraped, setting
    ``request.meta["depth"]`` to 0 when there is no value previously set
    (usually just the first request) and incrementing it by 1 otherwise.

    It can be used to limit the maximum depth to scrape, control request
    priority based on their depth, and things like that, through the
    :setting:`DEPTH_LIMIT`, :setting:`DEPTH_STATS_VERBOSE` and
    :setting:`DEPTH_PRIORITY` settings.

    When the :setting:`DEPTH_SHORTEST_PATH` setting is enabled, the
    middleware also makes sure that every page is crawled as if it had been
    reached through the shortest path found to it, regardless of the order
    in which pages are discovered.

    .. reqmeta:: depth_reset

    depth_reset
    -----------

    .. versionadded:: 2.18.0

    :attr:`~scrapy.Request.meta` key that, set to ``True``, gives a request
    depth 0 instead of the depth of its source response plus 1, e.g. to keep
    :setting:`DEPTH_LIMIT` from applying across a domain change.
    """

    crawler: Crawler

    def __init__(  # pylint: disable=super-init-not-called
        self,
        maxdepth: int,
        stats: StatsCollector,
        verbose_stats: bool = False,
        prio: int = 1,
    ):
        self.maxdepth = maxdepth
        self.stats = stats
        self.verbose_stats = verbose_stats
        self.prio = prio
        self._ignored_logged = False
        self._shortest_path: _ShortestPathState | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        settings = crawler.settings
        maxdepth = settings.getint("DEPTH_LIMIT")
        verbose = settings.getbool("DEPTH_STATS_VERBOSE")
        prio = settings.getint("DEPTH_PRIORITY")
        o = cls(maxdepth, crawler.stats, verbose, prio)
        if settings.getbool("DEPTH_SHORTEST_PATH"):
            o._shortest_path = _ShortestPathState(crawler)
        o.crawler = crawler
        return o

    @_warn_spider_arg
    def process_spider_output(
        self,
        response: Response | None,
        result: Iterable[Any],
        spider: Spider | None = None,
    ) -> Iterable[Any]:
        if response is not None:
            self._init_depth(response)
        yield from super().process_spider_output(response, result)
        if response is not None and self._shortest_path is not None:
            self._shortest_path.improve(response)

    @_warn_spider_arg
    async def process_spider_output_async(
        self,
        response: Response | None,
        result: AsyncIterator[Any],
        spider: Spider | None = None,
    ) -> AsyncIterator[Any]:
        if response is not None:
            self._init_depth(response)
        async for o in super().process_spider_output_async(response, result):
            yield o
        if response is not None and self._shortest_path is not None:
            self._shortest_path.improve(response)

    def _init_depth(self, response: Response) -> None:
        # base case (depth=0)
        if "depth" not in response.meta:
            response.meta["depth"] = 0
            if self.verbose_stats:
                self.stats.inc_value("request_depth_count/0")

    def get_processed_request(
        self, request: Request, response: Response | None
    ) -> Request | None:
        # Consumed here so that it cannot reach response.meta and, from there,
        # spread to further requests through a meta copy.
        depth_reset = request.meta.pop("depth_reset", False)
        if response is None:
            # start requests
            return request
        depth = 0 if depth_reset else response.meta["depth"] + 1
        if self._shortest_path is not None:
            self._shortest_path.track(request, response, depth)
        request.meta["depth"] = depth
        if self.prio:
            request.priority -= depth * self.prio
        if self.maxdepth and depth > self.maxdepth:
            if not self._ignored_logged:
                logger.debug(
                    f"Ignoring link (depth > {self.maxdepth}): {request.url}"
                    " - no more ignored links will be shown",
                    extra={"spider": self.crawler.spider},
                )
                self._ignored_logged = True
            self.stats.inc_value("depth/request_ignored_count")
            return None
        if self.verbose_stats:
            self.stats.inc_value(f"request_depth_count/{depth}")
        self.stats.max_value("request_depth_max", depth)
        return request
