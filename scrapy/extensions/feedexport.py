"""
Feed Exports extension

See documentation in docs/topics/feed-exports.rst
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import sys
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from tempfile import NamedTemporaryFile
from typing import IO, TYPE_CHECKING, Any, Protocol, TypeAlias, cast
from urllib.parse import unquote, urlparse
from uuid import uuid4

from twisted.internet.defer import Deferred, DeferredList
from w3lib.url import file_uri_to_path
from zope.interface import Interface

from scrapy import Spider, signals
from scrapy.exceptions import CloseSpider, NotConfigured, ScrapyDeprecationWarning
from scrapy.extensions.postprocessing import PostProcessingManager
from scrapy.utils._ftp import ftp_store_file
from scrapy.utils.asyncio import is_asyncio_available, run_in_thread
from scrapy.utils.boto import _get_max_pool_connections
from scrapy.utils.conf import feed_complete_default_values_from_settings
from scrapy.utils.defer import deferred_from_coro, ensure_awaitable
from scrapy.utils.misc import build_from_crawler, load_object
from scrapy.utils.python import without_none_values

if TYPE_CHECKING:
    from _typeshed import OpenBinaryMode

    # typing.Self requires Python 3.11
    from typing_extensions import Self

    from scrapy.crawler import Crawler
    from scrapy.exporters import BaseItemExporter
    from scrapy.settings import BaseSettings, Settings


logger = logging.getLogger(__name__)


# Printf-style placeholders (e.g. %(time)s) used to build feed URIs. Any other
# percent character in a URI (e.g. percent-encoding such as %20 or %23) must be
# treated as a literal rather than as the start of a placeholder.
_FEED_URI_PLACEHOLDER_RE = re.compile(
    r"%\([^)]+\)[-+ #0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[diouxXeEfFgGcrsa]"
)


def apply_uri_params(uri_template: str, uri_params: dict[str, Any]) -> str:
    """Return *uri_template* with its ``%(...)s`` placeholders replaced using
    *uri_params*, leaving any other percent character untouched.

    This allows feed URIs to contain percent-encoded characters (e.g. ``%20``
    in a path with spaces or ``%23`` in FTP credentials) without them being
    misinterpreted as printf-style formatting directives.
    """
    parts: list[str] = []
    last = 0
    for match in _FEED_URI_PLACEHOLDER_RE.finditer(uri_template):
        parts.append(uri_template[last : match.start()].replace("%", "%%"))
        parts.append(match.group(0))
        last = match.end()
    parts.append(uri_template[last:].replace("%", "%%"))
    return "".join(parts) % uri_params


UriParamsCallableT: TypeAlias = Callable[
    [dict[str, Any], Spider], dict[str, Any] | None
]


class ItemFilter:
    """
    This will be used by FeedExporter to decide if an item should be allowed
    to be exported to a particular feed.

    :param feed_options: feed specific options passed from FeedExporter
    :type feed_options: dict
    """

    feed_options: dict[str, Any] | None
    item_classes: tuple[type, ...]

    def __init__(self, feed_options: dict[str, Any] | None) -> None:
        self.feed_options = feed_options
        if feed_options is not None:
            self.item_classes = tuple(
                load_object(item_class)
                for item_class in feed_options.get("item_classes") or ()
            )
        else:
            self.item_classes = ()

    def accepts(self, item: Any) -> bool:
        """
        Return ``True`` if `item` should be exported or ``False`` otherwise.

        :param item: scraped item which user wants to check if is acceptable
        :type item: :ref:`Scrapy items <topics-items>`
        :return: `True` if accepted, `False` otherwise
        :rtype: bool
        """
        if self.item_classes:
            return isinstance(item, self.item_classes)
        return True  # accept all items by default


class _IFeedStorage(Interface):  # type: ignore[misc]  # pragma: no cover
    # pylint: disable=no-self-argument

    def __init__(uri, *, feed_options=None): ...  # type: ignore[no-untyped-def]  # pylint: disable=super-init-not-called

    def open(spider): ...  # type: ignore[no-untyped-def]

    def store(file): ...  # type: ignore[no-untyped-def]


class FeedStorageProtocol(Protocol):
    """Protocol that all Feed Storages must follow."""

    def __init__(self, uri: str, *, feed_options: dict[str, Any] | None = None):
        """Initialize the storage with the parameters given in the URI and the
        feed-specific options (see :setting:`FEEDS`)"""

    def open(self, spider: Spider) -> IO[bytes]:
        """Open the storage for the given spider. It must return a file-like
        object that will be used for the exporters"""

    def store(self, file: IO[bytes]) -> Deferred[None] | None:
        """Store the given file stream"""


class BlockingFeedStorage(ABC):
    def open(self, spider: Spider) -> IO[bytes]:
        path = spider.crawler.settings["FEED_TEMPDIR"]
        if path and not Path(path).is_dir():
            raise OSError("Not a Directory: " + str(path))

        return NamedTemporaryFile(prefix="feed-", dir=path)

    def store(self, file: IO[bytes]) -> Deferred[None]:
        return deferred_from_coro(run_in_thread(self._store_in_thread, file))

    @abstractmethod
    def _store_in_thread(self, file: IO[bytes]) -> None:
        raise NotImplementedError


class StdoutFeedStorage:
    def __init__(
        self,
        uri: str,
        _stdout: IO[bytes] | None = None,
        *,
        feed_options: dict[str, Any] | None = None,
    ):
        if not _stdout:
            _stdout = sys.stdout.buffer
        self._stdout: IO[bytes] = _stdout
        if feed_options and feed_options.get("overwrite", False) is True:
            logger.warning(
                "Standard output (stdout) storage does not support "
                "overwriting. To suppress this warning, remove the "
                "overwrite option from your FEEDS setting, or set "
                "it to False."
            )

    def open(self, spider: Spider) -> IO[bytes]:
        return self._stdout

    def store(self, file: IO[bytes]) -> Deferred[None] | None:
        pass


class FileFeedStorage:
    def __init__(self, uri: str, *, feed_options: dict[str, Any] | None = None):
        self.path: str = file_uri_to_path(uri) if uri.startswith("file:") else uri
        feed_options = feed_options or {}
        self.write_mode: OpenBinaryMode = (
            "wb" if feed_options.get("overwrite", False) else "ab"
        )

    def open(self, spider: Spider) -> IO[bytes]:
        dirname = Path(self.path).parent
        if dirname and not dirname.exists():
            dirname.mkdir(parents=True)
        return Path(self.path).open(self.write_mode)

    def store(self, file: IO[bytes]) -> Deferred[None] | None:
        file.close()
        return None


def _jsonable(value: Any) -> Any:
    """Return a JSON-serializable representation of *value* that is stable
    across processes, so that it can be hashed to compare feed options."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, type) or callable(value):
        module = getattr(value, "__module__", None) or type(value).__module__
        qualname = getattr(value, "__qualname__", None) or type(value).__qualname__
        return f"{module}.{qualname}"
    return str(value)


