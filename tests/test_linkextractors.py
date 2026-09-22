from __future__ import annotations

import pickle
import re
from typing import TYPE_CHECKING, Any

import pytest
from w3lib.url import canonicalize_url

from scrapy import Spider
from scrapy.http import HtmlResponse, Request, TextResponse, XmlResponse
from scrapy.link import Link
from scrapy.linkextractors import lxmlhtml
from scrapy.linkextractors.lxmlhtml import LxmlLinkExtractor, LxmlParserLinkExtractor
from scrapy.utils.test import get_crawler
from tests import get_testdata
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from tests.mockserver.http import MockServer


class TestLxmlLinkExtractor:
    def setup_method(self):
        body = get_testdata("link_extractor", "linkextractor.html")
        self.response = HtmlResponse(url="http://example.com/index", body=body)

    def test_urls_type(self):
        """Test that the resulting urls are str objects"""
        lx = LxmlLinkExtractor()
        assert all(
            isinstance(link.url, str) for link in lx.extract_links(self.response)
        )

    def test_extract_all_links(self):
        lx = LxmlLinkExtractor()
        page4_url = "http://example.com/page%204.html"

        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
            Link(
                url="http://example.com/sample3.html#foo",
                text="sample 3 repetition with fragment",
            ),
            Link(url="http://www.google.com/something", text=""),
            Link(url="http://example.com/innertag.html", text="inner tag"),
            Link(url=page4_url, text="href with whitespaces"),
        ]

    def test_extract_filter_allow(self):
        lx = LxmlLinkExtractor(allow=("sample",))
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
            Link(
                url="http://example.com/sample3.html#foo",
                text="sample 3 repetition with fragment",
            ),
        ]

    def test_extract_filter_allow_with_duplicates(self):
        lx = LxmlLinkExtractor(allow=("sample",), unique=False)
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
            Link(
                url="http://example.com/sample3.html",
                text="sample 3 repetition",
            ),
            Link(
                url="http://example.com/sample3.html",
                text="sample 3 repetition",
            ),
            Link(
                url="http://example.com/sample3.html#foo",
                text="sample 3 repetition with fragment",
            ),
        ]

    def test_extract_filter_allow_with_duplicates_canonicalize(self):
        lx = LxmlLinkExtractor(allow=("sample",), unique=False, canonicalize=True)
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
            Link(
                url="http://example.com/sample3.html",
                text="sample 3 repetition",
            ),
            Link(
                url="http://example.com/sample3.html",
                text="sample 3 repetition",
            ),
            Link(
                url="http://example.com/sample3.html",
                text="sample 3 repetition with fragment",
            ),
        ]

    def test_extract_filter_allow_no_duplicates_canonicalize(self):
        lx = LxmlLinkExtractor(allow=("sample",), unique=True, canonicalize=True)
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
        ]

    def test_extract_filter_allow_and_deny(self):
        lx = LxmlLinkExtractor(allow=("sample",), deny=("3",))
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
        ]

    def test_extract_filter_allowed_domains(self):
        lx = LxmlLinkExtractor(allow_domains=("google.com",))
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://www.google.com/something", text=""),
        ]

    def test_extraction_using_single_values(self):
        """Test the extractor's behaviour among different situations"""

        lx = LxmlLinkExtractor(allow="sample")
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
            Link(
                url="http://example.com/sample3.html#foo",
                text="sample 3 repetition with fragment",
            ),
        ]

        lx = LxmlLinkExtractor(allow="sample", deny="3")
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
        ]

        lx = LxmlLinkExtractor(allow_domains="google.com")
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://www.google.com/something", text=""),
        ]

        lx = LxmlLinkExtractor(deny_domains="example.com")
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://www.google.com/something", text=""),
        ]

    def test_nofollow(self):
        """Test the extractor's behaviour for links with rel='nofollow'"""

        html = b"""<html><head><title>Page title</title></head>
        <body>
        <div class='links'>
        <p><a href="/about.html">About us</a></p>
        </div>
        <div>
        <p><a href="/follow.html">Follow this link</a></p>
        </div>
        <div>
        <p><a href="/nofollow.html" rel="nofollow">Dont follow this one</a></p>
        </div>
        <div>
        <p><a href="/nofollow2.html" rel="blah">Choose to follow or not</a></p>
        </div>
        <div>
        <p><a href="http://google.com/something" rel="external nofollow">External link not to follow</a></p>
        </div>
        </body></html>"""
        response = HtmlResponse("http://example.org/somepage/index.html", body=html)

        lx = LxmlLinkExtractor()
        assert lx.extract_links(response) == [
            Link(url="http://example.org/about.html", text="About us"),
            Link(url="http://example.org/follow.html", text="Follow this link"),
            Link(
                url="http://example.org/nofollow.html",
                text="Dont follow this one",
                nofollow=True,
            ),
            Link(
                url="http://example.org/nofollow2.html",
                text="Choose to follow or not",
            ),
            Link(
                url="http://google.com/something",
                text="External link not to follow",
                nofollow=True,
            ),
        ]

    def test_matches(self):
        url1 = "http://lotsofstuff.com/stuff1/index"
        url2 = "http://evenmorestuff.com/uglystuff/index"

        lx = LxmlLinkExtractor(allow=(r"stuff1",))
        assert lx.matches(url1)
        assert not lx.matches(url2)

        lx = LxmlLinkExtractor(deny=(r"uglystuff",))
        assert lx.matches(url1)
        assert not lx.matches(url2)

        lx = LxmlLinkExtractor(allow_domains=("evenmorestuff.com",))
        assert not lx.matches(url1)
        assert lx.matches(url2)

        lx = LxmlLinkExtractor(deny_domains=("lotsofstuff.com",))
        assert not lx.matches(url1)
        assert lx.matches(url2)

        lx = LxmlLinkExtractor(
            allow=["blah1"],
            deny=["blah2"],
            allow_domains=["blah1.com"],
            deny_domains=["blah2.com"],
        )
        assert lx.matches("http://blah1.com/blah1")
        assert not lx.matches("http://blah1.com/blah2")
        assert not lx.matches("http://blah2.com/blah1")
        assert not lx.matches("http://blah2.com/blah2")

    def test_restrict_xpaths(self):
        lx = LxmlLinkExtractor(restrict_xpaths=('//div[@id="subwrapper"]',))
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
        ]

    def test_restrict_xpaths_encoding(self):
        """Test restrict_xpaths with encodings"""
        html = b"""<html><head><title>Page title</title></head>
        <body><p><a href="item/12.html">Item 12</a></p>
        <div class='links'>
        <p><a href="/about.html">About us\xa3</a></p>
        </div>
        <div>
        <p><a href="/nofollow.html">This shouldn't be followed</a></p>
        </div>
        </body></html>"""
        response = HtmlResponse(
            "http://example.org/somepage/index.html",
            body=html,
            encoding="windows-1252",
        )

        lx = LxmlLinkExtractor(restrict_xpaths="//div[@class='links']")
        assert lx.extract_links(response) == [
            Link(url="http://example.org/about.html", text="About us\xa3")
        ]

    def test_restrict_xpaths_with_html_entities(self):
        html = b'<html><body><p><a href="/&hearts;/you?c=&euro;">text</a></p></body></html>'
        response = HtmlResponse(
            "http://example.org/somepage/index.html",
            body=html,
            encoding="iso8859-15",
        )
        links = LxmlLinkExtractor(restrict_xpaths="//p").extract_links(response)
        assert links == [
            Link(url="http://example.org/%E2%99%A5/you?c=%A4", text="text")
        ]

    def test_restrict_xpaths_concat_in_handle_data(self):
        """html entities cause SGMLParser to call handle_data hook twice"""
        body = b"""<html><body><div><a href="/foo">&gt;\xbe\xa9&lt;\xb6\xab</a></body></html>"""
        response = HtmlResponse("http://example.org", body=body, encoding="gb18030")
        lx = LxmlLinkExtractor(restrict_xpaths="//div")
        assert lx.extract_links(response) == [
            Link(
                url="http://example.org/foo",
                text=">\u4eac<\u4e1c",
                fragment="",
                nofollow=False,
            )
        ]

    def test_restrict_css(self):
        lx = LxmlLinkExtractor(restrict_css=("#subwrapper a",))
        assert lx.extract_links(self.response) == [
            Link(url="http://example.com/sample2.html", text="sample 2")
        ]

    def test_restrict_css_and_restrict_xpaths_together(self):
        lx = LxmlLinkExtractor(
            restrict_xpaths=('//div[@id="subwrapper"]',),
            restrict_css=("#subwrapper + a",),
        )
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
        ]

    def test_area_tag_with_unicode_present(self):
        body = b"""<html><body>\xbe\xa9<map><area href="http://example.org/foo" /></map></body></html>"""
        response = HtmlResponse("http://example.org", body=body, encoding="utf-8")
        lx = LxmlLinkExtractor()
        lx.extract_links(response)
        lx.extract_links(response)
        lx.extract_links(response)
        assert lx.extract_links(response) == [
            Link(
                url="http://example.org/foo",
                text="",
                fragment="",
                nofollow=False,
            )
        ]

    def test_encoded_url(self):
        body = b"""<html><body><div><a href="?page=2">BinB</a></body></html>"""
        response = HtmlResponse("http://known.fm/AC%2FDC/", body=body, encoding="utf8")
        lx = LxmlLinkExtractor()
        assert lx.extract_links(response) == [
            Link(
                url="http://known.fm/AC%2FDC/?page=2",
                text="BinB",
                fragment="",
                nofollow=False,
            ),
        ]

    def test_encoded_url_in_restricted_xpath(self):
        body = b"""<html><body><div><a href="?page=2">BinB</a></body></html>"""
        response = HtmlResponse("http://known.fm/AC%2FDC/", body=body, encoding="utf8")
        lx = LxmlLinkExtractor(restrict_xpaths="//div")
        assert lx.extract_links(response) == [
            Link(
                url="http://known.fm/AC%2FDC/?page=2",
                text="BinB",
                fragment="",
                nofollow=False,
            ),
        ]

    def test_ignored_extensions(self):
        # jpg is ignored by default
        html = b"""<a href="page.html">asd</a> and <a href="photo.jpg">"""
        response = HtmlResponse("http://example.org/", body=html)
        lx = LxmlLinkExtractor()
        assert lx.extract_links(response) == [
            Link(url="http://example.org/page.html", text="asd"),
        ]

        # override denied extensions
        lx = LxmlLinkExtractor(deny_extensions=["html"])
        assert lx.extract_links(response) == [
            Link(url="http://example.org/photo.jpg"),
        ]

    def test_process_value(self):
        """Test restrict_xpaths with encodings"""
        html = b"""
<a href="javascript:goToPage('../other/page.html','photo','width=600,height=540,scrollbars'); return false">Text</a>
<a href="/about.html">About us</a>
        """
        response = HtmlResponse(
            "http://example.org/somepage/index.html",
            body=html,
            encoding="windows-1252",
        )

        def process_value(value):
            m = re.search(r"javascript:goToPage\('(.*?)'", value)
            return m.group(1) if m else None

        lx = LxmlLinkExtractor(process_value=process_value)
        assert lx.extract_links(response) == [
            Link(url="http://example.org/other/page.html", text="Text")
        ]

    def test_base_url_with_restrict_xpaths(self):
        html = b"""<html><head><title>Page title</title><base href="http://otherdomain.com/base/" /></head>
        <body><p><a href="item/12.html">Item 12</a></p>
        </body></html>"""
        response = HtmlResponse("http://example.org/somepage/index.html", body=html)
        lx = LxmlLinkExtractor(restrict_xpaths="//p")
        assert lx.extract_links(response) == [
            Link(url="http://otherdomain.com/base/item/12.html", text="Item 12")
        ]

    def test_attrs(self):
        lx = LxmlLinkExtractor(attrs="href")
        page4_url = "http://example.com/page%204.html"

        assert lx.extract_links(self.response) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
            Link(
                url="http://example.com/sample3.html#foo",
                text="sample 3 repetition with fragment",
            ),
            Link(url="http://www.google.com/something", text=""),
            Link(url="http://example.com/innertag.html", text="inner tag"),
            Link(url=page4_url, text="href with whitespaces"),
        ]

        lx = LxmlLinkExtractor(
            attrs=("href", "src"), tags=("a", "area", "img"), deny_extensions=()
        )
        assert lx.extract_links(self.response) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample2.jpg", text=""),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
            Link(
                url="http://example.com/sample3.html#foo",
                text="sample 3 repetition with fragment",
            ),
            Link(url="http://www.google.com/something", text=""),
            Link(url="http://example.com/innertag.html", text="inner tag"),
            Link(url=page4_url, text="href with whitespaces"),
        ]

        lx = LxmlLinkExtractor(attrs=None)
        assert lx.extract_links(self.response) == []

    def test_tags(self):
        html = (
            b'<html><area href="sample1.html"></area>'
            b'<a href="sample2.html">sample 2</a><img src="sample2.jpg"/></html>'
        )
        response = HtmlResponse("http://example.com/index.html", body=html)

        lx = LxmlLinkExtractor(tags=None)
        assert lx.extract_links(response) == []

        lx = LxmlLinkExtractor()
        assert lx.extract_links(response) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
        ]

        lx = LxmlLinkExtractor(tags="area")
        assert lx.extract_links(response) == [
            Link(url="http://example.com/sample1.html", text=""),
        ]

        lx = LxmlLinkExtractor(tags="a")
        assert lx.extract_links(response) == [
            Link(url="http://example.com/sample2.html", text="sample 2"),
        ]

        lx = LxmlLinkExtractor(
            tags=("a", "img"), attrs=("href", "src"), deny_extensions=()
        )
        assert lx.extract_links(response) == [
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample2.jpg", text=""),
        ]

    def test_tags_attrs(self):
        html = b"""
        <html><body>
        <div id="item1" data-url="get?id=1"><a href="#">Item 1</a></div>
        <div id="item2" data-url="get?id=2"><a href="#">Item 2</a></div>
        </body></html>
        """
        response = HtmlResponse("http://example.com/index.html", body=html)

        lx = LxmlLinkExtractor(tags="div", attrs="data-url")
        assert lx.extract_links(response) == [
            Link(
                url="http://example.com/get?id=1",
                text="Item 1",
                fragment="",
                nofollow=False,
            ),
            Link(
                url="http://example.com/get?id=2",
                text="Item 2",
                fragment="",
                nofollow=False,
            ),
        ]

        lx = LxmlLinkExtractor(tags=("div",), attrs=("data-url",))
        assert lx.extract_links(response) == [
            Link(
                url="http://example.com/get?id=1",
                text="Item 1",
                fragment="",
                nofollow=False,
            ),
            Link(
                url="http://example.com/get?id=2",
                text="Item 2",
                fragment="",
                nofollow=False,
            ),
        ]

    def test_tags_attrs_wildcard_and_deny(self):
        html = b"""
        <html><body>
        <a href="a.html">a</a>
        <div data-url="div.html">div</div>
        <span href="span.html">span</span>
        <p data-url="p.html">p</p>
        </body></html>
        """
        response = HtmlResponse("http://example.com/index.html", body=html)

        def urls(**kwargs: Any) -> list[str]:
            lx = LxmlLinkExtractor(**kwargs)
            return [link.url for link in lx.extract_links(response)]

        # Default behavior is unchanged: only the listed tags and attributes.
        assert urls() == ["http://example.com/a.html"]

        # "*" as a tag matches every tag.
        assert urls(tags="*") == [
            "http://example.com/a.html",
            "http://example.com/span.html",
        ]

        # "*" as an attribute matches every attribute.
        assert urls(tags="*", attrs="*") == [
            "http://example.com/a.html",
            "http://example.com/div.html",
            "http://example.com/span.html",
            "http://example.com/p.html",
        ]

        # deny_tags excludes tags from the wildcard.
        assert urls(tags="*", attrs="*", deny_tags="div") == [
            "http://example.com/a.html",
            "http://example.com/span.html",
            "http://example.com/p.html",
        ]

        # deny_attrs excludes attributes from the wildcard.
        assert urls(tags="*", attrs="*", deny_attrs="data-url") == [
            "http://example.com/a.html",
            "http://example.com/span.html",
        ]

        # deny_tags also applies when tags are listed explicitly.
        assert urls(tags=("a", "span"), attrs="*", deny_tags="span") == [
            "http://example.com/a.html",
        ]

        # The wildcard for one parameter is independent of the other.
        assert urls(tags="a", attrs="*") == ["http://example.com/a.html"]

    def test_xhtml(self):
        xhtml = b"""
<?xml version="1.0"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN"
    "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">
<head>
    <title>XHTML document title</title>
</head>
<body>
    <div class='links'>
    <p><a href="/about.html">About us</a></p>
    </div>
    <div>
    <p><a href="/follow.html">Follow this link</a></p>
    </div>
    <div>
    <p><a href="/nofollow.html" rel="nofollow">Dont follow this one</a></p>
    </div>
    <div>
    <p><a href="/nofollow2.html" rel="blah">Choose to follow or not</a></p>
    </div>
    <div>
    <p><a href="http://google.com/something" rel="external nofollow">External link not to follow</a></p>
    </div>
</body>
</html>
        """

        response: TextResponse = HtmlResponse(
            "http://example.com/index.xhtml", body=xhtml
        )

        lx = LxmlLinkExtractor()
        assert lx.extract_links(response) == [
            Link(
                url="http://example.com/about.html",
                text="About us",
                fragment="",
                nofollow=False,
            ),
            Link(
                url="http://example.com/follow.html",
                text="Follow this link",
                fragment="",
                nofollow=False,
            ),
            Link(
                url="http://example.com/nofollow.html",
                text="Dont follow this one",
                fragment="",
                nofollow=True,
            ),
            Link(
                url="http://example.com/nofollow2.html",
                text="Choose to follow or not",
                fragment="",
                nofollow=False,
            ),
            Link(
                url="http://google.com/something",
                text="External link not to follow",
                nofollow=True,
            ),
        ]

        response = XmlResponse("http://example.com/index.xhtml", body=xhtml)

        lx = LxmlLinkExtractor()
        assert lx.extract_links(response) == [
            Link(
                url="http://example.com/about.html",
                text="About us",
                fragment="",
                nofollow=False,
            ),
            Link(
                url="http://example.com/follow.html",
                text="Follow this link",
                fragment="",
                nofollow=False,
            ),
            Link(
                url="http://example.com/nofollow.html",
                text="Dont follow this one",
                fragment="",
                nofollow=True,
            ),
            Link(
                url="http://example.com/nofollow2.html",
                text="Choose to follow or not",
                fragment="",
                nofollow=False,
            ),
            Link(
                url="http://google.com/something",
                text="External link not to follow",
                nofollow=True,
            ),
        ]

    def test_link_wrong_href(self):
        html = b"""
        <a href="http://example.org/item1.html">Item 1</a>
        <a href="http://[example.org/item2.html">Item 2</a>
        <a href="http://example.org/item3.html">Item 3</a>
        """
        response = HtmlResponse("http://example.org/index.html", body=html)
        lx = LxmlLinkExtractor()
        assert list(lx.extract_links(response)) == [
            Link(
                url="http://example.org/item1.html",
                text="Item 1",
                nofollow=False,
            ),
            Link(
                url="http://example.org/item3.html",
                text="Item 3",
                nofollow=False,
            ),
        ]

    def test_ftp_links(self):
        body = b"""
        <html><body>
        <div><a href="ftp://www.external.com/">An Item</a></div>
        </body></html>"""
        response = HtmlResponse(
            "http://www.example.com/index.html", body=body, encoding="utf8"
        )
        lx = LxmlLinkExtractor()
        assert lx.extract_links(response) == [
            Link(
                url="ftp://www.external.com/",
                text="An Item",
                fragment="",
                nofollow=False,
            ),
        ]

    def test_pickle_extractor(self):
        lx = LxmlLinkExtractor()
        assert isinstance(pickle.loads(pickle.dumps(lx)), LxmlLinkExtractor)

    def test_link_extractor_aggregation(self):
        """When a parameter like restrict_css is used, the underlying
        implementation calls its internal link extractor once per selector
        matching the specified restrictions, and then aggregates the
        extracted links.

        Test that aggregation respects the unique and canonicalize
        parameters.
        """
        # unique=True (default), canonicalize=False (default)
        lx = LxmlLinkExtractor(restrict_css=("div",))
        response = HtmlResponse(
            "https://example.com",
            body=b"""
                <div>
                    <a href="/a">a1</a>
                    <a href="/b?a=1&b=2">b1</a>
                </div>
                <div>
                    <a href="/a">a2</a>
                    <a href="/b?b=2&a=1">b2</a>
                </div>
            """,
        )
        actual = lx.extract_links(response)
        assert actual == [
            Link(url="https://example.com/a", text="a1"),
            Link(url="https://example.com/b?a=1&b=2", text="b1"),
            Link(url="https://example.com/b?b=2&a=1", text="b2"),
        ]

        # unique=True (default), canonicalize=True
        lx = LxmlLinkExtractor(restrict_css=("div",), canonicalize=True)
        response = HtmlResponse(
            "https://example.com",
            body=b"""
                <div>
                    <a href="/a">a1</a>
                    <a href="/b?a=1&b=2">b1</a>
                </div>
                <div>
                    <a href="/a">a2</a>
                    <a href="/b?b=2&a=1">b2</a>
                </div>
            """,
        )
        actual = lx.extract_links(response)
        assert actual == [
            Link(url="https://example.com/a", text="a1"),
            Link(url="https://example.com/b?a=1&b=2", text="b1"),
        ]

        # unique=False, canonicalize=False (default)
        lx = LxmlLinkExtractor(restrict_css=("div",), unique=False)
        response = HtmlResponse(
            "https://example.com",
            body=b"""
                <div>
                    <a href="/a">a1</a>
                    <a href="/b?a=1&b=2">b1</a>
                </div>
                <div>
                    <a href="/a">a2</a>
                    <a href="/b?b=2&a=1">b2</a>
                </div>
            """,
        )
        actual = lx.extract_links(response)
        assert actual == [
            Link(url="https://example.com/a", text="a1"),
            Link(url="https://example.com/b?a=1&b=2", text="b1"),
            Link(url="https://example.com/a", text="a2"),
            Link(url="https://example.com/b?b=2&a=1", text="b2"),
        ]

        # unique=False, canonicalize=True
        lx = LxmlLinkExtractor(restrict_css=("div",), unique=False, canonicalize=True)
        response = HtmlResponse(
            "https://example.com",
            body=b"""
                <div>
                    <a href="/a">a1</a>
                    <a href="/b?a=1&b=2">b1</a>
                </div>
                <div>
                    <a href="/a">a2</a>
                    <a href="/b?b=2&a=1">b2</a>
                </div>
            """,
        )
        actual = lx.extract_links(response)
        assert actual == [
            Link(url="https://example.com/a", text="a1"),
            Link(url="https://example.com/b?a=1&b=2", text="b1"),
            Link(url="https://example.com/a", text="a2"),
            Link(url="https://example.com/b?a=1&b=2", text="b2"),
        ]

    def test_canonicalize_once_per_link(self, monkeypatch):
        calls = []

        def counting_canonicalize_url(url, *args, **kwargs):
            calls.append(url)
            return canonicalize_url(url, *args, **kwargs)

        monkeypatch.setattr(lxmlhtml, "canonicalize_url", counting_canonicalize_url)
        response = HtmlResponse(
            "https://example.com",
            body=b"".join(b'<a href="/p?b=2&a=1#f%d">x</a>' % i for i in range(10)),
        )
        lx = LxmlLinkExtractor(canonicalize=True)
        assert lx.extract_links(response) == [
            Link(url="https://example.com/p?a=1&b=2", text="x")
        ]
        assert len(calls) == 10

    def test_link_restrict_text(self):
        html = b"""
        <a href="http://example.org/item1.html">Pic of a cat</a>
        <a href="http://example.org/item2.html">Pic of a dog</a>
        <a href="http://example.org/item3.html">Pic of a cow</a>
        """
        response = HtmlResponse("http://example.org/index.html", body=html)
        # Simple text inclusion test
        lx = LxmlLinkExtractor(restrict_text="dog")
        assert list(lx.extract_links(response)) == [
            Link(
                url="http://example.org/item2.html",
                text="Pic of a dog",
                nofollow=False,
            ),
        ]
        # Unique regex test
        lx = LxmlLinkExtractor(restrict_text=r"of.*dog")
        assert list(lx.extract_links(response)) == [
            Link(
                url="http://example.org/item2.html",
                text="Pic of a dog",
                nofollow=False,
            ),
        ]
        # Multiple regex test
        lx = LxmlLinkExtractor(restrict_text=[r"of.*dog", r"of.*cat"])
        assert list(lx.extract_links(response)) == [
            Link(
                url="http://example.org/item1.html",
                text="Pic of a cat",
                nofollow=False,
            ),
            Link(
                url="http://example.org/item2.html",
                text="Pic of a dog",
                nofollow=False,
            ),
        ]

    def test_skip_bad_links(self):
        html = b"""
        <a href="http://example.org:non-port">Why would you do this?</a>
        <a href="http://example.org/item2.html">Good Link</a>
        <a href="http://example.org/item3.html">Good Link 2</a>
        """
        response = HtmlResponse("http://example.org/index.html", body=html)
        lx = LxmlLinkExtractor()
        assert list(lx.extract_links(response)) == [
            Link(
                url="http://example.org/item2.html",
                text="Good Link",
                nofollow=False,
            ),
            Link(
                url="http://example.org/item3.html",
                text="Good Link 2",
                nofollow=False,
            ),
        ]

    def test_link_allowed_is_false_with_empty_url(self):
        bad_link = Link("")
        assert not LxmlLinkExtractor()._link_allowed(bad_link)

    def test_link_allowed_is_false_with_bad_url_prefix(self):
        bad_link = Link("htp://should_be_http.example")
        assert not LxmlLinkExtractor()._link_allowed(bad_link)

    def test_link_allowed_is_false_with_missing_url_prefix(self):
        bad_link = Link("should_have_prefix.example")
        assert not LxmlLinkExtractor()._link_allowed(bad_link)


