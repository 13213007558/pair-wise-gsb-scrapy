from __future__ import annotations

import re
import sys
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote
from weakref import WeakKeyDictionary

from twisted.python.failure import Failure

if TYPE_CHECKING:
    from types import TracebackType

    from scrapy.crawler import Crawler
    from scrapy.http import Headers, Request, Response


_REDACTED = "[redacted]"
_REDACTED_USERINFO = "redacted"

# Headers whose values are hidden from log output by default. Applications add
# their own names through LOG_SENSITIVE_HEADERS.
_DEFAULT_SENSITIVE_HEADERS: tuple[str, ...] = (
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "x-auth-token",
    "x-amz-security-token",
)

# Query string keys whose values are hidden from log output by default.
# Applications add their own names through LOG_SENSITIVE_QUERY_PARAMS.
_DEFAULT_SENSITIVE_QUERY_PARAMS: tuple[str, ...] = (
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "key",
    "password",
    "secret",
    "signature",
    "token",
)

_URL_RE = re.compile(r"(?:data|https?|ftp):[^\s<>\"']+", re.IGNORECASE)
_URL_USERINFO_RE = re.compile(r"//([^/@\s]+)@")
_QUERY_PAIR_RE = re.compile(
    r"(?P<prefix>(?:[?&;]|\s)(?P<key>[A-Za-z0-9_%.-]+)=)(?P<value>[^\s&;,.<>\"']+)",
    re.IGNORECASE,
)

_PROJECTED_EXCEPTION_CLASSES: dict[type[BaseException], type[BaseException]] = {}
_PROJECTED_EXCEPTION_CLASSES_LOCK = threading.Lock()

_projections: WeakKeyDictionary[
    Crawler, tuple[tuple[str, ...], tuple[str, ...], _LogProjection]
] = WeakKeyDictionary()


def _create_projected_exception_class(
    exception_class: type[BaseException],
) -> type[BaseException]:
    """Return a subclass of *exception_class* whose text is projected.

    Python formats tracebacks from the exception type and its ``__str__``.
    Subclassing keeps the exception class name in the traceback while allowing
    the text to be projected, without changing the original exception object.
    """

    def projected_str(self: Any) -> str:
        original = self.__dict__.get("_log_original")
        projection = self.__dict__.get("_log_projection")
        if original is None or projection is None:  # pragma: no cover
            return exception_class.__name__
        try:
            text = str(original)
        except Exception:
            # Never fall back to repr(), which can expose the exception
            # arguments, and never use Twisted's traceback fallback either.
            return exception_class.__name__
        return projection.project_text(text)

    def projected_repr(self: Any) -> str:
        return projected_str(self)

    with _PROJECTED_EXCEPTION_CLASSES_LOCK:
        projected_class = _PROJECTED_EXCEPTION_CLASSES.get(exception_class)
        if projected_class is None:
            projected_class = type(
                exception_class.__name__,
                (exception_class,),
                {
                    "__module__": exception_class.__module__,
                    "__qualname__": exception_class.__qualname__,
                    "__str__": projected_str,
                    "__repr__": projected_repr,
                },
            )
            _PROJECTED_EXCEPTION_CLASSES[exception_class] = projected_class
    return projected_class


def _new_projected_instance(
    exception_class: type[BaseException],
    projected_class: type[BaseException],
    projection: _LogProjection,
    original: BaseException | None = None,
) -> BaseException:
    """Create an instance of *projected_class* using the original type's path.

    Some exception types, such as :exc:`OSError` and :exc:`ExceptionGroup`,
    define ``__new__`` and reject ``BaseException.__new__``. Their own
    ``__new__`` must be used to preserve the exception type name in tracebacks.
    """
    if issubclass(exception_class, BaseExceptionGroup):
        exceptions = (
            [projection.project_exception(child) for child in original.exceptions]
            if original is not None
            else []
        )
        message = original.message if original is not None else ""
        projected = exception_class.__new__(projected_class, message, exceptions)
    else:
        args = original.args if original is not None else ("",)
        projected = exception_class.__new__(projected_class, *args)
    return projected