def _config_fingerprint(feed_options: dict[str, Any]) -> str:
    """Return a hash of the feed options that determine the output files."""
    options = {k: v for k, v in feed_options.items() if k != "manifest"}
    serialized = json.dumps(_jsonable(options), sort_keys=True)
    return hashlib.sha256(serialized.encode()).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _FeedManifest:
    """Commit log of the finished batch files of a single feed.

    A batch is committed only after its file has been fully exported,
    post-processed and stored, so consumers that only read files listed in
    the manifest never see a partial batch. Every commit rewrites the
    manifest atomically.
    """

    def __init__(
        self, path: str | Path, uri_template: str, feed_options: dict[str, Any]
    ):
        self.path: Path = Path(path)
        self.uri_template: str = uri_template
        self.config_sha256: str = _config_fingerprint(feed_options)
        self.batches: list[dict[str, Any]] = []
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ValueError(f"Cannot parse feed manifest {self.path}: {e}") from e
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError(f"Unsupported feed manifest format: {self.path}")
        batches = data.get("batches")
        if not isinstance(batches, list):
            raise ValueError(f"Unsupported feed manifest format: {self.path}")
        if data.get("config_sha256") != self.config_sha256:
            raise ValueError(
                f"Feed manifest {self.path} was written with different feed "
                f"options for {self.uri_template!r}; refusing to resume"
            )
        for entry in batches:
            batch_id = entry["batch_id"]
            file = Path(entry["file"])
            if not file.is_file():
                raise ValueError(
                    f"Feed manifest {self.path}: file {file} of batch "
                    f"{batch_id} is missing"
                )
            if file.stat().st_size != entry["bytes"]:
                raise ValueError(
                    f"Feed manifest {self.path}: file {file} of batch "
                    f"{batch_id} has a different size than recorded"
                )
            if _file_sha256(file) != entry["sha256"]:
                raise ValueError(
                    f"Feed manifest {self.path}: file {file} of batch "
                    f"{batch_id} does not match its recorded SHA-256"
                )
        self.batches = batches

    @property
    def next_batch_id(self) -> int:
        """Lowest batch ID greater than every committed one."""
        return max((entry["batch_id"] for entry in self.batches), default=0) + 1

    @property
    def committed_files(self) -> set[str]:
        return {entry["file"] for entry in self.batches}

    def commit(self, storage: FileFeedStorage, batch_id: int, itemcount: int) -> None:
        """Record the stored file of batch *batch_id* in the manifest."""
        file = Path(storage.path).absolute()
        self.batches.append(
            {
                "batch_id": batch_id,
                "file": str(file),
                "itemcount": itemcount,
                "bytes": file.stat().st_size,
                "sha256": _file_sha256(file),
            }
        )
        self._write()

    def _write(self) -> None:
        if not self.path.parent.is_dir():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "feed": self.uri_template,
            "config_sha256": self.config_sha256,
            "batches": sorted(self.batches, key=lambda entry: entry["batch_id"]),
        }
        # Write to a sibling file and rename it, so that readers either see
        # the whole previous manifest or the whole new one.
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


