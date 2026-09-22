from __future__ import annotations

import pickle
import re
from typing import TYPE_CHECKING, Any

import pytest
from w3lib.url import canonicalize_url

from scrapy.http import HtmlResponse, Request, TextResponse, XmlResponse
from scrapy.link import Link
from scrapy.linkextractors import lxmlhtml
from scrapy.linkextractors.lxmlhtml import LxmlLinkExtractor, LxmlParserLinkExtractor
from scrapy.utils.test import get_crawler
from tests import get_testdata
from tests.spiders import RawResponseSpider
from tests.utils.decorators import inline_callbacks_test

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


class TestSrcsetLinkExtractor:
    def _extract(self, html: bytes, **kwargs: Any) -> list[Link]:
        kwargs.setdefault("tags", ("img", "source", "link"))
        kwargs.setdefault("attrs", ("src", "srcset", "imagesrcset"))
        kwargs.setdefault("deny_extensions", ())
        response = HtmlResponse("http://example.com/index.html", body=html)
        return LxmlLinkExtractor(**kwargs).extract_links(response)

    @staticmethod
    def _srcset_info(link: Link) -> tuple[str, int, tuple[str, ...]]:
        assert link.srcset is not None
        return (
            link.srcset.element.tag,
            link.srcset.position,
            link.srcset.descriptors,
        )

    def test_img_srcset(self):
        links = self._extract(
            b'<img src="a.jpg" srcset="a-small.jpg 480w, a-large.jpg 2x">'
        )
        assert [link.url for link in links] == [
            "http://example.com/a.jpg",
            "http://example.com/a-small.jpg",
            "http://example.com/a-large.jpg",
        ]
        # src is a plain link, srcset candidates carry their element,
        # position and descriptors.
        assert links[0].srcset is None
        assert self._srcset_info(links[1]) == ("img", 0, ("480w",))
        assert self._srcset_info(links[2]) == ("img", 1, ("2x",))

    def test_picture_source(self):
        links = self._extract(
            b"<picture>"
            b'<source srcset="wide.jpg 800w, narrow.jpg 400w">'
            b'<img src="fallback.jpg">'
            b"</picture>"
        )
        assert [link.url for link in links] == [
            "http://example.com/wide.jpg",
            "http://example.com/narrow.jpg",
            "http://example.com/fallback.jpg",
        ]
        assert self._srcset_info(links[0]) == ("source", 0, ("800w",))
        assert self._srcset_info(links[1]) == ("source", 1, ("400w",))
        assert links[2].srcset is None

    def test_link_imagesrcset(self):
        links = self._extract(
            b'<link rel="preload" as="image" '
            b'imagesrcset="hero-1x.jpg 1x, hero-2x.jpg 2x">'
        )
        assert [link.url for link in links] == [
            "http://example.com/hero-1x.jpg",
            "http://example.com/hero-2x.jpg",
        ]
        assert self._srcset_info(links[0]) == ("link", 0, ("1x",))
        assert self._srcset_info(links[1]) == ("link", 1, ("2x",))

    def test_data_url_commas_not_split(self):
        seen = []

        def process_value(value):
            seen.append(value)
            return value

        links = self._extract(
            b'<img srcset="data:image/png;base64,iVBORw0KGgo= 1x, '
            b"data:text/plain,hello,world 2x, "
            b'a.jpg 3x">',
            process_value=process_value,
        )
        # The commas inside the data URLs do not split them into bogus
        # candidates; the data URLs are then dropped by the URL scheme
        # filtering, like data: URLs in any other attribute.
        assert seen == [
            "data:image/png;base64,iVBORw0KGgo=",
            "data:text/plain,hello,world",
            "http://example.com/a.jpg",
        ]
        assert [link.url for link in links] == ["http://example.com/a.jpg"]

    @pytest.mark.parametrize(
        ("descriptor", "expected"),
        [
            ("1x", ("1x",)),
            ("1.5x", ("1.5x",)),
            (".5x", (".5x",)),
            ("480w", ("480w",)),
            ("100w 100h", ("100w", "100h")),
        ],
    )
    def test_valid_descriptors(self, descriptor, expected):
        links = self._extract(f'<img srcset="a.jpg {descriptor}">'.encode())
        assert [link.url for link in links] == ["http://example.com/a.jpg"]
        assert self._srcset_info(links[0]) == ("img", 0, expected)

    @pytest.mark.parametrize(
        "descriptor",
        [
            "1q",
            "0w",
            "0x",
            "-1x",
            "w",
            "x",
            "1 x",
            "1.5.2x",
            "1x 1x",
            "100w 100w",
            "100w 2x",
            "100h",
            "2x 100w",
            "100w 100h 100h",
        ],
    )
    def test_invalid_descriptors_drop_only_their_candidate(self, descriptor):
        links = self._extract(f'<img srcset="bad.jpg {descriptor}, good.jpg">'.encode())
        assert [link.url for link in links] == ["http://example.com/good.jpg"]

    def test_whitespace_and_comma_placement(self):
        links = self._extract(b'<img srcset="  a.jpg 1x ,\n\t b.jpg,, ,c.jpg 2x,">')
        assert [link.url for link in links] == [
            "http://example.com/a.jpg",
            "http://example.com/b.jpg",
            "http://example.com/c.jpg",
        ]

    def test_empty_srcset(self):
        assert self._extract(b'<img srcset="">') == []
        assert self._extract(b'<img srcset="  , ,  ">') == []

    def test_character_entities(self):
        links = self._extract(
            b'<img srcset="a.jpg?x=1&amp;y=2 1x, b.jpg?z=3&amp;w=4 2x">'
        )
        assert [link.url for link in links] == [
            "http://example.com/a.jpg?x=1&y=2",
            "http://example.com/b.jpg?z=3&w=4",
        ]

    def test_non_utf8_encoding(self):
        body = (
            '<a href="r\xe9sum\xe9.html">a</a>'
            '<img srcset="r\xe9sum\xe9-1x.jpg 1x, r\xe9sum\xe9-2x.jpg 2x">'
        ).encode("latin-1")
        response = HtmlResponse("http://example.com/", body=body, encoding="latin-1")
        lx = LxmlLinkExtractor(
            tags=("a", "img"), attrs=("href", "srcset"), deny_extensions=()
        )
        # srcset candidates are encoded like URLs from any other attribute.
        assert [link.url for link in lx.extract_links(response)] == [
            "http://example.com/r%C3%A9sum%C3%A9.html",
            "http://example.com/r%C3%A9sum%C3%A9-1x.jpg",
            "http://example.com/r%C3%A9sum%C3%A9-2x.jpg",
        ]

    def test_base_url(self):
        links = self._extract(
            b'<base href="http://cdn.example/"><img srcset="a.jpg 1x, b.jpg 2x">'
        )
        assert [link.url for link in links] == [
            "http://cdn.example/a.jpg",
            "http://cdn.example/b.jpg",
        ]

    def test_duplicate_candidates_keep_first(self):
        # With unique=True (the default), a URL repeated at different
        # densities yields a single link with the first candidate's info.
        links = self._extract(b'<img srcset="a.jpg 1x, a.jpg 2x">')
        assert len(links) == 1
        assert self._srcset_info(links[0]) == ("img", 0, ("1x",))

    def test_duplicate_candidates_unique_false(self):
        links = self._extract(b'<img srcset="a.jpg 1x, a.jpg 2x">', unique=False)
        assert [link.url for link in links] == [
            "http://example.com/a.jpg",
            "http://example.com/a.jpg",
        ]
        assert self._srcset_info(links[0]) == ("img", 0, ("1x",))
        assert self._srcset_info(links[1]) == ("img", 1, ("2x",))

    def test_process_value_applies_per_candidate(self):
        def process_value(value):
            return None if value.endswith("b.jpg") else value

        links = self._extract(
            b'<img src="a.jpg" srcset="b.jpg 1x, c.jpg 2x">',
            process_value=process_value,
        )
        assert [link.url for link in links] == [
            "http://example.com/a.jpg",
            "http://example.com/c.jpg",
        ]

    def test_allow_deny(self):
        html = b'<img srcset="keep-this.jpg 1x, drop-this.jpg 2x, other.png 3x">'
        links = self._extract(html, allow=("keep",))
        assert [link.url for link in links] == ["http://example.com/keep-this.jpg"]
        links = self._extract(html, deny=("drop|other",))
        assert [link.url for link in links] == ["http://example.com/keep-this.jpg"]

    def test_deny_domains(self):
        html = b'<img srcset="http://a.example/a.jpg 1x, http://b.example/b.jpg 2x">'
        links = self._extract(html, deny_domains=("b.example",))
        assert [link.url for link in links] == ["http://a.example/a.jpg"]

    def test_restrict_xpaths(self):
        html = (
            b'<div><img srcset="in-1x.jpg 1x, in-2x.jpg 2x"></div>'
            b'<img srcset="out.jpg 1x">'
        )
        links = self._extract(html, restrict_xpaths="//div")
        assert [link.url for link in links] == [
            "http://example.com/in-1x.jpg",
            "http://example.com/in-2x.jpg",
        ]

    def test_restrict_css(self):
        html = (
            b'<div><img srcset="in-1x.jpg 1x, in-2x.jpg 2x"></div>'
            b'<img srcset="out.jpg 1x">'
        )
        links = self._extract(html, restrict_css="div")
        assert [link.url for link in links] == [
            "http://example.com/in-1x.jpg",
            "http://example.com/in-2x.jpg",
        ]

    def test_wildcard_and_deny_attrs(self):
        html = b'<img src="a.jpg" srcset="b.jpg 1x, c.jpg 2x">'
        links = self._extract(html, tags="*", attrs="*")
        assert [link.url for link in links] == [
            "http://example.com/a.jpg",
            "http://example.com/b.jpg",
            "http://example.com/c.jpg",
        ]
        links = self._extract(html, tags="*", attrs="*", deny_attrs="srcset")
        assert [link.url for link in links] == ["http://example.com/a.jpg"]


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

    def test_srcset(self):
        html = (
            b'<img srcset="http://example.com/a.jpg 1x, http://example.com/b.jpg 2x">'
        )
        response = HtmlResponse("http://example.com/", body=html)
        lx = LxmlParserLinkExtractor(tag="img", attr="srcset")
        links = lx.extract_links(response)
        assert [link.url for link in links] == [
            "http://example.com/a.jpg",
            "http://example.com/b.jpg",
        ]
        for link, position, descriptors in zip(
            links, [0, 1], [("1x",), ("2x",)], strict=True
        ):
            assert link.srcset is not None
            assert link.srcset.position == position
            assert link.srcset.descriptors == descriptors