def _projected_exception_class(
    exception_class: type[BaseException],
) -> type[BaseException] | None:
    try:
        return _PROJECTED_EXCEPTION_CLASSES[exception_class]
    except KeyError:
        pass
    try:
        projected_class = _create_projected_exception_class(exception_class)
        _probe_projected_class(exception_class, projected_class)
    except (TypeError, ValueError):  # pragma: no cover
        return None
    return projected_class


def _probe_projected_class(
    exception_class: type[BaseException], projected_class: type[BaseException]
) -> None:
    """Create a throwaway instance to check how *projected_class* must be built."""
    if issubclass(exception_class, BaseExceptionGroup):
        exception_class.__new__(projected_class, "", [ValueError()])
    else:
        exception_class.__new__(projected_class, "")


def _projected_header_names(
    names: frozenset[str],
) -> tuple[re.Pattern[str] | None, re.Pattern[str] | None, re.Pattern[str] | None]:
    if not names:
        return None, None, None
    alternatives = "|".join(re.escape(name) for name in sorted(names))
    quoted = re.compile(
        rf"(?i)(?:b?['\"](?:{alternatives})['\"])\s*:\s*"
        rf"(?:b?)(?P<quote>['\"])(?P<value>[^'\"]*)(?P=quote)",
        re.DOTALL,
    )
    line = re.compile(
        rf"(?im)(?P<prefix>(?:^|[\s'\"(])(?:{alternatives})\s*:\s*)"
        rf"(?P<value>[^\r\n]*)"
    )
    pair = re.compile(
        rf"(?i)(?P<prefix>\b(?:{alternatives})\s*=\s*)"
        rf"(?P<value>[^\s&;,<>\"]+)"
    )
    return quoted, line, pair


class _ProjectedHeaders:
    """A read-only view of headers with sensitive values replaced."""

    __slots__ = ("_headers", "_projection")

    def __init__(self, headers: Headers, projection: _LogProjection):
        self._headers = headers
        self._projection = projection

    def _project_value(self, key: str | bytes, value: bytes | None) -> bytes | None:
        if value is not None and self._projection.is_sensitive_header(key):
            return _REDACTED.encode()
        return value

    def __getitem__(self, key: str | bytes) -> bytes | None:
        return self._project_value(key, self._headers[key])

    def get(self, key: str | bytes, default: Any = None) -> bytes | None:
        if key not in self._headers:
            return default
        return self._project_value(key, self._headers.get(key))

    def getlist(self, key: str | bytes) -> list[bytes]:
        if key not in self._headers:
            return []
        if self._projection.is_sensitive_header(key):
            return [_REDACTED.encode()]
        return self._headers.getlist(key)

    def __contains__(self, key: str | bytes) -> bool:
        return key in self._headers

    def __iter__(self):
        return iter(self._headers)

    def __len__(self) -> int:
        return len(self._headers)

    def __bool__(self) -> bool:
        return bool(self._headers)

    def items(self):
        return [
            (
                key,
                [
                    _REDACTED.encode()
                    if self._projection.is_sensitive_header(key)
                    else value
                    for value in values
                ],
            )
            for key, values in self._headers.items()
        ]

    def values(self) -> list[bytes | None]:
        return [self[key] for key in self._headers]

    def keys(self):
        return self._headers.keys()

    def to_string(self) -> bytes:
        from scrapy.http import Headers

        return Headers(self.items(), encoding=self._headers.encoding).to_string()

    def to_tuple_list(self) -> list[tuple[str, str]]:
        return [
            (
                key.decode(self._headers.encoding, errors="replace"),
                value.decode(self._headers.encoding, errors="replace"),
            )
            for key, values in self.items()
            for value in values
        ]

    def __str__(self) -> str:
        return "\n".join(
            f"{key.decode(self._headers.encoding, errors='replace')}: "
            f"{value.decode(self._headers.encoding, errors='replace')}"
            for key, values in self.items()
            for value in values
        )

    def __repr__(self) -> str:
        return str(self)