class S3FeedStorage(BlockingFeedStorage):
    def __init__(
        self,
        uri: str,
        access_key: str | None = None,
        secret_key: str | None = None,
        acl: str | None = None,
        endpoint_url: str | None = None,
        *,
        feed_options: dict[str, Any] | None = None,
        session_token: str | None = None,
        region_name: str | None = None,
        max_pool_connections: int | None = None,
    ):
        try:
            import boto3.session  # noqa: PLC0415
        except ImportError:
            raise NotConfigured("missing boto3 library") from None
        from botocore.config import Config  # noqa: PLC0415

        u = urlparse(uri)
        assert u.hostname
        self.bucketname: str = u.hostname
        self.access_key: str | None = u.username or access_key
        self.secret_key: str | None = u.password or secret_key
        self.session_token: str | None = session_token
        self.keyname: str = u.path[1:]  # remove first "/"
        self.acl: str | None = acl
        self.endpoint_url: str | None = endpoint_url
        self.region_name: str | None = region_name
        self.max_pool_connections: int | None = max_pool_connections

        boto3_session = boto3.session.Session()
        self.s3_client = boto3_session.client(
            "s3",
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            aws_session_token=self.session_token,
            endpoint_url=self.endpoint_url,
            region_name=self.region_name,
            config=(
                Config(max_pool_connections=self.max_pool_connections)
                if self.max_pool_connections is not None
                else None
            ),
        )

        if feed_options and feed_options.get("overwrite", True) is False:
            logger.warning(
                "S3 does not support appending to files. To "
                "suppress this warning, remove the overwrite "
                "option from your FEEDS setting or set it to True."
            )

    @classmethod
    def from_crawler(
        cls,
        crawler: Crawler,
        uri: str,
        *,
        feed_options: dict[str, Any] | None = None,
    ) -> Self:
        return cls(
            uri,
            access_key=crawler.settings["AWS_ACCESS_KEY_ID"],
            secret_key=crawler.settings["AWS_SECRET_ACCESS_KEY"],
            session_token=crawler.settings["AWS_SESSION_TOKEN"],
            acl=crawler.settings["FEED_STORAGE_S3_ACL"] or None,
            endpoint_url=crawler.settings["AWS_ENDPOINT_URL"] or None,
            region_name=crawler.settings["AWS_REGION_NAME"] or None,
            max_pool_connections=_get_max_pool_connections(crawler.settings),
            feed_options=feed_options,
        )

    def _store_in_thread(self, file: IO[bytes]) -> None:
        file.seek(0)
        try:
            if self.acl:
                self.s3_client.upload_fileobj(
                    Bucket=self.bucketname,
                    Key=self.keyname,
                    Fileobj=file,
                    ExtraArgs={"ACL": self.acl},
                )
            else:
                self.s3_client.upload_fileobj(
                    Bucket=self.bucketname,
                    Key=self.keyname,
                    Fileobj=file,
                )
        finally:
            file.close()


