from __future__ import annotations

import asyncio
import functools
import logging
import warnings
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, TypedDict, cast

from twisted.internet.defer import Deferred, DeferredList
from twisted.python.failure import Failure

from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http.request import NO_CALLBACK, Request
from scrapy.utils._deps_compat import TWISTED_FAILURE_HAS_STACK
from scrapy.utils.asyncio import is_asyncio_available
from scrapy.utils.datatypes import SequenceExclude
from scrapy.utils.decorators import _warn_spider_arg
from scrapy.utils.defer import (
    _process_pending_io,
    deferred_from_coro,
    ensure_awaitable,
    maybe_deferred_to_future,
)
from scrapy.utils.log import failure_to_exc_info
from scrapy.utils.misc import arg_to_iter
from scrapy.utils.python import global_object_name

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    # typing.Self requires Python 3.11
    from typing_extensions import Self

    from scrapy import Spider
    from scrapy.crawler import Crawler
    from scrapy.http import Response
    from scrapy.settings import Settings
    from scrapy.utils.request import RequestFingerprinterProtocol


class FileInfo(TypedDict):
    url: str
    path: str
    checksum: str | None
    status: str


FileInfoOrError: TypeAlias = (
    tuple[Literal[True], FileInfo] | tuple[Literal[False], Failure]
)


class _RecoveredDownload:
    """Sentinel for download failures that :meth:`MediaPipeline.media_failed`
    recovered from by returning instead of raising."""


_RECOVERED_DOWNLOAD = _RecoveredDownload()

logger = logging.getLogger(__name__)


class FileException(Exception):
    """General media error exception"""


class _MediaRequestFiltered(FileException):
    """Raised internally by media pipelines when a media request is filtered
    out (e.g. as an offsite request) instead of being downloaded.

    It is a subclass of :exc:`FileException` for backward compatibility, but
    unlike an actual download error it is logged at the ``DEBUG`` level and
    without a traceback, since filtering a request is expected behavior rather
    than an error.
    """


def _media_request_filtered(failure: Failure) -> bool:
    return isinstance(failure.value, _MediaRequestFiltered)


