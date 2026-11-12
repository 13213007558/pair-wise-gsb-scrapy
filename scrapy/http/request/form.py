"""
This module implements the FormRequest class which is a more convenient class
(than Request) to generate Requests based on form data.

See documentation in docs/topics/request-response.rst
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Self, TypeAlias, cast
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from warnings import warn

from parsel.csstranslator import HTMLTranslator
from w3lib.html import strip_html5_whitespace
from w3lib.url import canonicalize_url

from scrapy.exceptions import FormSubmissionRejected, ScrapyDeprecationWarning
from scrapy.http.request import Request
from scrapy.utils.python import to_bytes

if TYPE_CHECKING:
    from lxml.html import (
        FormElement,
        InputElement,
        MultipleSelectOptions,
        SelectElement,
        TextareaElement,
    )

    from scrapy.http.response.text import TextResponse

FormdataVType: TypeAlias = str | bytes | Iterable[str] | Iterable[bytes]
FormdataKVType: TypeAlias = tuple[str | bytes, FormdataVType]
FormdataType: TypeAlias = (
    Mapping[str, FormdataVType]
    | Mapping[bytes, FormdataVType]
    | Iterable[FormdataKVType]
    | None
)


class FormSubmissionIntent:
    """A set of values approved for a form submission by the code that
    creates the :class:`FormRequest`.

    Pass an instance as the *submission_intent* parameter of a
    :class:`FormRequest`, including through
    :meth:`~scrapy.FormRequest.from_response`, to require that the request
    that is finally sent still matches the approved target, method and fields.
    The check is repeated for requests derived from that request, such as
    redirects and retries, which stop the crawl with
    :exc:`~scrapy.exceptions.FormSubmissionRejected` when they no longer
    match.

    :param targets: absolute URLs approved as submission targets. The form
        ``action`` is resolved against the response URL before it is compared.
        The query string and the fragment are not compared. Query parameters
        of the form ``action`` are checked like the submitted field names.
    :param methods: approved HTTP methods, e.g. ``"POST"``. They are compared
        case-insensitively.
    :param field_categories: maps a category name to the names of the fields
        that belong to it, e.g.
        ``{"credentials": ["username", "password"], "search": ["q"]}``. Every
        field submitted by the request must be listed in one of the categories
        in this mapping. Matching is by field name only; field values are not
        stored or checked. A field name may only belong to one category.

    For example:

    .. code-block:: python

        intent = FormSubmissionIntent(
            targets=["https://example.com/users/login"],
            methods=["POST"],
            field_categories={
                "credentials": ["username", "password"],
                "csrf": ["csrf_token"],
            },
        )
        yield FormRequest.from_response(
            response,
            formname="login",
            formdata={"username": "john", "password": "secret"},
            submission_intent=intent,
        )

    .. versionadded:: VERSION
    """

    __slots__ = ("_approved_field_names", "_field_categories", "_methods", "_targets")

    def __init__(
        self,
        *,
        targets: Iterable[str],
        methods: Iterable[str],
        field_categories: Mapping[str, Iterable[str]],
    ) -> None:
        self._targets = frozenset(
            _normalize_target(target, "target") for target in _as_iterable(targets)
        )
        if not self._targets:
            raise ValueError("targets must contain at least one URL")

        self._methods = frozenset(
            _normalize_method(method) for method in _as_iterable(methods)
        )
        if not self._methods:
            raise ValueError("methods must contain at least one HTTP method")

        categories: dict[str, frozenset[str]] = {}
        owners: dict[str, str] = {}
        for category, names in field_categories.items():
            if not isinstance(category, str) or not category:
                raise ValueError("field category names must be non-empty strings")
            category_names = frozenset(
                _normalize_field_name(name) for name in _as_iterable(names)
            )
            for name in category_names:
                if name in owners:
                    raise ValueError(
                        f"field {name!r} belongs to more than one field category, "
                        f"{owners[name]!r} and {category!r}"
                    )
                owners[name] = category
            categories[category] = category_names
        self._field_categories = categories
        self._approved_field_names = frozenset(owners)

    @property
    def targets(self) -> frozenset[str]:
        """The approved submission targets, normalized."""
        return self._targets

    @property
    def methods(self) -> frozenset[str]:
        """The approved HTTP methods, upper-cased."""
        return self._methods

    @property
    def field_categories(self) -> Mapping[str, frozenset[str]]:
        """The approved field categories and their field names."""
        return MappingProxyType(self._field_categories)

    def _check_request(self, request: Request) -> None:
        """Reject *request* if it does not match the approved intent."""
        parts = urlsplit(request.url)
        if parts.username or parts.password:
            raise FormSubmissionRejected(
                "Form submission rejected: the request target contains "
                "credentials, which approved targets cannot contain"
            )
        target = _target_key(request.url)
        if target not in self._targets:
            approved = _format_targets(self._targets)
            raise FormSubmissionRejected(
                f"Form submission rejected: request target "
                f"{_display_target(target)!r} is not approved; approved "
                f"targets: {approved}"
            )

        method = str(request.method).upper()
        if method not in self._methods:
            approved = ", ".join(sorted(self._methods))
            raise FormSubmissionRejected(
                f"Form submission rejected: request method {method!r} is not "
                f"approved; approved methods: {approved}"
            )

        field_names = _get_request_field_names(request)
        if field_names is None:
            raise FormSubmissionRejected(
                "Form submission rejected: the request body cannot be checked "
                "against the approved field categories; only "
                "application/x-www-form-urlencoded form data is supported"
            )
        unapproved = sorted(field_names - self._approved_field_names)
        if unapproved:
            raise FormSubmissionRejected(
                f"Form submission rejected: form fields {unapproved} are not "
                f"approved; add them to an approved field category or remove "
                f"them from the form data"
            )


class FormRequest(Request):
    """A :class:`~scrapy.Request` subclass with a ``formdata`` parameter that
    url-encodes the given data and assigns it to the request, which makes it
    convenient to send arbitrary form data via HTTP POST or GET without an HTML
    ``<form>`` element to parse.

    .. note:: To build a request from an HTML ``<form>`` element found in a
       response, use :doc:`form2request <form2request:index>` instead. See
       :ref:`form`.

    The remaining arguments are the same as for the :class:`~scrapy.Request`
    class and are not documented here.

    :param formdata: a dictionary (or iterable of (key, value) tuples)
       containing HTML form data which will be url-encoded. If
       :attr:`~scrapy.Request.method` is not given and ``formdata`` is
       provided, the method is set to ``"POST"`` and the data is assigned to
       the request body; if the method is ``"GET"``, the data is added to the
       URL query string instead.
    :type formdata: dict or collections.abc.Iterable

    *submission_intent* optionally restricts the submission to an approved
    target, method and set of field names. The request is checked when it is
    built, and requests derived from it, such as redirects and retries, are
    checked again before they are sent.

    To send data via HTTP POST, simulating an HTML form submission, return a
    :class:`~scrapy.FormRequest` object from your spider:

    .. skip: next
    .. code-block:: python

        return [
            FormRequest(
                url="http://www.example.com/post/action",
                formdata={"name": "John Doe", "age": "27"},
                callback=self.after_post,
            )
        ]

    To send the data in the URL query string instead, use the ``GET`` method:

    .. skip: next
    .. code-block:: python

        return [
            FormRequest(
                url="http://www.example.com/search",
                method="GET",
                formdata={"q": "keyword", "page": "1"},
                callback=self.parse_results,
            )
        ]
    """

    __slots__ = ("_submission_intent",)

    attributes: tuple[str, ...] = (*Request.attributes, "submission_intent")

    valid_form_methods: ClassVar[list[str]] = ["GET", "POST"]

    def __init__(
        self,
        *args: Any,
        formdata: FormdataType = None,
        submission_intent: FormSubmissionIntent | None = None,
        **kwargs: Any,
    ) -> None:
        if formdata and kwargs.get("method") is None:
            kwargs["method"] = "POST"

        super().__init__(*args, **kwargs)
        self._submission_intent = submission_intent

        if formdata:
            items = formdata.items() if isinstance(formdata, Mapping) else formdata
            form_query_str = _urlencode(items, self.encoding)
            if self.method == "POST":
                self.headers.setdefault(
                    b"Content-Type", b"application/x-www-form-urlencoded"
                )
                self._set_body(form_query_str)
            else:
                self._set_url(
                    urlunsplit(urlsplit(self.url)._replace(query=form_query_str))
                )

        if submission_intent is not None:
            submission_intent._check_request(self)

    @property
    def submission_intent(self) -> FormSubmissionIntent | None:
        """The :class:`FormSubmissionIntent` that the request must match,
        or ``None``.

        .. versionadded:: VERSION
        """
        return self._submission_intent

    @classmethod
    def from_response(
        cls,
        response: TextResponse,
        formname: str | None = None,
        formid: str | None = None,
        formnumber: int = 0,
        formdata: FormdataType = None,
        clickdata: dict[str, str | int] | None = None,
        dont_click: bool = False,
        formxpath: str | None = None,
        formcss: str | None = None,
        submission_intent: FormSubmissionIntent | None = None,
        **kwargs: Any,
    ) -> Self:
        warn(
            "FormRequest.from_response() is deprecated. Use the form2request "
            "library instead.",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )

        kwargs.setdefault("encoding", response.encoding)

        if formcss is not None:
            formxpath = HTMLTranslator().css_to_xpath(formcss)

        form = _get_form(response, formname, formid, formnumber, formxpath)
        formdata = _get_inputs(form, formdata, dont_click, clickdata)
        url = _get_form_url(form, kwargs.pop("url", None))

        method = kwargs.pop("method", form.method)
        if method is not None:
            method = method.upper()
            if method not in cls.valid_form_methods:
                method = "GET"

        return cls(
            url=url,
            method=method,
            formdata=formdata,
            submission_intent=submission_intent,
            **kwargs,
        )


def _as_iterable(value: Any) -> Iterable[Any]:
    """Return *value* as an iterable, treating strings as single items."""
    if isinstance(value, (str, bytes)):
        return (value,)
    return value


def _format_targets(targets: Iterable[str]) -> str:
    return ", ".join(sorted(targets))


def _normalize_target(target: str, name: str) -> str:
    if not isinstance(target, str):
        raise TypeError(f"{name} must be a string, got {type(target).__name__}")
    if "://" not in target:
        raise ValueError(
            f"{name} {_display_target(target)!r} is not an absolute URL; "
            f"resolve relative URLs before approving them"
        )
    parts = urlsplit(target)
    if parts.username or parts.password:
        raise ValueError(
            f"{name} {_display_target(target)!r} must not have credentials"
        )
    if parts.fragment:
        raise ValueError(f"{name} {_display_target(target)!r} must not have a fragment")
    return _target_key(target)


def _target_key(url: str) -> str:
    """Return the canonical path-level form of *url* used to compare targets.

    The query string is left out: for ``GET`` submissions it holds the form
    data, whose field names are checked against the approved field
    categories.
    """
    parts = urlsplit(canonicalize_url(url))
    netloc = parts.hostname or ""
    default_port = {"http": 80, "https": 443}.get(parts.scheme)
    if parts.port is not None and parts.port != default_port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _normalize_method(method: str) -> str:
    if not isinstance(method, str):
        raise TypeError(f"method must be a string, got {type(method).__name__}")
    method = method.upper()
    if not method:
        raise ValueError("methods must not contain empty values")
    return method


def _normalize_field_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("field names must be non-empty strings")
    return name


def _display_target(target: str) -> str:
    """Return a URL for an error message, without credentials, query or
    fragments."""
    parts = urlsplit(target)
    netloc = parts.hostname or ""
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _get_request_field_names(request: Request) -> frozenset[str] | None:
    """Return the names of the form fields that *request* would submit, or
    ``None`` if the body is not url-encoded form data."""
    names = _query_names(urlsplit(request.url).query)
    if request.method != "POST":
        return frozenset(names)
    if not request.body:
        return frozenset(names)
    content_type = request.headers.get("Content-Type", b"")
    if content_type:
        if not isinstance(content_type, bytes):
            return None
        scheme = content_type.partition(b";")[0].strip().lower()
        if scheme != b"application/x-www-form-urlencoded":
            return None
    names.update(_query_names(request.body.decode(request.encoding)))
    return frozenset(names)


def _query_names(query: str) -> set[str]:
    return {name for name, _ in parse_qsl(query, keep_blank_values=True)}


def _get_form_url(form: FormElement, url: str | None) -> str:
    assert form.base_url is not None  # typing
    if url is None:
        action = form.get("action")
        if action is None:
            return form.base_url
        return urljoin(form.base_url, strip_html5_whitespace(action))
    return urljoin(form.base_url, url)


def _urlencode(seq: Iterable[FormdataKVType], enc: str) -> str:
    values = [
        (to_bytes(k, enc), to_bytes(v, enc))
        for k, vs in seq
        for v in ([vs] if isinstance(vs, (str, bytes)) else vs)
    ]
    return urlencode(values, doseq=True)


def _get_form(
    response: TextResponse,
    formname: str | None,
    formid: str | None,
    formnumber: int,
    formxpath: str | None,
) -> FormElement:
    """Find the wanted form element within the given response."""
    root = response.selector.root
    forms = root.xpath("//form")
    if not forms:
        raise ValueError(f"No <form> element found in {response}")

    if formname is not None:
        f = root.xpath(f'//form[@name="{formname}"]')
        if f:
            return cast("FormElement", f[0])

    if formid is not None:
        f = root.xpath(f'//form[@id="{formid}"]')
        if f:
            return cast("FormElement", f[0])

    # Get form element from xpath, if not found, go up
    if formxpath is not None:
        nodes = root.xpath(formxpath)
        if nodes:
            el = nodes[0]
            while True:
                if el.tag == "form":
                    return cast("FormElement", el)
                el = el.getparent()
                if el is None:
                    break
        raise ValueError(f"No <form> element found with {formxpath}")

    # If we get here, it means that either formname was None or invalid
    try:
        form = forms[formnumber]
    except IndexError:
        raise IndexError(f"Form number {formnumber} not found in {response}") from None
    return cast("FormElement", form)


def _get_inputs(
    form: FormElement,
    formdata: FormdataType,
    dont_click: bool,
    clickdata: dict[str, str | int] | None,
) -> list[FormdataKVType]:
    """Return a list of key-value pairs for the inputs found in the given form."""
    try:
        formdata_keys = dict(formdata or ()).keys()
    except (ValueError, TypeError):
        raise ValueError("formdata should be a dict or iterable of tuples") from None

    if not formdata:
        formdata = []
    inputs = form.xpath(
        "descendant::textarea"
        "|descendant::select"
        "|descendant::input[not(@type) or @type["
        ' not(re:test(., "^(?:submit|image|reset)$", "i"))'
        " and (../@checked or"
        '  not(re:test(., "^(?:checkbox|radio)$", "i")))]]',
        namespaces={"re": "http://exslt.org/regular-expressions"},
    )
    values: list[FormdataKVType] = [
        (k, "" if v is None else v)
        for k, v in (_value(e) for e in inputs)
        if k and k not in formdata_keys
    ]

    if not dont_click:
        clickable = _get_clickable(clickdata, form)
        if clickable and clickable[0] not in formdata and clickable[0] is not None:
            values.append(clickable)

    formdata_items = formdata.items() if isinstance(formdata, Mapping) else formdata
    values.extend((k, v) for k, v in formdata_items if v is not None)
    return values


def _value(
    ele: InputElement | SelectElement | TextareaElement,
) -> tuple[str | None, str | MultipleSelectOptions | None]:
    n = ele.name
    v = ele.value
    if ele.tag == "select":
        return _select_value(cast("SelectElement", ele), n, v)
    return n, v


def _select_value(
    ele: SelectElement, n: str | None, v: str | MultipleSelectOptions | None
) -> tuple[str | None, str | MultipleSelectOptions | None]:
    multiple = ele.multiple
    if v is None and not multiple:
        # Match browser behaviour on simple select tag without options selected
        # And for select tags without options
        o = ele.value_options
        return (n, o[0]) if o else (None, None)
    return n, v


def _get_clickable(
    clickdata: dict[str, str | int] | None, form: FormElement
) -> tuple[str, str] | None:
    """
    Returns the clickable element specified in clickdata,
    if the latter is given. If not, it returns the first
    clickable element found
    """
    clickables = list(
        form.xpath(
            'descendant::input[re:test(@type, "^(submit|image)$", "i")]'
            '|descendant::button[not(@type) or re:test(@type, "^submit$", "i")]',
            namespaces={"re": "http://exslt.org/regular-expressions"},
        )
    )
    if not clickables:
        return None

    # If we don't have clickdata, we just use the first clickable element
    if clickdata is None:
        el = clickables[0]
        return (el.get("name"), el.get("value") or "")

    # If clickdata is given, we compare it to the clickable elements to find a
    # match. We first look to see if the number is specified in clickdata,
    # because that uniquely identifies the element
    nr = clickdata.get("nr", None)
    if nr is not None:
        assert isinstance(nr, int)
        try:
            el = list(form.inputs)[nr]
        except IndexError:
            pass
        else:
            return (cast("str", el.get("name")), el.get("value") or "")

    # We didn't find it, so now we build an XPath expression out of the other
    # arguments, because they can be used as such
    xpath = ".//*" + "".join(f'[@{k}="{v}"]' for k, v in clickdata.items())
    el = form.xpath(xpath)
    if len(el) == 1:
        return (el[0].get("name"), el[0].get("value") or "")
    if len(el) > 1:
        raise ValueError(
            f"Multiple elements found ({el!r}) matching the "
            f"criteria in clickdata: {clickdata!r}"
        )
    raise ValueError(f"No clickable element matching clickdata: {clickdata!r}")
