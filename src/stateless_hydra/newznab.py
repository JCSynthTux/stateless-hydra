"""Newznab/Torznab protocol vocabulary.

This module is deliberately pure: it contains no network, Redis, or filesystem
access. It provides the shared protocol layer used in both directions of the
proxy:

* rendering Newznab/Torznab XML (and JSON) that clients such as Sonarr/Radarr
  consume -- capability documents, result feeds, and error documents;
* parsing Newznab/Torznab RSS returned by the upstream Usenet indexers.

Everything here operates on plain strings and in-memory models so it can be
unit-tested without any external service.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lxml import etree
from pydantic import BaseModel, Field

from . import __version__
from .exceptions import StatelessHydraError

# Newznab error codes, as defined by the Newznab API specification and listed
# in AGENTS.md rule 5.
ERROR_CODES: dict[int, str] = {
    100: "Incorrect user credentials",
    200: "Missing parameter",
    201: "Incorrect parameter",
    202: "No such function",
    203: "Function not available",
    300: "No such item",
    900: "Unknown error",
    910: "API hit limit reached",
    920: "API disabled",
    930: "Download limit reached",
    940: "Download disabled",
}

# Standard Newznab/Torznab category table: ``id -> name``. An id divisible by
# 1000 is a parent category; every other id is a subcategory of the parent with
# the same thousands prefix. Names for subcategories include their parent path
# so a rendered feed is self-describing.
CATEGORIES: dict[int, str] = {
    1000: "Console",
    1010: "Console/NDS",
    1020: "Console/PSP",
    1030: "Console/Wii",
    1040: "Console/Xbox",
    1050: "Console/Xbox 360",
    1060: "Console/WiiU",
    1070: "Console/Xbox One",
    1080: "Console/PS3",
    1090: "Console/PS4",
    2000: "Movies",
    2010: "Movies/Foreign",
    2020: "Movies/Other",
    2030: "Movies/SD",
    2040: "Movies/HD",
    2045: "Movies/UHD",
    2050: "Movies/BluRay",
    2060: "Movies/3D",
    2070: "Movies/DVD",
    2080: "Movies/WEB-DL",
    3000: "Audio",
    3010: "Audio/MP3",
    3020: "Audio/Video",
    3030: "Audio/Audiobook",
    3040: "Audio/Lossless",
    3050: "Audio/Other",
    4000: "PC",
    4010: "PC/0day",
    4020: "PC/ISO",
    4030: "PC/Mac",
    4040: "PC/Mobile-iOS",
    4050: "PC/Mobile-Android",
    5000: "TV",
    5010: "TV/WEB-DL",
    5020: "TV/Foreign",
    5030: "TV/SD",
    5040: "TV/HD",
    5050: "TV/Other",
    5060: "TV/Sport",
    5070: "TV/Anime",
    5080: "TV/Documentary",
    6000: "XXX",
    6010: "XXX/DVD",
    6020: "XXX/WMV",
    6030: "XXX/XviD",
    6040: "XXX/x264",
    6050: "XXX/UHD",
    6060: "XXX/Pack",
    7000: "Other",
    7010: "Other/Misc",
    7020: "Other/E-Book",
    7030: "Other/Comics",
    7040: "Other/Audiobook",
    7050: "Other/Mobile",
    7060: "Other/Educational",
}

_NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"
_TORZNAB_NS = "http://torznab.com/schemas/2015/feed"
_ATOM_NS = "http://www.w3.org/2005/Atom"

# Fixed server metadata advertised in the ``caps`` document. ``appversion``
# tracks the package version so there is a single source of truth.
_SERVER_INFO: dict[str, str] = {
    "appversion": __version__,
    "version": "2.0",
    "title": "stateless-hydra",
    "strapline": "stateless-hydra",
}

# Newznab search function -> (rendered element name, supported params). The
# dict order defines the order of the ``<searching>`` children.
_SEARCH_FUNCTIONS: dict[str, tuple[str, str]] = {
    "search": ("search", "q"),
    "tvsearch": ("tv-search", "q,season,ep"),
    "movie": ("movie-search", "q,imdbid,tmdbid"),
    "music": ("music-search", "q,album,artist,label,track,year,genre"),
    "book": ("book-search", "q,title,author"),
}


class NewznabError(StatelessHydraError):
    """An error expressible as a Newznab ``<error code=... description=.../>``.

    ``description`` defaults to the canonical text for ``code``. An unknown code
    is normalized to ``900`` ("Unknown error").
    """

    def __init__(self, code: int, description: str | None = None) -> None:
        if code not in ERROR_CODES:
            code = 900
        if description is None:
            description = ERROR_CODES[code]
        self.code = code
        self.description = description
        super().__init__(f"{self.code}: {self.description}")


class ResultItem(BaseModel):
    """One aggregated search result, in Newznab/Torznab terms.

    ``pub_date`` is the RFC 2822 string exactly as received from (or rendered
    for) the indexer. ``category`` is a single category id string such as
    ``"5040"``. ``attributes`` holds torznab/newznab ``attr`` name/value pairs
    (``seeders``, ``imdb``, ...).
    """

    title: str
    guid: str
    link: str
    pub_date: str
    category: str
    size: int | None = None
    description: str | None = None
    # Download URL advertised as an RSS ``<enclosure>``. Newznab clients
    # (AIOStreams among them) fetch the NZB from this URL, and drop items that
    # lack an ``application/x-nzb`` enclosure. ``None`` means no enclosure.
    enclosure_url: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class ParsedRss:
    """Result of parsing an indexer RSS feed.

    ``total``/``offset`` come from the channel-level ``newznab:response``
    element and are ``None`` when that element (or attribute) is absent.
    """

    items: list[ResultItem]
    total: int | None = None
    offset: int | None = None


def _local_name(tag: Any) -> str:
    """Return the local part of a possibly namespace-qualified tag."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _qname(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def _text(element: etree._Element | None) -> str | None:
    return element.text if element is not None else None