class MediaPipeline(ABC):
    LOG_FAILED_RESULTS: bool = True

    class SpiderInfo:
        def __init__(self, spider: Spider):
            self.spider: Spider = spider
            self.downloading: set[bytes] = set()
            self.downloaded: dict[bytes, Response | Failure | _RecoveredDownload] = (
                {}
            )
            self.waiting: defaultdict[
                bytes, list[Deferred[Response | _RecoveredDownload]]
            ] = defaultdict(list)

    def __init__(
        self,
        download_func: None = None,
        *,
        crawler: Crawler,
    ):
        if download_func is not None:  # pragma: no cover
            warnings.warn(
                "The download_func argument of MediaPipeline.__init__() is ignored"
                " and will be removed in a future Scrapy version.",
                category=ScrapyDeprecationWarning,
                stacklevel=2,
            )
        self.crawler: Crawler = crawler
        self._fingerprinter: RequestFingerprinterProtocol = (
            crawler.request_fingerprinter
        )

        settings = crawler.settings
        resolve = functools.partial(
            self._key_for_pipe, base_class_name="MediaPipeline", settings=settings
        )
        self.allow_redirects: bool = settings.getbool(
            resolve("MEDIA_ALLOW_REDIRECTS"), False
        )
        self._handle_statuses(self.allow_redirects)

    def _handle_statuses(self, allow_redirects: bool) -> None:
        self.handle_httpstatus_list = None
        if allow_redirects:
            self.handle_httpstatus_list = SequenceExclude(range(300, 400))

    def _key_for_pipe(
        self,
        key: str,
        base_class_name: str | None = None,
        settings: Settings | None = None,
    ) -> str:
        class_name = self.__class__.__name__
        formatted_key = f"{class_name.upper()}_{key}"
        if (
            not base_class_name
            or class_name == base_class_name
            or (settings and not settings.get(formatted_key))
        ):
            return key
        return formatted_key

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls(crawler=crawler)

    @_warn_spider_arg
    def open_spider(self, spider: Spider | None = None) -> None:
        assert self.crawler.spider
        self.spiderinfo = self.SpiderInfo(self.crawler.spider)

    @_warn_spider_arg
    async def process_item(self, item: Any, spider: Spider | None = None) -> Any:
        info = self.spiderinfo
        requests = arg_to_iter(self.get_media_requests(item, info))
        coros = [self._process_request(r, info, item) for r in requests]
        results: list[FileInfoOrError] = []
        if coros:
            if is_asyncio_available():
                results_asyncio = await asyncio.gather(*coros, return_exceptions=True)
                for res in results_asyncio:
                    if isinstance(res, BaseException):
                        results.append((False, Failure(res)))
                    else:
                        results.append((True, res))
            else:
                results = await cast(
                    "Deferred[list[FileInfoOrError]]",
                    DeferredList(
                        (deferred_from_coro(coro) for coro in coros), consumeErrors=True
                    ),
                )
        return self.item_completed(results, item, info)

    async def _process_request(
        self, request: Request, info: SpiderInfo, item: Any
    ) -> FileInfo:
        fp = self._fingerprinter.fingerprint(request)

        eb: Callable[[Failure], FileInfo] | None = request.errback
        request.callback = NO_CALLBACK
        request.errback = None

        try:
            # A failed download is shared by all items, so there is no
            # per-item processing to do for it.
            cached = info.downloaded.get(fp)
            if isinstance(cached, Failure):
                await _process_pending_io()
                cached.raiseException()

            try:
                file_info: FileInfo | None = await ensure_awaitable(
                    self.media_to_download(request, info, item=item)
                )
            except Exception:
                logger.exception(Failure())
                raise
            if file_info:
                # got a result without downloading
                return file_info

            response = await self._download(request, info, fp)
            if isinstance(response, _RecoveredDownload):
                # media_failed() recovered from the download failure
                return cast("FileInfo", None)

            try:
                return await ensure_awaitable(
                    self.media_downloaded(response, request, info, item=item)
                )
            except Exception:
                self._media_failed(request, info)
                return cast("FileInfo", None)
        except Exception:
            if eb is None:
                raise
            result = eb(Failure())
            if isinstance(result, Failure):
                result.raiseException()
            return result

    async def _download(
        self, request: Request, info: SpiderInfo, fp: bytes
    ) -> Response | _RecoveredDownload:
        """Download *request* once and share the response with all waiters.

        Download failures are raised, unless :meth:`media_failed` recovers
        from them, in which case a sentinel is returned instead.
        """
        # Return the cached download result if the request was already seen
        if fp in info.downloaded:
            await _process_pending_io()
            cached = info.downloaded[fp]
            if isinstance(cached, Failure):
                cached.raiseException()
            return cached

        # Otherwise, wait for the download to finish
        wad: Deferred[Response | _RecoveredDownload] = Deferred()
        info.waiting[fp].append(wad)

        # Check if the request is downloading right now to avoid doing it twice
        if fp in info.downloading:
            return await maybe_deferred_to_future(wad)

        # Download the request
        info.downloading.add(fp)
        await _process_pending_io()
        result: Response | Failure | _RecoveredDownload
        try:
            self._modify_media_request(request)
            result = await self.crawler.engine.download_async(request)
        except Exception:
            try:
                self._media_failed(request, info)
            except Exception:
                result = Failure()
            else:
                result = _RECOVERED_DOWNLOAD
        await _process_pending_io()
        self._cache_result_and_execute_waiters(result, fp, info)
        return await maybe_deferred_to_future(wad)  # it must return wad at last

    def _media_failed(self, request: Request, info: SpiderInfo) -> None:
        """Run :meth:`media_failed` for the exception being handled.

        Return if :meth:`media_failed` recovered from the failure by
        returning, re-raise the failure exception otherwise.
        """
        try:
            result = self.media_failed(Failure(), request, info)
        except Exception:
            failure = Failure()
            if not _media_request_filtered(failure):
                logger.exception(failure)
            raise
        if isinstance(result, Failure):
            warnings.warn(
                f"{global_object_name(self.media_failed)} returned a Failure instance."
                f" This is deprecated, please raise an exception instead, e.g. via failure.raiseException().",
                category=ScrapyDeprecationWarning,
                stacklevel=2,
            )
            result.raiseException()

    def _modify_media_request(self, request: Request) -> None:
        if self.handle_httpstatus_list:
            request.meta["handle_httpstatus_list"] = self.handle_httpstatus_list
        else:
            request.meta["handle_httpstatus_all"] = True

    def _cache_result_and_execute_waiters(
        self,
        result: Response | Failure | _RecoveredDownload,
        fp: bytes,
        info: SpiderInfo,
    ) -> None:
        if isinstance(result, Failure):
            # minimize cached information for failure
            result.cleanFailure()
            result.frames.clear()
            if TWISTED_FAILURE_HAS_STACK:
                result.stack.clear()
            # This code fixes a memory leak by avoiding to keep references to
            # the Request and Response objects on the Media Pipeline cache.
            #
            # What happens when the media_downloaded callback raises an
            # exception, for example a FileException('download-error') when
            # the Response status code is not 200 OK, is that the original
            # StopIteration exception (which in turn contains the failed
            # Response and by extension, the original Request) gets encapsulated
            # within the FileException context.
            #
            # Originally, Scrapy was using twisted.internet.defer.returnValue
            # inside functions decorated with twisted.internet.defer.inlineCallbacks,
            # encapsulating the returned Response in a _DefGen_Return exception
            # instead of a StopIteration.
            #
            # To avoid keeping references to the Response and therefore Request
            # objects on the Media Pipeline cache, we should wipe the context of
            # the encapsulated exception when it is a StopIteration instance
            context = getattr(result.value, "__context__", None)
            if isinstance(context, StopIteration):
                assert result.value is not None
                result.value.__context__ = None

        info.downloading.remove(fp)
        info.downloaded[fp] = result  # cache result
        for wad in info.waiting.pop(fp):
            if isinstance(result, Failure):
                wad.errback(result)
            else:
                wad.callback(result)

    # Overridable Interface
    @abstractmethod
    def media_to_download(
        self, request: Request, info: SpiderInfo, *, item: Any = None
    ) -> Deferred[FileInfo | None] | None:
        """Check request before starting download"""
        raise NotImplementedError

    @abstractmethod
    def get_media_requests(self, item: Any, info: SpiderInfo) -> list[Request]:
        """Returns the media requests to download"""
        raise NotImplementedError

    @abstractmethod
    def media_downloaded(
        self,
        response: Response,
        request: Request,
        info: SpiderInfo,
        *,
        item: Any = None,
    ) -> FileInfo | Awaitable[FileInfo]:
        """Handler for success downloads"""
        raise NotImplementedError

    @abstractmethod
    def media_failed(
        self, failure: Failure, request: Request, info: SpiderInfo
    ) -> Failure:
        """Handler for failed downloads"""
        raise NotImplementedError

    def item_completed(
        self, results: list[FileInfoOrError], item: Any, info: SpiderInfo
    ) -> Any:
        """Called per item when all media requests has been processed"""
        if self.LOG_FAILED_RESULTS:
            for ok, value in results:
                if not ok:
                    assert isinstance(value, Failure)
                    if _media_request_filtered(value):
                        continue
                    logger.error(
                        "%(class)s found errors processing %(item)s",
                        {"class": self.__class__.__name__, "item": item},
                        exc_info=failure_to_exc_info(value),
                        extra={"spider": info.spider},
                    )
        return item

    @abstractmethod
    def file_path(
        self,
        request: Request,
        response: Response | None = None,
        info: SpiderInfo | None = None,
        *,
        item: Any = None,
    ) -> str:
        """Returns the path where downloaded media should be stored"""
        raise NotImplementedError