class TestLxmlParserLinkExtractor:
    def test_extract_links(self):
        html = b'<a href="http://example.com/page.html">Link</a>'
        response = HtmlResponse("http://example.com/", body=html)
        lx = LxmlParserLinkExtractor()
        assert lx.extract_links(response) == [
            Link(url="http://example.com/page.html", text="Link", nofollow=False),
        ]

    def test_deduplicates_by_canonical_url(self):
        # With canonicalized=False, the default, URLs are canonicalized to
        # decide whether two extracted links are the same one.
        html = (
            b'<a href="http://example.com/page.html?b=2&amp;a=1">1</a>'
            b'<a href="http://example.com/page.html?a=1&amp;b=2">2</a>'
        )
        response = HtmlResponse("http://example.com/", body=html)
        lx = LxmlParserLinkExtractor(unique=True)
        assert lx.extract_links(response) == [
            Link(url="http://example.com/page.html?b=2&a=1", text="1", nofollow=False),
        ]

    def test_strip_false(self):
        # With strip=False, trailing whitespace on a relative href survives urljoin
        # and is visible to process_value (safe_url_string cleans it up afterward).
        # Here process_value rejects URLs that still carry trailing whitespace,
        # demonstrating the difference from strip=True.
        def reject_trailing_whitespace(url):
            return None if url != url.rstrip() else url

        html = b'<a href="page.html   ">Link</a>'
        response = HtmlResponse("http://example.com/", body=html)

        lx_strip = LxmlParserLinkExtractor(
            strip=True, process=reject_trailing_whitespace
        )
        assert lx_strip.extract_links(response) == [
            Link(url="http://example.com/page.html", text="Link", nofollow=False),
        ]

        lx_no_strip = LxmlParserLinkExtractor(
            strip=False, process=reject_trailing_whitespace
        )
        assert lx_no_strip.extract_links(response) == []