def _child(element: etree._Element, name: str) -> etree._Element | None:
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def render_error(code: int, description: str | None = None) -> str:
    """Render a Newznab error document (newline-terminated).

    Behaves like :class:`NewznabError`: an unknown ``code`` is normalized to
    ``900``, and ``description`` defaults to :data:`ERROR_CODES` for the
    (possibly normalized) code. A caller-supplied description is kept as-is.
    """
    if code not in ERROR_CODES:
        code = 900
    if description is None:
        description = ERROR_CODES[code]
    element = etree.Element("error", code=str(code), description=description)
    return etree.tostring(element, encoding="unicode") + "\n"


def _category_tree() -> list[tuple[int, str, list[tuple[int, str]]]]:
    """Group :data:`CATEGORIES` into ``(parent_id, parent_name, subs)`` triples."""
    parents: list[tuple[int, str, list[tuple[int, str]]]] = []
    for parent_id in sorted(cid for cid in CATEGORIES if cid % 1000 == 0):
        subs = [
            (cid, CATEGORIES[cid])
            for cid in sorted(CATEGORIES)
            if cid != parent_id and cid // 1000 == parent_id // 1000
        ]
        parents.append((parent_id, CATEGORIES[parent_id], subs))
    return parents


def _categories_json() -> list[dict[str, Any]]:
    return [
        {
            "id": str(parent_id),
            "name": parent_name,
            "subcat": [{"id": str(sub_id), "name": sub_name} for sub_id, sub_name in subs],
        }
        for parent_id, parent_name, subs in _category_tree()
    ]


def render_caps(search_types: Collection[str], o: str = "xml") -> str | dict:
    """Render the Newznab ``caps`` document.

    Only search functions present in ``search_types`` are advertised. The
    accepted vocabulary is exactly the Newznab function names ``"search"``,
    ``"tvsearch"``, ``"movie"``, ``"music"`` and ``"book"``; any other value
    raises :class:`ValueError` listing the valid names.

    XML output is a ``<caps>`` document. When ``o == "json"`` a JSON-serializable
    dict with the following exact shape is returned::

        {
            "caps": {
                "server": {
                    "appversion": "0.1.0",
                    "version": "2.0",
                    "title": "stateless-hydra",
                    "strapline": "stateless-hydra",
                },
                "searching": {
                    "search": {"available": "yes", "supportedParams": "q"},
                    "tv-search": {...},
                    ...
                },
                "categories": [
                    {"id": "1000", "name": "Console",
                     "subcat": [{"id": "1010", "name": "Console/NDS"}, ...]},
                    ...
                ],
            }
        }

    Raises:
        ValueError: if ``search_types`` contains an unknown search function.
    """
    unknown = sorted(name for name in search_types if name not in _SEARCH_FUNCTIONS)
    if unknown:
        valid = ", ".join(repr(name) for name in _SEARCH_FUNCTIONS)
        raise ValueError(f"unknown search type(s) {unknown}; valid search types are: {valid}")

    searching = [
        (tag, supported)
        for function, (tag, supported) in _SEARCH_FUNCTIONS.items()
        if function in search_types
    ]

    if o == "json":
        return {
            "caps": {
                "server": dict(_SERVER_INFO),
                "searching": {
                    tag: {"available": "yes", "supportedParams": supported}
                    for tag, supported in searching
                },
                "categories": _categories_json(),
            }
        }

    root = etree.Element("caps")
    etree.SubElement(root, "server", **_SERVER_INFO)
    searching_el = etree.SubElement(root, "searching")
    for tag, supported in searching:
        etree.SubElement(searching_el, tag, available="yes", supportedParams=supported)

    categories_el = etree.SubElement(root, "categories")
    for parent_id, parent_name, subs in _category_tree():
        parent_el = etree.SubElement(categories_el, "category", id=str(parent_id), name=parent_name)
        for sub_id, sub_name in subs:
            etree.SubElement(parent_el, "subcat", id=str(sub_id), name=sub_name)

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8").decode("UTF-8")