class _ProjectedRequest:
    """A read-only view of a request for log output.

    Attributes other than ``url`` and ``headers`` are read through to the
    original request. The original request is never modified.
    """

    __slots__ = ("_projection", "_request")

    def __init__(self, request: Request, projection: _LogProjection):
        self._request = request
        self._projection = projection

    @property
    def url(self) -> str:
        return self._projection.project_url(self._request.url)

    @property
    def headers(self) -> _ProjectedHeaders:
        return _ProjectedHeaders(self._request.headers, self._projection)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._request, name)

    def __str__(self) -> str:
        return f"<{self._request.method} {self.url}>"

    def __repr__(self) -> str:
        return self.__str__()


class _ProjectedResponse:
    """A read-only view of a response for log output."""

    __slots__ = ("_projection", "_response")

    def __init__(self, response: Response, projection: _LogProjection):
        self._response = response
        self._projection = projection

    @property
    def url(self) -> str:
        return self._projection.project_url(self._response.url)

    @property
    def headers(self) -> _ProjectedHeaders:
        return _ProjectedHeaders(self._response.headers, self._projection)

    @property
    def request(self) -> _ProjectedRequest | None:
        request = self._response.request
        if request is None:
            return None
        return self._projection.project_request(request)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

    def __str__(self) -> str:
        return f"<{self._response.status} {self.url}>"

    def __repr__(self) -> str:
        return self.__str__()


class _FallbackProjectedException:
    """Text projection for exception types that reject subclasses.

    A few third-party exception types prevent subclass creation, so the
    dynamically created subclass used for other exceptions cannot be built.
    The ``exc_info`` tuple keeps the original type, so a traceback still names
    the exception class, while this object supplies the projected text.
    """

    __slots__ = ("_exception", "_projection")

    def __init__(self, exception: BaseException, projection: _LogProjection):
        self._exception = exception
        self._projection = projection

    def __getattr__(self, name: str) -> Any:
        return getattr(self._exception, name)

    def __str__(self) -> str:
        try:
            text = str(self._exception)
        except Exception:
            return type(self._exception).__name__
        return self._projection.project_text(text)

    def __repr__(self) -> str:
        return self.__str__()