class GCSFeedStorage(BlockingFeedStorage):
    def __init__(
        self,
        uri: str,
        project_id: str | None,
        acl: str | None,
        *,
        feed_options: dict[str, Any] | None = None,
    ):
        self.project_id: str | None = project_id
        self.acl: str | None = acl
        u = urlparse(uri)
        assert u.hostname
        self.bucket_name: str = u.hostname
        self.blob_name: str = u.path[1:]  # remove first "/"
        self.overwrite: bool = not feed_options or feed_options.get("overwrite", True)

    @classmethod
    def from_crawler(
        cls,
        crawler: Crawler,
        uri: str,
        *,
        feed_options: dict[str, Any] | None = None,
    ) -> Self:
        return cls(
            uri,
            crawler.settings["GCS_PROJECT_ID"],
            crawler.settings["FEED_STORAGE_GCS_ACL"] or None,
            feed_options=feed_options,
        )

    def _store_in_thread(self, file: IO[bytes]) -> None:
        file.seek(0)
        try:
            from google.cloud.storage import Client  # noqa: PLC0415

            client = Client(project=self.project_id)
            bucket = client.bucket(self.bucket_name)
            blob = None if self.overwrite else bucket.get_blob(self.blob_name)
            if blob is None:
                bucket.blob(self.blob_name).upload_from_file(
                    file, predefined_acl=self.acl
                )
                return
            # Appending uploads the new data as a separate object and composes
            # it with the existing one, leaving the data already stored
            # untouched. Composition resolves its sources by name, so it always
            # appends to the latest version of the blob.
            part = bucket.blob(f"{self.blob_name}.{uuid4().hex}.part")
            part.upload_from_file(file, predefined_acl=self.acl)
            try:
                blob.compose([blob, part], client=client)
            finally:
                part.delete(client=client)
            if self.acl:
                blob.acl.save_predefined(self.acl, client=client)
        finally:
            file.close()


class FTPFeedStorage(BlockingFeedStorage):
    def __init__(
        self,
        uri: str,
        use_active_mode: bool = False,
        *,
        feed_options: dict[str, Any] | None = None,
    ):
        u = urlparse(uri)
        if not u.hostname:
            raise ValueError(f"Got a storage URI without a hostname: {uri}")
        self.host: str = u.hostname
        self.port: int = int(u.port or "21")
        self.username: str = u.username or ""
        self.password: str = unquote(u.password or "")
        self.path: str = u.path
        self.tls: bool = u.scheme == "ftps"
        self.use_active_mode: bool = use_active_mode
        self.overwrite: bool = not feed_options or feed_options.get("overwrite", True)

    @classmethod
    def from_crawler(
        cls,
        crawler: Crawler,
        uri: str,
        *,
        feed_options: dict[str, Any] | None = None,
    ) -> Self:
        return cls(
            uri,
            use_active_mode=crawler.settings.getbool("FEED_STORAGE_FTP_ACTIVE"),
            feed_options=feed_options,
        )

    def _store_in_thread(self, file: IO[bytes]) -> None:
        ftp_store_file(
            path=self.path,
            file=file,
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            use_active_mode=self.use_active_mode,
            overwrite=self.overwrite,
            tls=self.tls,
        )