def _item_to_json(item: ResultItem) -> dict[str, Any]:
    return {
        "title": item.title,
        "guid": item.guid,
        "link": item.link,
        "pubDate": item.pub_date,
        "category": item.category,
        "size": item.size,
        "description": item.description,
        "enclosure": item.enclosure_url,
        "attributes": dict(item.attributes),
    }


def render_results(
    items: Sequence[ResultItem],
    total: int,
    offset: int,
    o: str = "xml",
) -> str | dict:
    """Render aggregated results as a Newznab/Torznab RSS feed.

    The XML document declares the ``atom``, ``newznab`` and ``torznab``
    namespaces (the atom prefix is kept for newznab-client compatibility) and
    carries a channel-level ``<newznab:response offset=... total=.../>`` followed
    by one ``<item>`` per result. When ``item.enclosure_url`` is set an
    ``<enclosure url=... length=... type="application/x-nzb"/>`` is rendered
    (nzbhydra2 parity; AIOStreams' newznab integration drops items without one).
    Each ``attributes`` entry becomes a
    ``<torznab:attr name=... value=.../>`` element.

    When ``o == "json"`` a JSON-serializable dict is returned with this exact
    shape::

        {
            "results": {
                "channel": {
                    "offset": 0,
                    "total": 2,
                    "items": [
                        {
                            "title": ..., "guid": ..., "link": ...,
                            "pubDate": ..., "category": ..., "size": ...,
                            "description": ..., "enclosure": ...,
                            "attributes": {...},
                        }
                    ],
                }
            }
        }
    """
    if o == "json":
        return {
            "results": {
                "channel": {
                    "offset": offset,
                    "total": total,
                    "items": [_item_to_json(item) for item in items],
                }
            }
        }

    rss = etree.Element(
        "rss",
        nsmap={"atom": _ATOM_NS, "newznab": _NEWZNAB_NS, "torznab": _TORZNAB_NS},
        version="2.0",
    )
    channel = etree.SubElement(rss, "channel")
    etree.SubElement(channel, "title").text = "stateless-hydra"
    etree.SubElement(channel, "description").text = "stateless-hydra aggregated results"
    etree.SubElement(channel, "link")
    etree.SubElement(channel, "language").text = "en-us"
    etree.SubElement(channel, "webMaster").text = "admin@stateless-hydra.invalid"
    etree.SubElement(channel, "category").text = "search"
    etree.SubElement(channel, _qname(_NEWZNAB_NS, "response"), offset=str(offset), total=str(total))

    for item in items:
        item_el = etree.SubElement(channel, "item")
        etree.SubElement(item_el, "title").text = item.title
        etree.SubElement(item_el, "guid", isPermaLink="false").text = item.guid
        etree.SubElement(item_el, "link").text = item.link
        if item.enclosure_url is not None:
            # nzbhydra2 emits the download URL as an enclosure; clients such as
            # AIOStreams build the NZB URL from it and skip items without an
            # ``application/x-nzb`` enclosure entirely. ``length`` is omitted
            # when the size is unknown rather than emitted as an empty value.
            enclosure_attrs = {"url": item.enclosure_url, "type": "application/x-nzb"}
            if item.size is not None:
                enclosure_attrs["length"] = str(item.size)
            etree.SubElement(item_el, "enclosure", **enclosure_attrs)
        etree.SubElement(item_el, "pubDate").text = item.pub_date
        etree.SubElement(item_el, "category").text = item.category
        if item.size is not None:
            etree.SubElement(item_el, "size").text = str(item.size)
        etree.SubElement(item_el, "description").text = item.description
        for name, value in item.attributes.items():
            etree.SubElement(item_el, _qname(_TORZNAB_NS, "attr"), name=name, value=value)

    return etree.tostring(rss, xml_declaration=True, encoding="UTF-8").decode("UTF-8")