class _LogProjection:
    """Project requests, responses and exception text for log output."""

    def __init__(
        self,
        *,
        sensitive_headers: list[str] | tuple[str, ...] = (),
        sensitive_query_params: list[str] | tuple[str, ...] = (),
    ):
        self._sensitive_headers = frozenset(
            [*_DEFAULT_SENSITIVE_HEADERS, *(name.lower() for name in sensitive_headers)]
        )
        self._sensitive_query_params = frozenset(
            [
                *_DEFAULT_SENSITIVE_QUERY_PARAMS,
                *(name.lower() for name in sensitive_query_params),
            ]
        )
        (
            self._header_quoted_re,
            self._header_line_re,
            self._header_pair_re,
        ) = _projected_header_names(self._sensitive_headers)

    @classmethod
    def from_crawler(cls, crawler: Crawler | None) -> _LogProjection:
        if crawler is None:
            return _get_default_projection()
        return _get_log_projection(crawler)

    def is_sensitive_header(self, name: str | bytes) -> bool:
        if isinstance(name, bytes):
            name = name.decode(errors="replace")
        return name.lower() in self._sensitive_headers

    def is_sensitive_query_param(self, name: str) -> bool:
        return name.strip().lower() in self._sensitive_query_params

    def project_url(self, url: str) -> str:
        from urllib.parse import urlsplit, urlunsplit

        try:
            parts = urlsplit(url)
        except ValueError:
            return self._project_url_fallback(url)
        netloc = parts.netloc
        if "@" in netloc:
            userinfo, separator, host = netloc.rpartition("@")
            if separator and userinfo and userinfo != _REDACTED_USERINFO:
                netloc = f"{_REDACTED_USERINFO}@{host}"
        query = self._project_query(parts.query)
        fragment = self._project_query(parts.fragment)
        if (
            netloc == parts.netloc
            and query == parts.query
            and fragment == parts.fragment
        ):
            return url
        return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))

    def _project_url_fallback(self, url: str) -> str:
        """Project a URL that cannot be parsed, such as one with an invalid
        bracketed host."""
        head, has_query, query_and_fragment = url.partition("?")
        head = _URL_USERINFO_RE.sub(f"//{_REDACTED_USERINFO}@", head)
        if not has_query:
            return head
        query, has_fragment, fragment = query_and_fragment.partition("#")
        projected = self._project_query(query)
        if has_fragment:
            projected = f"{projected}#{self._project_query(fragment)}"
        return f"{head}?{projected}"

    def _project_query(self, query: str) -> str:
        if not query or "=" not in query:
            return query
        projected = []
        for part in query.split("&"):
            key, separator, _value = part.partition("=")
            if separator and self.is_sensitive_query_param(unquote(key)):
                projected.append(f"{key}={_REDACTED}")
            else:
                projected.append(part)
        return "&".join(projected)

    def project_text(self, text: str) -> str:
        if not isinstance(text, str):
            return text

        def replace_url(match: re.Match[str]) -> str:
            return self.project_url(match.group(0))

        text = _URL_RE.sub(replace_url, text)
        if self._header_quoted_re is not None:
            text = self._header_quoted_re.sub(self._replace_quoted_value, text)
        if self._header_line_re is not None:
            text = self._header_line_re.sub(
                lambda match: f"{match.group('prefix')}{_REDACTED}", text
            )
        if self._header_pair_re is not None:
            text = self._header_pair_re.sub(
                lambda match: f"{match.group('prefix')}{_REDACTED}", text
            )
        if "=" in text:
            text = _QUERY_PAIR_RE.sub(self._replace_query_pair, text)
        return text

    @staticmethod
    def _replace_quoted_value(match: re.Match[str]) -> str:
        return (
            match.group(0)[: match.start("value") - match.start(0)]
            + _REDACTED
            + match.group("quote")
        )

    def _replace_query_pair(self, match: re.Match[str]) -> str:
        if not self.is_sensitive_query_param(match.group("key")):
            return match.group(0)
        return f"{match.group('prefix')}{_REDACTED}"

    def raw_error_message(self, failure: Failure) -> str:
        if not isinstance(failure.value, BaseException):
            return ""
        try:
            return str(failure.value)
        except Exception:
            return type(failure.value).__name__

    def project_request(self, request: Request) -> _ProjectedRequest:
        return _ProjectedRequest(request, self)

    def project_response(self, response: Response) -> _ProjectedResponse:
        return _ProjectedResponse(response, self)

    def project_exception(
        self, exception: BaseException | None
    ) -> BaseException | None:
        if exception is None:
            return None
        if getattr(exception, "_log_projection", None) is self:
            return exception
        projected_class = _projected_exception_class(type(exception))
        if projected_class is None:
            return _FallbackProjectedException(exception, self)
        projected = _new_projected_instance(
            type(exception), projected_class, self, exception
        )
        self._copy_exception_state(exception, projected)
        projected._log_original = exception
        projected._log_projection = self
        projected.__traceback__ = exception.__traceback__
        projected.__cause__ = self.project_exception(exception.__cause__)
        projected.__context__ = self.project_exception(exception.__context__)
        projected.__suppress_context__ = exception.__suppress_context__
        notes = getattr(exception, "__notes__", None)
        if notes is not None:
            projected.__notes__ = self._project_notes(notes)
        return projected

    def _copy_exception_state(
        self, exception: BaseException, projected: BaseException
    ) -> None:
        try:
            projected.__dict__.update(exception.__dict__)
        except AttributeError:  # pragma: no cover
            pass
        if isinstance(exception, BaseExceptionGroup):
            # The constructor args hold the projected children; copying the
            # original args back would restore the unprojected children.
            return
        try:
            projected.args = exception.args
        except Exception:  # pragma: no cover
            pass
        if type(exception).__name__ == "SyntaxError":
            self._copy_syntax_error_state(exception, projected)

    def _copy_syntax_error_state(
        self, exception: BaseException, projected: BaseException
    ) -> None:
        for attribute in (
            "filename",
            "lineno",
            "end_lineno",
            "text",
            "offset",
            "end_offset",
        ):
            try:
                value = getattr(exception, attribute)
            except AttributeError:
                continue
            try:
                setattr(projected, attribute, value)
            except AttributeError:  # pragma: no cover
                pass
        try:
            projected.msg = self.project_text(exception.msg)
        except AttributeError:  # pragma: no cover
            pass

    def _project_notes(self, notes: Any) -> list[str]:
        try:
            return [self.project_text(note) for note in notes]
        except TypeError:  # pragma: no cover
            return ["[redacted]"]

    def project_failure(self, failure: Failure) -> _ProjectedFailure:
        return _ProjectedFailure(failure, self)

    def project_value(self, value: Any) -> Any:
        from scrapy.http import Headers, Request, Response

        if isinstance(value, _ProjectedRequest | _ProjectedResponse):
            return value
        if isinstance(value, Request):
            return self.project_request(value)
        if isinstance(value, Response):
            return self.project_response(value)
        if isinstance(value, Failure):
            return self.project_failure(value)
        if isinstance(value, Headers):
            return _ProjectedHeaders(value, self)
        if isinstance(value, BaseException):
            return self.project_exception(value)
        if isinstance(value, str):
            return self.project_text(value)
        return value

    def project_args(
        self, args: Any
    ) -> dict[str, Any] | tuple[Any, ...] | Any:
        if isinstance(args, tuple):
            return tuple(self.project_value(value) for value in args)
        if isinstance(args, dict):
            return {key: self.project_value(value) for key, value in args.items()}
        return args


