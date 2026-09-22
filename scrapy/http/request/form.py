"""
This module implements the FormRequest class which is a more convenient class
(than Request) to generate Requests based on form data.

See documentation in docs/topics/request-response.rst
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import TYPE_CHECKING, Any, ClassVar, TypeAlias, cast
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from warnings import warn

from parsel.csstranslator import HTMLTranslator
from w3lib.html import strip_html5_whitespace

from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http.request import Request
from scrapy.utils.python import is_listlike, to_bytes

if TYPE_CHECKING:
    # typing.Self requires Python 3.11
    from lxml.html import (
        FormElement,
        HtmlElement,
        InputElement,
        SelectElement,
        TextareaElement,
    )
    from typing_extensions import Self

    from scrapy.http.response.text import TextResponse

FormdataVType: TypeAlias = str | Iterable[str]
FormdataKVType: TypeAlias = tuple[str, FormdataVType]
FormdataType: TypeAlias = Mapping[str, FormdataVType] | Iterable[FormdataKVType] | None


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

    __slots__ = ()

    valid_form_methods: ClassVar[list[str]] = ["GET", "POST"]

    def __init__(
        self, *args: Any, formdata: FormdataType = None, **kwargs: Any
    ) -> None:
        if formdata and kwargs.get("method") is None:
            kwargs["method"] = "POST"

        super().__init__(*args, **kwargs)

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
        clicked = None if dont_click else _get_clickable(clickdata, form)
        formdata = _get_inputs(form, formdata, clicked)
        url = _get_form_url(form, kwargs.pop("url", None), clicked)

        if "method" in kwargs:
            method = kwargs.pop("method")
        else:
            method = _get_submission_method(form, clicked)
        if method is not None:
            method = method.upper()
            if method not in cls.valid_form_methods:
                method = "GET"

        return cls(url=url, method=method, formdata=formdata, **kwargs)


def _get_submission_method(
    form: FormElement, clicked: HtmlElement | None
) -> str | None:
    if clicked is not None:
        formmethod = clicked.get("formmethod")
        if formmethod is not None:
            return formmethod
    return form.method


def _get_form_url(
    form: FormElement, url: str | None, clicked: HtmlElement | None
) -> str:
    assert form.base_url is not None  # typing
    if url is None:
        action = clicked.get("formaction") if clicked is not None else None
        if action is None:
            action = form.get("action")
        if action is None:
            return form.base_url
        return urljoin(form.base_url, strip_html5_whitespace(action))
    return urljoin(form.base_url, url)


def _urlencode(seq: Iterable[FormdataKVType], enc: str) -> str:
    values = [
        (to_bytes(k, enc), to_bytes(v, enc))
        for k, vs in seq
        for v in (vs if is_listlike(vs) else [cast("str", vs)])
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
    clicked: HtmlElement | None,
) -> list[FormdataKVType]:
    """Return a list of key-value pairs for the successful controls of the
    given form, in document order."""
    try:
        formdata_keys = dict(formdata or ()).keys()
    except (ValueError, TypeError):
        raise ValueError("formdata should be a dict or iterable of tuples") from None

    if not formdata:
        formdata = []
    values: list[FormdataKVType] = []
    for el in _owned_controls(form):
        if clicked is not None and el is clicked:
            name = el.get("name")
            if name and name not in formdata_keys:
                values.append((name, el.get("value") or ""))
            continue
        if _is_disabled(el) or _is_submitter(el) or el.tag == "button":
            continue
        if el.tag == "input":
            input_type = el.get("type", "").lower()
            if input_type == "reset" or (
                input_type in ("checkbox", "radio") and el.get("checked") is None
            ):
                continue
        name = el.get("name")
        if not name or name in formdata_keys:
            continue
        if el.tag == "select":
            values.extend(
                (name, value) for value in _select_values(cast("SelectElement", el))
            )
        else:
            value = cast("InputElement | TextareaElement", el).value
            values.append((name, "" if value is None else value))

    formdata_items = formdata.items() if isinstance(formdata, Mapping) else formdata
    values.extend((k, v) for k, v in formdata_items if v is not None)
    return values


def _owned_controls(form: FormElement) -> Iterator[HtmlElement]:
    """Yield the controls owned by *form* in document order.

    A control is owned by a form when its ``form`` attribute matches the
    form id or, without a ``form`` attribute, when the form is its closest ancestor
    form.
    """
    elements = list(_document_elements(form))
    forms = [el for el in elements if el.tag == "form"]
    for el in elements:
        if (
            el.tag in ("input", "select", "textarea", "button")
            and _form_owner(el, forms) is form
        ):
            yield el


def _document_elements(element: HtmlElement) -> Iterator[HtmlElement]:
    """Yield the elements of the document in document order.

    Malformed HTML can make the parser create additional top-level elements,
    siblings of the document root, which the root iteration does not reach.
    """
    root = element.getroottree().getroot()
    while root is not None:
        yield from root.iter()
        root = root.getnext()


def _form_owner(el: HtmlElement, forms: list[HtmlElement]) -> HtmlElement | None:
    form_id = el.get("form")
    if form_id is not None:
        return next((form for form in forms if form.get("id") == form_id), None)
    for ancestor in el.iterancestors("form"):
        return ancestor
    return None


def _is_disabled(el: HtmlElement) -> bool:
    """Return True if *el* is disabled, directly or through a fieldset.

    A disabled fieldset disables its descendants, except those inside its
    first legend child.
    """
    if el.get("disabled") is not None:
        return True
    for fieldset in el.iterancestors("fieldset"):
        if fieldset.get("disabled") is None:
            continue
        legend = fieldset.find("legend")
        if legend is not None and el in legend.iterdescendants():
            continue
        return True
    return False


def _is_submitter(el: HtmlElement) -> bool:
    """Return True if *el* can submit a form when clicked."""
    if el.tag == "button":
        return el.get("type", "submit").lower() == "submit"
    return el.tag == "input" and el.get("type", "").lower() in ("submit", "image")


def _select_values(select: SelectElement) -> list[str]:
    """Return the values that *select* submits.

    Disabled options and options of disabled optgroups are not submitted.
    When no option is selected, a single-option select submits its first
    available option.
    """
    selected = [
        option for option in select.iter("option") if option.get("selected") is not None
    ]
    if select.multiple:
        return [
            _option_value(option) for option in selected if not _option_disabled(option)
        ]
    if selected:
        option = selected[-1]
        return [] if _option_disabled(option) else [_option_value(option)]
    for option in select.iter("option"):
        if not _option_disabled(option):
            return [_option_value(option)]
    return []


def _option_disabled(option: HtmlElement) -> bool:
    if option.get("disabled") is not None:
        return True
    parent = option.getparent()
    return (
        parent is not None
        and parent.tag == "optgroup"
        and parent.get("disabled") is not None
    )


def _option_value(option: HtmlElement) -> str:
    value = option.get("value")
    if value is None:
        value = (option.text or "").strip()
    return value


def _get_clickable(
    clickdata: dict[str, str | int] | None, form: FormElement
) -> HtmlElement | None:
    """Return the element that submits the form: the one matching *clickdata*
    if given, else the first submit element in document order."""
    clickables = [
        el for el in _owned_controls(form) if _is_submitter(el) and not _is_disabled(el)
    ]
    if not clickables:
        return None

    # If we don't have clickdata, we just use the first clickable element
    if clickdata is None:
        return clickables[0]

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
            return el

    # We didn't find it, so now we look for the clickable elements matching
    # all the given attributes
    matches = [
        el
        for el in clickables
        if all(el.get(key) == str(value) for key, value in clickdata.items())
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"Multiple elements found ({matches!r}) matching the "
            f"criteria in clickdata: {clickdata!r}"
        )
    raise ValueError(f"No clickable element matching clickdata: {clickdata!r}")