class SrcsetSpider(RawResponseSpider):
    """Requests every link extracted from a srcset attribute."""

    name = "srcset"
    content_type = "text/html"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.visited = []

    def raw_body(self):
        assert self.mockserver
        return (
            "<html><body>"
            '<img srcset="'
            f"{self.mockserver.url('/static/files/images/scrapy.png')} 1x, "
            f"{self.mockserver.url('/static/files/images/python-powered-h-50x65.png')} 2x, "
            "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg== 3x"
            '">'
            "</body></html>"
        )

    async def start(self):
        yield Request(self.raw_url)

    def parse(self, response):
        extractor = LxmlLinkExtractor(
            tags=("img",), attrs=("srcset",), deny_extensions=()
        )
        for link in extractor.extract_links(response):
            yield Request(link.url, callback=self.collect)

    def collect(self, response):
        self.visited.append(response.url)


@inline_callbacks_test
def test_srcset_spider(mockserver: MockServer):
    """Every valid srcset candidate is requested, while the commas of the
    data URL candidate do not leak into bogus HTTP requests."""
    crawler = get_crawler(SrcsetSpider)
    yield crawler.crawl(mockserver=mockserver)
    assert isinstance(crawler.spider, SrcsetSpider)
    assert sorted(crawler.spider.visited) == [
        mockserver.url("/static/files/images/python-powered-h-50x65.png"),
        mockserver.url("/static/files/images/scrapy.png"),
    ]
    # Only the page and the two images were requested: the data URL candidate
    # was not split at its commas into additional HTTP requests.
    assert crawler.stats.get_value("downloader/request_count") == 3