class FeedSlot:
    def __init__(
        self,
        storage: FeedStorageProtocol,
        uri: str,
        format: str,  # noqa: A002
        store_empty: bool,
        batch_id: int,
        uri_template: str,
        filter: ItemFilter,  # noqa: A002
        feed_options: dict[str, Any],
        spider: Spider,
        exporters: dict[str, type[BaseItemExporter]],
        settings: BaseSettings,
        crawler: Crawler,
    ):
        self.file: IO[bytes] | None = None
        self.exporter: BaseItemExporter | None = None
        self.storage: FeedStorageProtocol = storage
        # feed params
        self.batch_id: int = batch_id
        self.format: str = format
        self.store_empty: bool = store_empty
        self.uri_template: str = uri_template
        self.uri: str = uri
        self.filter: ItemFilter = filter
        # exporter params
        self.feed_options: dict[str, Any] = feed_options
        self.spider: Spider = spider
        self.exporters: dict[str, type[BaseItemExporter]] = exporters
        self.settings: BaseSettings = settings
        self.crawler: Crawler = crawler
        # flags
        self.itemcount: int = 0
        self._exporting: bool = False
        self._fileloaded: bool = False

    def start_exporting(self) -> None:
        if not self._fileloaded:
            self.file = self.storage.open(self.spider)
            if "postprocessing" in self.feed_options:
                self.file = cast(
                    "IO[bytes]",
                    PostProcessingManager(
                        self.feed_options["postprocessing"],
                        self.file,
                        self.feed_options,
                    ),
                )
            self.exporter = self._get_exporter(
                file=self.file,
                format_=self.feed_options["format"],
                fields_to_export=self.feed_options["fields"],
                encoding=self.feed_options["encoding"],
                indent=self.feed_options["indent"],
                **self.feed_options["item_export_kwargs"],
            )
            self._fileloaded = True

        if not self._exporting:
            assert self.exporter
            self.exporter.start_exporting()
            self._exporting = True

    def _get_exporter(
        self, file: IO[bytes], format_: str, *args: Any, **kwargs: Any
    ) -> BaseItemExporter:
        return build_from_crawler(
            self.exporters[format_], self.crawler, file, *args, **kwargs
        )

    def finish_exporting(self) -> None:
        if self._exporting:  # pragma: no branch
            assert self.exporter
            self.exporter.finish_exporting()
            self._exporting = False