def _parse_item(element: etree._Element) -> ResultItem:
    title = _text(_child(element, "title")) or ""
    link = _text(_child(element, "link")) or ""

    guid_el = _child(element, "guid")
    guid = _text(guid_el) or ""
    if guid_el is not None and (guid_el.get("isPermaLink") or "").lower() == "true":
        # A permalink guid *is* the detail link; keep the link as the guid.
        guid = link

    pub_date = _text(_child(element, "pubDate")) or ""

    category = ""
    category_el = _child(element, "category")
    if category_el is not None and category_el.text:
        tokens = category_el.text.split()
        if tokens:
            category = tokens[0]

    attributes: dict[str, str] = {}
    for child in element:
        if _local_name(child.tag) != "attr":
            continue
        name = child.get("name")
        if name is None:
            continue
        attributes[name] = child.get("value") or ""

    enclosure_el = _child(element, "enclosure")
    enclosure_url = enclosure_el.get("url") if enclosure_el is not None else None

    return ResultItem(
        title=title,
        guid=guid,
        link=link,
        pub_date=pub_date,
        category=category,
        size=_int_or_none(_text(_child(element, "size"))),
        description=_text(_child(element, "description")),
        enclosure_url=enclosure_url,
        attributes=attributes,
    )


def parse_indexer_rss(xml_text: str) -> ParsedRss:
    """Parse a Newznab/Torznab RSS feed returned by an upstream indexer.

    Namespaces are ignored (matching is by local name). Channel-level
    ``newznab:response`` ``offset``/``total`` attributes are read, and every
    ``torznab:attr``/``newznab:attr`` element becomes an entry in
    :attr:`ResultItem.attributes` (last value wins).

    Raises:
        ValueError: if ``xml_text`` is not well-formed XML.
    """
    try:
        root = etree.fromstring(xml_text.encode("utf-8"))
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise ValueError(f"malformed indexer RSS XML: {exc}") from exc

    channel = next(
        (el for el in root.iter() if _local_name(el.tag) == "channel"),
        None,
    )
    if channel is None:
        return ParsedRss(items=[])

    total: int | None = None
    offset: int | None = None
    response_el = _child(channel, "response")
    if response_el is not None:
        total = _int_or_none(response_el.get("total"))
        offset = _int_or_none(response_el.get("offset"))

    items = [_parse_item(child) for child in channel if _local_name(child.tag) == "item"]
    return ParsedRss(items=items, total=total, offset=offset)


def canonical_query(params: Mapping[Any, Any]) -> tuple[str, ...]:
    """Return a stable cache key for a set of query parameters.

    The ``apikey`` parameter is dropped (it is a credential, not part of the
    query semantics). ``None`` values are treated as absent and skipped. Keys
    and values are coerced with :func:`str`, stripped of surrounding whitespace,
    and empty keys are skipped. The result is sorted so dict iteration order
    cannot affect the key. This never raises for a ``Mapping`` input.
    """
    pairs: list[str] = []
    for key, value in params.items():
        if value is None:
            continue
        clean_key = str(key).strip()
        if not clean_key or clean_key.lower() == "apikey":
            continue
        pairs.append(f"{clean_key}={str(value).strip()}")
    pairs.sort()
    return tuple(pairs)


def compose_guid(indexer_name: str, original_guid: str) -> str:
    """Compose the aggregated guid ``"{indexer_name}:{original_guid}"``.

    The owning indexer is embedded so ``getnzb`` can route a later download
    back to it. ``indexer_name`` must not contain ``":"``; that constraint is
    enforced here so :func:`split_guid` is always able to recover the pair.
    """
    if ":" in indexer_name:
        raise ValueError(f"indexer name must not contain ':': {indexer_name!r}")
    return f"{indexer_name}:{original_guid}"


def split_guid(guid: str) -> tuple[str, str] | None:
    """Recover ``(indexer_name, original_guid)`` from a composed guid.

    Splits on the *first* colon. Returns ``None`` when ``guid`` contains no
    colon.
    """
    if ":" not in guid:
        return None
    indexer_name, original_guid = guid.split(":", 1)
    return indexer_name, original_guid