def _extract_srcset_links(html: bytes, **kwargs: Any) -> list[Link]:
    response = HtmlResponse("http://example.com/index", body=html)
    lx = LxmlLinkExtractor(
        tags=["img", "source", "link"],
        attrs=["srcset", "imagesrcset"],
        deny_extensions=[],
        **kwargs,
    )
    return lx.extract_links(response)


class TestLxmlLinkExtractorSrcset:
    def test_candidates_keep_order_descriptors_and_element(self):
        html = b'<img srcset="small.jpg 480w, large.jpg 2x">'
        links = _extract_srcset_links(html)
        assert [(link.url, link.descriptor, link.position) for link in links] == [
            ("http://example.com/small.jpg", "480w", 0),
            ("http://example.com/large.jpg", "2x", 1),
        ]
        assert all(link.element is not None for link in links)
        tags = [link.element.tag for link in links if link.element is not None]
        assert tags == ["img", "img"]

    def test_whitespace_and_commas(self):
        html = b'<img srcset=" ,\n a.jpg 1x ,\t b.jpg 2x, ,">'
        links = _extract_srcset_links(html)
        assert [(link.url, link.descriptor) for link in links] == [
            ("http://example.com/a.jpg", "1x"),
            ("http://example.com/b.jpg", "2x"),
        ]

    def test_comma_right_after_url(self):
        html = b'<img srcset="a.jpg, b.jpg">'
        links = _extract_srcset_links(html)
        assert [(link.url, link.descriptor) for link in links] == [
            ("http://example.com/a.jpg", ""),
            ("http://example.com/b.jpg", ""),
        ]

    def test_data_url_commas_do_not_split(self):
        html = b'<img srcset="data:image/png;base64,iVBORw0KG,go= 1x, b.jpg 2x">'
        response = HtmlResponse("http://example.com/index", body=html)
        lx = LxmlParserLinkExtractor(tag="img", attr="srcset")
        assert [link.url for link in lx.extract_links(response)] == [
            "data:image/png;base64,iVBORw0KG,go=",
            "http://example.com/b.jpg",
        ]
        # LxmlLinkExtractor drops the data: URL as an unsupported scheme,
        # and no bogus URL is built from the base64 payload.
        links = _extract_srcset_links(html)
        assert [link.url for link in links] == ["http://example.com/b.jpg"]

    @pytest.mark.parametrize(
        "descriptor",
        [
            "2y",
            "0w",
            "100w 2x",
            "2x 100w",
            "100h",
            "100h 200h",
            "2x 100h",
            "1x 2x",
            "1.5.2x",
            "foo",
            "100ww",
            "(1x)",
        ],
    )
    def test_invalid_descriptor_drops_only_that_candidate(self, descriptor):
        html = f'<img srcset="a.jpg {descriptor}, b.jpg 2x">'.encode()
        links = _extract_srcset_links(html)
        assert [(link.url, link.descriptor) for link in links] == [
            ("http://example.com/b.jpg", "2x"),
        ]

    @pytest.mark.parametrize(
        "descriptor", ["", "100w", "2x", "1.5x", "1.5e1x", "100w 100h"]
    )
    def test_valid_descriptors(self, descriptor):
        html = f'<img srcset="a.jpg {descriptor}">'.encode()
        links = _extract_srcset_links(html)
        assert [(link.url, link.descriptor) for link in links] == [
            ("http://example.com/a.jpg", descriptor),
        ]

    def test_imagesrcset(self):
        html = (
            b'<link rel="preload" as="image"'
            b' imagesrcset="hero-800.jpg 800w, hero-1600.jpg 1600w">'
        )
        links = _extract_srcset_links(html)
        assert [(link.url, link.descriptor, link.position) for link in links] == [
            ("http://example.com/hero-800.jpg", "800w", 0),
            ("http://example.com/hero-1600.jpg", "1600w", 1),
        ]
        tags = [link.element.tag for link in links if link.element is not None]
        assert tags == ["link", "link"]

    def test_picture_source(self):
        html = (
            b"<picture>"
            b'<source srcset="a.webp 1x, a@2x.webp 2x">'
            b'<img src="a.jpg">'
            b"</picture>"
        )
        links = _extract_srcset_links(html)
        assert [(link.url, link.descriptor) for link in links] == [
            ("http://example.com/a.webp", "1x"),
            ("http://example.com/a@2x.webp", "2x"),
        ]
        tags = [link.element.tag for link in links if link.element is not None]
        assert tags == ["source", "source"]

    def test_entities(self):
        html = b'<img srcset="a.jpg?x=1&amp;y=2 1x, b.jpg 2x">'
        links = _extract_srcset_links(html)
        assert [link.url for link in links] == [
            "http://example.com/a.jpg?x=1&y=2",
            "http://example.com/b.jpg",
        ]

    def test_non_utf8_page(self):
        html = '<img srcset="\u56fe\u7247.jpg 1x, b.jpg 2x">'.encode("gb18030")
        response = HtmlResponse(
            "http://example.com/index", body=html, encoding="gb18030"
        )
        lx = LxmlLinkExtractor(tags=["img"], attrs=["srcset"], deny_extensions=[])
        links = lx.extract_links(response)
        assert [link.url for link in links] == [
            "http://example.com/%E5%9B%BE%E7%89%87.jpg",
            "http://example.com/b.jpg",
        ]

    def test_base_url(self):
        html = (
            b'<base href="http://cdn.example.com/media/">'
            b'<img srcset="a.jpg 1x, b.jpg 2x">'
        )
        links = _extract_srcset_links(html)
        assert [link.url for link in links] == [
            "http://cdn.example.com/media/a.jpg",
            "http://cdn.example.com/media/b.jpg",
        ]

    def test_process_value(self):
        html = b'<img srcset="a.jpg 1x, b.jpg 2x">'
        links = _extract_srcset_links(
            html, process_value=lambda value: value + "?from=srcset"
        )
        assert [link.url for link in links] == [
            "http://example.com/a.jpg?from=srcset",
            "http://example.com/b.jpg?from=srcset",
        ]
        links = _extract_srcset_links(
            html, process_value=lambda value: None if "a.jpg" in value else value
        )
        assert [link.url for link in links] == ["http://example.com/b.jpg"]

    def test_allow_deny(self):
        html = b'<img srcset="a.jpg 1x, b.jpg 2x, c.png 1x">'
        links = _extract_srcset_links(html, allow=(r"a\.jpg", r"c\.png"))
        assert [link.url for link in links] == [
            "http://example.com/a.jpg",
            "http://example.com/c.png",
        ]
        links = _extract_srcset_links(html, deny=(r"b\.jpg",))
        assert [link.url for link in links] == [
            "http://example.com/a.jpg",
            "http://example.com/c.png",
        ]

    def test_deny_domains(self):
        html = b'<img srcset="http://cdn.example.com/a.jpg 1x, /b.jpg 2x">'
        links = _extract_srcset_links(html, deny_domains=("cdn.example.com",))
        assert [link.url for link in links] == ["http://example.com/b.jpg"]

    def test_deny_extensions_default(self):
        html = b'<img srcset="a.jpg 1x, b.html 2x">'
        response = HtmlResponse("http://example.com/index", body=html)
        lx = LxmlLinkExtractor(tags=["img"], attrs=["srcset"])
        assert [link.url for link in lx.extract_links(response)] == [
            "http://example.com/b.html",
        ]

    def test_restrict_xpaths(self):
        html = (
            b'<div class="gallery"><img srcset="a.jpg 1x"></div>'
            b'<div><img srcset="b.jpg 1x"></div>'
        )
        links = _extract_srcset_links(html, restrict_xpaths="//div[@class='gallery']")
        assert [link.url for link in links] == ["http://example.com/a.jpg"]

    def test_restrict_css(self):
        html = (
            b'<div class="gallery"><img srcset="a.jpg 1x"></div>'
            b'<div><img srcset="b.jpg 1x"></div>'
        )
        links = _extract_srcset_links(html, restrict_css="div.gallery")
        assert [link.url for link in links] == ["http://example.com/a.jpg"]

    def test_deduplication_keeps_first_candidate(self):
        html = b'<img srcset="a.jpg 1x, a.jpg 2x, b.jpg 1x">'
        links = _extract_srcset_links(html)
        assert [(link.url, link.descriptor, link.position) for link in links] == [
            ("http://example.com/a.jpg", "1x", 0),
            ("http://example.com/b.jpg", "1x", 2),
        ]
        links = _extract_srcset_links(html, unique=False)
        assert [(link.url, link.descriptor) for link in links] == [
            ("http://example.com/a.jpg", "1x"),
            ("http://example.com/a.jpg", "2x"),
            ("http://example.com/b.jpg", "1x"),
        ]

    def test_plain_links_have_no_candidate_data(self):
        html = b'<a href="page.html">x</a>'
        response = HtmlResponse("http://example.com/index", body=html)
        (link,) = LxmlLinkExtractor().extract_links(response)
        assert link.element is None
        assert link.descriptor == ""
        assert link.position == 0