class FeedExporter:
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        exporter = cls(crawler)
        crawler.signals.connect(exporter.open_spider, signals.spider_opened)
        crawler.signals.connect(exporter.close_spider, signals.spider_closed)
        crawler.signals.connect(exporter.item_scraped, signals.item_scraped)
        return exporter

    def __init__(self, crawler: Crawler):
        self.crawler: Crawler = crawler
        self.settings: Settings = crawler.settings
        self.feeds: dict[str, dict[str, Any]] = {}
        self.slots: list[FeedSlot] = []
        self.filters: dict[str, ItemFilter] = {}
        self.manifests: dict[str, _FeedManifest] = {}
        self._pending_close_tasks: list[asyncio.Task[None] | Deferred[None]] = []

        if not self.settings["FEEDS"] and not self.settings["FEED_URI"]:
            raise NotConfigured

        # Begin: Backward compatibility for FEED_URI and FEED_FORMAT settings
        if self.settings["FEED_URI"]:
            warnings.warn(
                "The `FEED_URI` and `FEED_FORMAT` settings have been deprecated in favor of "
                "the `FEEDS` setting. Please see the `FEEDS` setting docs for more details",
                category=ScrapyDeprecationWarning,
                stacklevel=2,
            )
            uri = self.settings["FEED_URI"]
            # handle pathlib.Path objects
            uri = str(uri.absolute()) if isinstance(uri, Path) else str(uri)
            feed_options = {"format": self.settings["FEED_FORMAT"]}
            self.feeds[uri] = feed_complete_default_values_from_settings(
                feed_options, self.settings, uri
            )
            self.filters[uri] = self._load_filter(feed_options)
        # End: Backward compatibility for FEED_URI and FEED_FORMAT settings

        # 'FEEDS' setting takes precedence over 'FEED_URI'
        for settings_uri, feed_options in self.settings.getdict("FEEDS").items():
            # handle pathlib.Path objects
            uri = (
                str(settings_uri.absolute())
                if isinstance(settings_uri, Path)
                else str(settings_uri)
            )
            self.feeds[uri] = feed_complete_default_values_from_settings(
                feed_options, self.settings, uri
            )
            self.filters[uri] = self._load_filter(feed_options)

        self.storages: dict[str, type[FeedStorageProtocol]] = self._load_components(
            "FEED_STORAGES"
        )
        self.exporters: dict[str, type[BaseItemExporter]] = self._load_components(
            "FEED_EXPORTERS"
        )
        for uri, feed_options in self.feeds.items():
            if not self._storage_supported(uri, feed_options):
                raise NotConfigured
            if not self._settings_are_valid():
                raise NotConfigured
            if not self._exporter_supported(feed_options["format"]):
                raise NotConfigured
        self._load_manifests()

    def _load_manifests(self) -> None:
        """Set up the commit manifest of every feed that declares one.

        Loading an existing manifest validates the committed files and the
        feed options they were written with, and raises if they do not
        match, so that a crawl never starts on top of inconsistent output.
        """
        manifest_paths: set[Path] = set()
        for uri, feed_options in self.feeds.items():
            manifest_path = feed_options.get("manifest")
            if not manifest_path:
                continue
            storage_cls = self.storages.get(
                urlparse(uri).scheme, self.storages["file"]
            )
            if not (
                isinstance(storage_cls, type)
                and issubclass(storage_cls, FileFeedStorage)
            ):
                raise NotConfigured(
                    f"The 'manifest' feed option is only supported with "
                    f"local file storage, got feed URI {uri!r}"
                )
            path = Path(manifest_path)
            if path in manifest_paths:
                raise NotConfigured(
                    f"Feeds cannot share a manifest file: {manifest_path}"
                )
            manifest_paths.add(path)
            self.manifests[uri] = _FeedManifest(path, uri, feed_options)

    def open_spider(self, spider: Spider) -> None:
        for uri, feed_options in self.feeds.items():
            uri_params = self._get_uri_params(spider, feed_options["uri_params"])
            batch_id = 1
            manifest = self.manifests.get(uri)
            if manifest is not None:
                batch_id = manifest.next_batch_id
                uri_params["batch_id"] = batch_id
                if manifest.batches:
                    logger.info(
                        f"Resuming feed {uri} at batch {batch_id}: "
                        f"{len(manifest.batches)} batches already committed "
                        f"in {manifest.path}"
                    )
            self.slots.append(
                self._start_new_batch(
                    batch_id=batch_id,
                    uri=apply_uri_params(uri, uri_params),
                    feed_options=feed_options,
                    spider=spider,
                    uri_template=uri,
                )
            )

    async def close_spider(self, spider: Spider) -> None:
        for slot in self.slots:
            self._schedule_slot_close(slot, spider)

        if self._pending_close_tasks:  # pragma: no branch
            if is_asyncio_available():
                await asyncio.wait(
                    cast("list[asyncio.Task[None]]", list(self._pending_close_tasks))
                )
            else:
                await DeferredList(
                    cast("list[Deferred[None]]", list(self._pending_close_tasks))
                )

        # Send FEED_EXPORTER_CLOSED signal
        await self.crawler.signals.send_catch_log_async(signals.feed_exporter_closed)

    def _schedule_slot_close(
        self, slot: FeedSlot, spider: Spider
    ) -> asyncio.Task[None] | Deferred[None]:
        """Start closing the slot without waiting for it to finish, keeping
        track of the pending work so that it can be awaited in
        :meth:`close_spider` if it hasn't finished by then."""
        aw: asyncio.Task[None] | Deferred[None]
        coro = self._close_slot(slot, spider)
        if is_asyncio_available():
            aw = asyncio.create_task(coro)
            self._pending_close_tasks.append(aw)
            aw.add_done_callback(self._pending_close_tasks.remove)
        else:
            aw = deferred_from_coro(coro)
            self._pending_close_tasks.append(aw)
            aw.addBoth(self._untrack_pending_close_task, aw)
        return aw

    def _untrack_pending_close_task(self, result: Any, aw: Deferred[None]) -> Any:
        self._pending_close_tasks.remove(aw)
        return result

    @staticmethod
    def _get_file(slot_: FeedSlot) -> IO[bytes]:
        assert slot_.file
        if isinstance(slot_.file, PostProcessingManager):
            slot_.file.close()
            return slot_.file.file
        return slot_.file

    async def _close_slot(self, slot: FeedSlot, spider: Spider) -> None:

        if slot.itemcount:
            # Normal case
            slot.finish_exporting()
        elif slot.store_empty and slot.batch_id == 1:
            # Need to store the empty file
            slot.start_exporting()
            slot.finish_exporting()
        else:
            # In this case, the file is not stored, so no processing is required.
            return

        logmsg = f"{slot.format} feed ({slot.itemcount} items) in: {slot.uri}"
        slot_type = type(slot.storage).__name__
        try:
            await ensure_awaitable(slot.storage.store(self._get_file(slot)))
        except Exception:
            logger.exception(
                "Error storing %s",
                logmsg,
                extra={"spider": spider},
            )
            self.crawler.stats.inc_value(f"feedexport/failed_count/{slot_type}")
        else:
            logger.info("Stored %s", logmsg, extra={"spider": spider})
            self.crawler.stats.inc_value(f"feedexport/success_count/{slot_type}")
            manifest = self.manifests.get(slot.uri_template)
            if manifest is not None:
                assert isinstance(slot.storage, FileFeedStorage)
                try:
                    manifest.commit(slot.storage, slot.batch_id, slot.itemcount)
                except Exception:
                    logger.exception(
                        "Error committing %s to the feed manifest %s",
                        logmsg,
                        manifest.path,
                        extra={"spider": spider},
                    )

        await self.crawler.signals.send_catch_log_async(
            signals.feed_slot_closed, slot=slot
        )

    def _start_new_batch(
        self,
        batch_id: int,
        uri: str,
        feed_options: dict[str, Any],
        spider: Spider,
        uri_template: str,
    ) -> FeedSlot:
        """
        Redirect the output data stream to a new file.
        Execute multiple times if FEED_EXPORT_BATCH_ITEM_COUNT setting or FEEDS.batch_item_count is specified
        :param batch_id: sequence number of current batch
        :param uri: uri of the new batch to start
        :param feed_options: dict with parameters of feed
        :param spider: user spider
        :param uri_template: template of uri which contains %(batch_time)s or %(batch_id)d to create new uri
        """
        manifest = self.manifests.get(uri_template)
        if manifest is not None:
            # Files of batches missing from the manifest are leftovers of an
            # interrupted run, so the new batch must start from an empty
            # file instead of appending to one.
            storage = self._get_storage(uri, {**feed_options, "overwrite": True})
            assert isinstance(storage, FileFeedStorage)
            file = str(Path(storage.path).absolute())
            if file in manifest.committed_files:
                raise CloseSpider(
                    f"batch {batch_id} of feed {uri_template!r} would "
                    f"overwrite {file}, which is already committed in the "
                    f"feed manifest {manifest.path}"
                )
        else:
            storage = self._get_storage(uri, feed_options)
        return FeedSlot(
            storage=storage,
            uri=uri,
            format=feed_options["format"],
            store_empty=feed_options["store_empty"],
            batch_id=batch_id,
            uri_template=uri_template,
            filter=self.filters[uri_template],
            feed_options=feed_options,
            spider=spider,
            exporters=self.exporters,
            settings=self.settings,
            crawler=self.crawler,
        )

    def item_scraped(self, item: Any, spider: Spider) -> None:
        slots = []
        for slot in self.slots:
            if not slot.filter.accepts(item):
                slots.append(
                    slot
                )  # if slot doesn't accept item, continue with next slot
                continue

            slot.start_exporting()
            assert slot.exporter
            slot.exporter.export_item(item)
            slot.itemcount += 1
            # create new slot for each slot with itemcount == FEED_EXPORT_BATCH_ITEM_COUNT and close the old one
            if (
                self.feeds[slot.uri_template]["batch_item_count"]
                and slot.itemcount >= self.feeds[slot.uri_template]["batch_item_count"]
            ):
                uri_params = self._get_uri_params(
                    spider, self.feeds[slot.uri_template]["uri_params"], slot
                )
                self._schedule_slot_close(slot, spider)
                slots.append(
                    self._start_new_batch(
                        batch_id=slot.batch_id + 1,
                        uri=apply_uri_params(slot.uri_template, uri_params),
                        feed_options=self.feeds[slot.uri_template],
                        spider=spider,
                        uri_template=slot.uri_template,
                    )
                )
            else:
                slots.append(slot)
        self.slots = slots

    def _load_components(self, setting_prefix: str) -> dict[str, Any]:
        conf = without_none_values(
            cast("dict[str, str]", self.settings.getwithbase(setting_prefix))
        )
        d = {}
        for k, v in conf.items():
            with contextlib.suppress(NotConfigured):
                d[k] = load_object(v)
        return d

    def _exporter_supported(self, format_: str) -> bool:
        if format_ in self.exporters:
            return True
        if format_:
            logger.error("Unknown feed format: %(format)s", {"format": format_})
        else:
            logger.error(
                "Feed format not set and it could not be inferred from the "
                "feed URI; set it explicitly with the 'format' key"
            )
        return False

    def _settings_are_valid(self) -> bool:
        """
        If FEED_EXPORT_BATCH_ITEM_COUNT setting or FEEDS.batch_item_count is specified uri has to contain
        %(batch_time)s or %(batch_id)d to distinguish different files of partial output
        """
        for uri_template, values in self.feeds.items():
            if values["batch_item_count"] and not re.search(
                r"%\(batch_time\)s|%\(batch_id\)", uri_template
            ):
                logger.error(
                    "%%(batch_time)s or %%(batch_id)d must be in the feed URI (%s) if FEED_EXPORT_BATCH_ITEM_COUNT "
                    "setting or FEEDS.batch_item_count is specified and greater than 0. For more info see: "
                    "https://docs.scrapy.org/en/latest/topics/feed-exports.html#feed-export-batch-item-count",
                    uri_template,
                )
                return False
        return True

    def _storage_supported(self, uri: str, feed_options: dict[str, Any]) -> bool:
        scheme = urlparse(uri).scheme
        if scheme in self.storages or PureWindowsPath(uri).drive:
            try:
                self._get_storage(uri, feed_options)
                return True
            except NotConfigured as e:
                logger.error(
                    "Disabled feed storage scheme: %(scheme)s. Reason: %(reason)s",
                    {"scheme": scheme, "reason": str(e)},
                )
        else:
            logger.error("Unknown feed storage scheme: %(scheme)s", {"scheme": scheme})
        return False

    def _get_storage(
        self, uri: str, feed_options: dict[str, Any]
    ) -> FeedStorageProtocol:
        """Build a storage object for the specified *uri* with the specified
        *feed_options*."""
        cls = self.storages.get(urlparse(uri).scheme, self.storages["file"])
        return build_from_crawler(cls, self.crawler, uri, feed_options=feed_options)

    def _get_uri_params(
        self,
        spider: Spider,
        uri_params_function: str | UriParamsCallableT | None,
        slot: FeedSlot | None = None,
    ) -> dict[str, Any]:
        params = {k: getattr(spider, k) for k in dir(spider)}
        utc_now = datetime.now(tz=timezone.utc)
        params["time"] = utc_now.replace(microsecond=0).isoformat().replace(":", "-")
        params["batch_time"] = utc_now.isoformat().replace(":", "-")
        params["batch_id"] = slot.batch_id + 1 if slot is not None else 1
        uripar_function: UriParamsCallableT = (
            load_object(uri_params_function)
            if uri_params_function
            else lambda params, _: params
        )
        new_params = uripar_function(params, spider)
        return new_params if new_params is not None else params

    def _load_filter(self, feed_options: dict[str, Any]) -> ItemFilter:
        # load the item filter if declared else load the default filter class
        item_filter_class: type[ItemFilter] = load_object(
            feed_options.get("item_filter", ItemFilter)
        )
        return item_filter_class(feed_options)


def __getattr__(name: str) -> Any:  # pragma: no cover
    if name == "IFeedStorage":
        warnings.warn(
            "scrapy.extensions.feedexport.IFeedStorage is deprecated.",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return _IFeedStorage
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
