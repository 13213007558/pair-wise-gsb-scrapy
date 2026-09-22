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

.. versionadded:: VERSION

When the *attrs* setting of a link extractor includes ``srcset`` or
``imagesrcset``, the value of those attributes is parsed following the
`parse a srcset attribute
<https://html.spec.whatwg.org/multipage/images.html#parse-a-srcset-attribute>`_
algorithm of the HTML standard, and every candidate yields its own
:class:`~scrapy.link.Link` object. This allows collecting every candidate
image URL, e.g. for offline media download, instead of picking one based on
the viewport or the screen density.

Commas inside a candidate URL, e.g. in a data URL, do not split it. A
candidate with invalid descriptors, e.g. ``1q``, is dropped without
affecting the other candidates.

As with any other attribute, only candidates with a supported URL scheme
(``http``, ``https``, ``file``, ``ftp``) become links, so candidates with a
data URL are skipped.

For a link extracted from a srcset attribute, the ``srcset`` attribute of
the :class:`~scrapy.link.Link` object is a
:class:`~scrapy.link.SrcsetCandidate` object with the source element, the
0-based position of the candidate in the attribute value and the candidate
descriptors. For links extracted from other attributes it is ``None``.

When the same URL appears in multiple candidates, e.g. at different pixel
densities, the default duplicate filtering (*unique*) keeps the first
candidate and its descriptors.

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

.. autoclass:: scrapy.link.SrcsetCandidate
    :members:
