.. _topics-link-extractors:

===============
Link Extractors
===============

A link extractor is an object that extracts links from responses.

The ``__init__`` method of
:class:`~scrapy.linkextractors.lxmlhtml.LxmlLinkExtractor` takes settings that
determine which links may be extracted. :class:`LxmlLinkExtractor.extract_links
<scrapy.linkextractors.lxmlhtml.LxmlLinkExtractor.extract_links>` returns a
list of matching :class:`~scrapy.link.Link` objects from a
:class:`~scrapy.http.Response` object.

Link extractors are used in :class:`~scrapy.spiders.CrawlSpider` spiders
through a set of :class:`~scrapy.spiders.Rule` objects.

You can also use link extractors in regular spiders. For example, you can
instantiate :class:`LinkExtractor
<scrapy.linkextractors.lxmlhtml.LxmlLinkExtractor>` as a class variable in your
spider and use it from your spider callbacks:

.. code-block:: python

    def parse(self, response):
        for link in self.link_extractor.extract_links(response):
            yield Request(link.url, callback=self.parse)

.. _srcset:

srcset attributes
=================

When the scanned attributes (see the *attrs* parameter of
:class:`~scrapy.linkextractors.lxmlhtml.LxmlLinkExtractor`) include
``srcset`` or ``imagesrcset``, their value is not treated as a single
URL. It is parsed into image candidate URLs following the HTML standard,
and every candidate with a valid descriptor becomes a separate
:class:`~scrapy.link.Link` object. For those links, the ``element``,
``descriptor`` and ``position`` attributes of :class:`~scrapy.link.Link`
identify the element and the candidate within its attribute that the
link comes from.

Candidate URLs go through the same processing and filtering as any other
link, including *process_value*, *allow*, *deny*, *allow_domains*,
*deny_domains*, *deny_extensions* and deduplication. Deduplication keeps
the first candidate of a URL, including its candidate attributes. Keep
in mind that the default value of *deny_extensions* ignores the most
common image file extensions, so you need to set it to an empty list to
collect image URLs:

.. code-block:: python

    from scrapy.linkextractors import LinkExtractor

    extractor = LinkExtractor(
        tags=["img", "source"],
        attrs=["srcset"],
        deny_extensions=[],
    )

Links whose URL scheme is not supported, such as ``data:`` URLs, are
dropped by filtering, so inline images are never turned into HTTP
requests.

.. _topics-link-extractors-ref:

Link extractor reference
========================

The link extractor class is
:class:`scrapy.linkextractors.lxmlhtml.LxmlLinkExtractor`. For convenience it
can also be imported as ``scrapy.linkextractors.LinkExtractor``:

.. code-block:: python

    from scrapy.linkextractors import LinkExtractor

LxmlLinkExtractor
-----------------

.. autoclass:: scrapy.linkextractors.lxmlhtml.LxmlLinkExtractor

    .. automethod:: extract_links

Link
----

.. autoclass:: scrapy.link.Link
