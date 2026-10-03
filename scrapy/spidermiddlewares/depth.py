"""
Depth Spider Middleware

See documentation in docs/topics/spider-middleware.rst
"""

from __future__ import annotations

import logging
import pickle
from collections import deque
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


class DepthMiddleware(BaseSpiderMiddleware):
    """Track the depth of each request within the site being scraped, setting
    ``request.meta["depth"]`` to 0 when there is no value previously set
    (usually just the first request) and incrementing it by 1 otherwise.

    It can be used to limit the maximum depth to scrape, control request
    priority based on their depth, and things like that, through the
    :setting:`DEPTH_LIMIT`, :setting:`DEPTH_STATS_VERBOSE` and
    :setting:`DEPTH_PRIORITY` settings.

    .. reqmeta:: depth_reset

    depth_reset
    -----------

    .. versionadded:: 2.18.0

    :attr:`~scrapy.Request.meta` key that, set to ``True``, gives a request
    depth 0 instead of the depth of its source response plus 1, e.g. to keep
    :setting:`DEPTH_LIMIT` from applying across a domain change.

    When :setting:`DEPTH_SHORTEST_DISCOVERY` is enabled, the middleware also
    tracks the shortest depth at which every request has been discovered and
    records the output requests of every downloaded response. When a page
    that has already been downloaded is discovered again through a strictly
    shorter path, its recorded output requests are emitted again at depths
    computed from the shorter path, so that requests dropped for exceeding
    :setting:`DEPTH_LIMIT` on the longer path get a new chance. The spider
    callback of the page is not run again and no items are produced again:
    the duplicate request itself is still filtered out by the duplicate
    filter, only the recorded output requests are emitted again. With
    :setting:`JOBDIR` set, the recorded depths and output requests are
    persisted into a :file:`requests.depths` file inside the job directory,
    so that resuming a crawl keeps using them.
    """

    crawler: Crawler

    def __init__(  # pylint: disable=super-init-not-called
        self,
        maxdepth: int,
        stats: StatsCollector,
        verbose_stats: bool = False,
        prio: int = 1,
        *,
        shortest_discovery: bool = False,
        jobdir: str | None = None,
    ):
        self.maxdepth = maxdepth
        self.stats = stats
        self.verbose_stats = verbose_stats
        self.prio = prio
        self._ignored_logged = False
        self._shortest_discovery = shortest_discovery
        self._jobdir = jobdir
        # Minimum discovery depth seen so far for each request fingerprint.
        self._min_depth: dict[bytes, int] = {}
        # Output requests recorded for each downloaded response, as
        # fingerprint -> {child fingerprint: (request template, relative
        # depth)}, where a relative depth of None means a depth_reset
        # request, which always gets depth 0.
        self._outputs: dict[bytes, dict[bytes, tuple[Request, int | None]]] = {}
        # Re-emitted requests waiting to be yielded from process_spider_output.
        self._pending: deque[Request] = deque()

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        settings = crawler.settings
        maxdepth = settings.getint("DEPTH_LIMIT")
        verbose = settings.getbool("DEPTH_STATS_VERBOSE")
        prio = settings.getint("DEPTH_PRIORITY")
        shortest = settings.getbool("DEPTH_SHORTEST_DISCOVERY")
        o = cls(
            maxdepth,
            crawler.stats,
            verbose,
            prio,
            shortest_discovery=shortest,
            jobdir=_job_dir(settings) if shortest else None,
        )
        o.crawler = crawler
        if shortest:
            crawler.signals.connect(o.spider_opened, signal=signals.spider_opened)
            crawler.signals.connect(o.spider_closed, signal=signals.spider_closed)
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
            if self._shortest_discovery:
                self._update_response_depth(response)
        yield from super().process_spider_output(response, result)
        yield from self._drain_pending()

    @_warn_spider_arg
    async def process_spider_output_async(
        self,
        response: Response | None,
        result: AsyncIterator[Any],
        spider: Spider | None = None,
    ) -> AsyncIterator[Any]:
        if response is not None:
            self._init_depth(response)
            if self._shortest_discovery:
                self._update_response_depth(response)
        async for o in super().process_spider_output_async(response, result):
            yield o
        while self._pending:
            yield self._pending.popleft()

    def _drain_pending(self) -> Iterable[Request]:
        while self._pending:
            yield self._pending.popleft()

    def _init_depth(self, response: Response) -> None:
        # base case (depth=0)
        if "depth" not in response.meta:
            response.meta["depth"] = 0
            if self.verbose_stats:
                self.stats.inc_value("request_depth_count/0")

    def _update_response_depth(self, response: Response) -> None:
        # The response URL may have been discovered through a shorter path
        # after its request was scheduled; its output requests must be
        # assigned depths relative to the shortest known discovery depth.
        assert response.request is not None
        fp = self.crawler.request_fingerprinter.fingerprint(response.request)
        depth = response.meta["depth"]
        known = self._min_depth.get(fp)
        if known is None:
            self._min_depth[fp] = depth
        elif known < depth:
            response.meta["depth"] = known

    def get_processed_request(
        self, request: Request, response: Response | None
    ) -> Request | None:
        # Consumed here so that it cannot reach response.meta and, from there,
        # spread to further requests through a meta copy.
        depth_reset = request.meta.pop("depth_reset", False)
        if response is None:
            # start requests
            return request
        if self._shortest_discovery:
            depth = self._record_output(response, request, depth_reset=depth_reset)
            return self._emit(request, depth)
        depth = 0 if depth_reset else response.meta["depth"] + 1
        return self._finalize(request, depth)

    def _record_output(
        self, response: Response, request: Request, *, depth_reset: bool
    ) -> int:
        """Record *request* as an output of *response* and return its depth."""
        assert response.request is not None
        parent_fp = self.crawler.request_fingerprinter.fingerprint(response.request)
        # A shorter path to the response URL may have been discovered while
        # its output was being processed.
        known = self._min_depth[parent_fp]
        response.meta["depth"] = min(known, response.meta["depth"])
        depth = 0 if depth_reset else response.meta["depth"] + 1
        rel_depth = None if depth_reset else depth - response.meta["depth"]
        child_fp = self.crawler.request_fingerprinter.fingerprint(request)
        self._outputs.setdefault(parent_fp, {})[child_fp] = (
            request.copy(),
            rel_depth,
        )
        return depth

    def _emit(self, request: Request, depth: int) -> Request | None:
        fp = self.crawler.request_fingerprinter.fingerprint(request)
        known = self._min_depth.get(fp)
        if known is None or depth < known:
            self._min_depth[fp] = depth
            if fp in self._outputs:
                self._reexpand(fp, depth)
        return self._finalize(request, depth)

    def _reexpand(self, fp: bytes, depth: int) -> None:
        # Re-emit the recorded output requests of a page rediscovered at a
        # strictly lower depth, so that requests dropped for exceeding
        # DEPTH_LIMIT get a new chance. Only strictly lower depths trigger
        # further re-expansions, so cycles cannot replay indefinitely.
        worklist = [(fp, depth)]
        while worklist:
            parent_fp, parent_depth = worklist.pop()
            for child_fp, (template, rel_depth) in self._outputs[parent_fp].items():
                child_depth = 0 if rel_depth is None else parent_depth + rel_depth
                known = self._min_depth.get(child_fp)
                assert known is not None  # recorded outputs were emitted once
                if child_depth >= known:
                    continue
                self._min_depth[child_fp] = child_depth
                if child_fp in self._outputs:
                    worklist.append((child_fp, child_depth))
                request = self._finalize(template.copy(), child_depth)
                if request is not None:
                    self.stats.inc_value("depth/request_reexpanded_count")
                    self._pending.append(request)

    def _finalize(self, request: Request, depth: int) -> Request | None:
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

    def spider_opened(self, spider: Spider) -> None:
        if self._jobdir is None:
            return
        path = Path(self._jobdir, "requests.depths")
        if not path.exists():
            return
        with path.open("rb") as f:
            min_depth, outputs = pickle.load(f)  # noqa: S301
        self._min_depth = {bytes.fromhex(fp): d for fp, d in min_depth.items()}
        self._outputs = {
            bytes.fromhex(parent_fp): {
                bytes.fromhex(child_fp): (
                    request_from_dict(template, spider=spider),
                    rel_depth,
                )
                for child_fp, (template, rel_depth) in children.items()
            }
            for parent_fp, children in outputs.items()
        }

    def spider_closed(self, spider: Spider) -> None:
        if self._jobdir is None:
            return
        min_depth = {fp.hex(): d for fp, d in self._min_depth.items()}
        outputs = {
            parent_fp.hex(): {
                child_fp.hex(): (template.to_dict(spider=spider), rel_depth)
                for child_fp, (template, rel_depth) in children.items()
            }
            for parent_fp, children in self._outputs.items()
        }
        try:
            data = pickle.dumps((min_depth, outputs), protocol=4)
        except Exception:
            logger.warning(
                f"Unable to serialize the depth state into {self._jobdir}",
                exc_info=True,
            )
            return
        Path(self._jobdir, "requests.depths").write_bytes(data)