class SrcsetSpider(Spider):
    """Collect image candidates from srcset attributes and request them."""

    name = "srcset"

    def __init__(self, *args: Any, mockserver: MockServer, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.start_urls = [mockserver.url("/static/srcset.html")]
        self.link_extractor = LxmlLinkExtractor(
            tags=["img"], attrs=["srcset"], deny_extensions=[]
        )
        self.extracted: list[str] = []
        self.requested: list[str] = []

    def parse(self, response):
        for link in self.link_extractor.extract_links(response):
            self.extracted.append(link.url)
            yield Request(link.url, callback=self.parse_image)

    def parse_image(self, response):
        self.requested.append(response.url)


class TestSrcsetSpider:
    @coroutine_test
    async def test_crawl(self, mockserver: MockServer):
        crawler = get_crawler(SrcsetSpider)
        await crawler.crawl_async(mockserver=mockserver)
        assert isinstance(crawler.spider, SrcsetSpider)
        # Every valid candidate URL is requested, while the commas of the
        # data: URL in the page do not turn its payload into HTTP requests.
        expected = [
            mockserver.url("/static/item1.html"),
            mockserver.url("/static/item2.html"),
            mockserver.url("/static/index.html"),
        ]
        assert crawler.spider.extracted == expected
        assert sorted(crawler.spider.requested) == sorted(expected)
