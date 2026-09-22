"""
This module implements the FormRequest class which is a more convenient class
(than Request) to generate Requests based on form data.

See documentation in docs/topics/request-response.rst
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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
    from lxml.etree import _Element
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
        controls = _get_form_controls(form)
        clicked = None if dont_click else _get_clickable(clickdata, controls)
        formdata = _get_inputs(controls, formdata, clicked)

        url = kwargs.pop("url", None)
        if url is None and clicked is not None:
            formaction = clicked.get("formaction")
            if formaction is not None:
                url = strip_html5_whitespace(formaction)
        url = _get_form_url(form, url)

        if "method" in kwargs:
            method = kwargs.pop("method")
        else:
            method = clicked.get("formmethod") if clicked is not None else None
            if method is None:
                method = form.method
        if method is not None:
            method = method.upper()
            if method not in cls.valid_form_methods:
                method = "GET"

        return cls(url=url, method=method, formdata=formdata, **kwargs)


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


def _get_form_controls(form: FormElement) -> list[HtmlElement]:
    """Return the controls whose form owner is *form*, in document order.

    The form owner of a control with a *form* attribute is the form with
    that id, wherever the control is in the document. For other controls
    it is their ancestor form.
    """
    form_id = form.get("id")
    controls = []
    # XPath from the tree, not from the form element, so that controls
    # outside the form are also covered. Broken HTML may place elements
    # in top-level elements that tree iteration from the root would miss.
    for el in form.getroottree().xpath("//button|//input|//select|//textarea"):
        owner = el.get("form")
        if owner is not None:
            if form_id is not None and owner == form_id:
                controls.append(el)
        elif _is_descendant(el, form):
            controls.append(el)
    return controls


def _is_descendant(el: _Element, ancestor: _Element) -> bool:
    parent = el.getparent()
    while parent is not None:
        if parent is ancestor:
            return True
        parent = parent.getparent()
    return False


def _is_disabled(control: HtmlElement) -> bool:
    """Return whether the control is disabled and hence never submitted.

    A control is disabled when it or an ancestor fieldset has a *disabled*
    attribute, unless the control is inside the first legend of that
    fieldset.
    """
    if control.get("disabled") is not None:
        return True
    for fieldset in control.iterancestors("fieldset"):
        if fieldset.get("disabled") is None:
            continue
        legend = fieldset.find("legend")
        if legend is not None and _is_descendant(control, legend):
            continue
        return True
    return False


def _get_inputs(
    controls: list[HtmlElement],
    formdata: FormdataType,
    clicked: HtmlElement | None,
) -> list[FormdataKVType]:
    """Return a list of key-value pairs for the successful controls."""
    try:
        formdata_keys = dict(formdata or ()).keys()
    except (ValueError, TypeError):
        raise ValueError("formdata should be a dict or iterable of tuples") from None

    if not formdata:
        formdata = []
    values: list[FormdataKVType] = []
    for control in controls:
        if not _is_submittable(control):
            continue
        k, v = _value(control)
        if k and k not in formdata_keys:
            values.append((k, "" if v is None else v))

    if clicked is not None:
        name = clicked.get("name")
        if name is not None and name not in formdata:
            values.append((name, clicked.get("value") or ""))

    formdata_items = formdata.items() if isinstance(formdata, Mapping) else formdata
    values.extend((k, v) for k, v in formdata_items if v is not None)
    return values


def _is_submittable(control: HtmlElement) -> bool:
    """Return whether the control contributes its value to the form data.

    Buttons only contribute a value as the clicked submitter, and disabled
    controls, submit-like inputs and unchecked checkboxes and radio
    buttons never contribute one.
    """
    if control.tag == "button" or _is_disabled(control):
        return False
    if control.tag != "input":
        return True
    input_type = control.get("type")
    if input_type is None:
        return True
    input_type = input_type.lower()
    if input_type in ("submit", "image", "reset"):
        return False
    return input_type not in ("checkbox", "radio") or control.get("checked") is not None


def _value(
    ele: HtmlElement,
) -> tuple[str | None, str | list[str] | None]:
    if ele.tag == "select":
        select = cast("SelectElement", ele)
        return select.name, _select_values(select)
    control = cast("InputElement | TextareaElement", ele)
    return control.name, control.value


def _select_values(ele: SelectElement) -> list[str]:
    """Return the values of the selected options of a select element.

    Disabled options and options of a disabled optgroup are skipped. When
    no option is selected and the select is not multiple, the first
    enabled option is returned, matching browser behavior.
    """
    options = [option for option in ele.iter("option") if _is_enabled_option(option)]
    if ele.multiple:
        return [_option_value(o) for o in options if o.get("selected") is not None]
    selected = [o for o in options if o.get("selected") is not None]
    if selected:
        return [_option_value(selected[-1])]
    return [_option_value(options[0])] if options else []


def _is_enabled_option(option: HtmlElement) -> bool:
    if option.get("disabled") is not None:
        return False
    optgroup = option.getparent()
    return not (
        optgroup is not None
        and optgroup.tag == "optgroup"
        and optgroup.get("disabled") is not None
    )


def _option_value(option: HtmlElement) -> str:
    value = option.get("value")
    if value is not None:
        return value
    return (option.text or "").strip()


def _get_clickable(
    clickdata: dict[str, str | int] | None, controls: list[HtmlElement]
) -> HtmlElement | None:
    """Return the clickable element specified in clickdata, if the latter is
    given. If not, return the first clickable element found.
    """
    clickables = [el for el in controls if _is_clickable(el)]
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
            el = [c for c in controls if c.tag in ("input", "select", "textarea")][nr]
        except IndexError:
            pass
        else:
            return el

    # We didn't find it, so now we match the other arguments against the
    # attributes of the form controls
    matches = [
        el for el in controls if all(el.get(k) == str(v) for k, v in clickdata.items())
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"Multiple elements found ({matches!r}) matching the "
            f"criteria in clickdata: {clickdata!r}"
        )
    raise ValueError(f"No clickable element matching clickdata: {clickdata!r}")


def _is_clickable(control: HtmlElement) -> bool:
    """Return whether the control can be the clicked submitter."""
    if _is_disabled(control):
        return False
    input_type = control.get("type")
    if control.tag == "input":
        return input_type is not None and input_type.lower() in ("submit", "image")
    return control.tag == "button" and (
        input_type is None or input_type.lower() == "submit"
    )