class _ProjectedFailure:
    """A view of a Twisted Failure with projected error text."""

    __slots__ = ("_failure", "_projection")

    def __init__(self, failure: Failure, projection: _LogProjection):
        self._failure = failure
        self._projection = projection

    def __getattr__(self, name: str) -> Any:
        return getattr(self._failure, name)

    @property
    def type(self) -> type[BaseException] | None:
        value = self.value
        if value is None:
            return self._failure.type
        return type(value)

    @property
    def value(self) -> BaseException | None:
        return self._projection.project_exception(self._failure.value)

    def getErrorMessage(self) -> str:
        return self._projection.project_text(
            self._projection.raw_error_message(self._failure)
        )

    def __str__(self) -> str:
        return self.getErrorMessage()

    def __repr__(self) -> str:
        return self.getErrorMessage()


_default_projection: _LogProjection | None = None


def _get_default_projection() -> _LogProjection:
    global _default_projection  # noqa: PLW0603
    if _default_projection is None:
        _default_projection = _LogProjection()
    return _default_projection


def _get_log_projection(crawler: Crawler) -> _LogProjection:
    """Return the projection configured for *crawler*.

    The projection is rebuilt when the related settings change, so a caller
    that updates the settings before logging still gets the new rules.
    """
    settings = crawler.settings
    headers = tuple(settings.getlist("LOG_SENSITIVE_HEADERS"))
    query_params = tuple(settings.getlist("LOG_SENSITIVE_QUERY_PARAMS"))
    cached = _projections.get(crawler)
    if cached is not None and cached[:2] == (headers, query_params):
        return cached[2]
    projection = _LogProjection(
        sensitive_headers=headers, sensitive_query_params=query_params
    )
    _projections[crawler] = (headers, query_params, projection)
    return projection


def _project_exc_info(
    projection: _LogProjection,
    exc_info: (
        bool
        | BaseException
        | tuple[type[BaseException], BaseException, TracebackType | None]
        | None
    ),
) -> tuple[type[BaseException], BaseException, TracebackType | None] | None:
    """Project an ``exc_info`` value accepted by :meth:`logging.Logger.log`."""
    if exc_info is True or exc_info is None:
        exc_info = sys.exc_info()
    if isinstance(exc_info, BaseException):
        exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
    if not exc_info or exc_info[0] is None or exc_info[1] is None:
        return None
    projected = projection.project_exception(exc_info[1])
    assert projected is not None
    return (exc_info[0], projected, exc_info[2])
