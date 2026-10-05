"""Tests for the pure Newznab/Torznab protocol module.

Nothing here touches the network or Redis: every function under test works on
in-memory strings and dataclasses. Fixtures live in this file because
``tests/conftest.py`` is owned by a later task.
"""

import pytest
from lxml import etree

from stateless_hydra import __version__
from stateless_hydra.exceptions import StatelessHydraError
from stateless_hydra.newznab import (
    CATEGORIES,
    ERROR_CODES,
    NewznabError,
    ResultItem,
    canonical_query,
    compose_guid,
    parse_indexer_rss,
    parse_upstream_error,
    render_caps,
    render_error,
    render_results,
    split_guid,
)

_NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"
_TORZNAB_NS = "http://torznab.com/schemas/2015/feed"


def _parse(xml_text: str) -> etree._Element:
    return etree.fromstring(xml_text.encode("utf-8"))


SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"
     xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>Example Indexer</title>
    <newznab:response offset="25" total="1234"/>
    <item>
      <title>Some.Release.1080p.WEB-DL</title>
      <guid isPermaLink="false">abc123</guid>
      <link>https://indexer.invalid/getnzb/abc123.nzb</link>
      <pubDate>Sun, 04 Oct 2026 12:00:00 +0000</pubDate>
      <category>5040</category>
      <size>1073741824</size>
      <description>A very nice release</description>
      <torznab:attr name="seeders" value="42"/>
      <torznab:attr name="peers" value="50"/>
      <newznab:attr name="imdb" value="tt1234567"/>
    </item>
    <item>
      <title>No.Frills.Release</title>
      <guid isPermaLink="false">def456</guid>
      <link>https://indexer.invalid/getnzb/def456.nzb</link>
      <pubDate>Sun, 04 Oct 2026 13:00:00 +0000</pubDate>
      <category>2000 2040</category>
      <size>not-a-number</size>
      <torznab:attr name="seeders" value="5"/>
    </item>
  </channel>
</rss>
"""


# --- error codes and NewznabError -------------------------------------------


def test_error_codes_contain_910_and_930_with_exact_descriptions():
    assert ERROR_CODES[910] == "API hit limit reached"
    assert ERROR_CODES[930] == "Download limit reached"


def test_newznab_error_known_code_uses_table_description():
    error = NewznabError(201)

    assert error.code == 201
    assert error.description == "Incorrect parameter"
    assert error.args[0] == "201: Incorrect parameter"


def test_newznab_error_unknown_code_falls_back_to_900():
    error = NewznabError(12345)

    assert error.code == 900
    assert error.description == "Unknown error"


def test_newznab_error_9999_normalizes_to_900():
    error = NewznabError(9999)

    assert error.code == 900
    assert error.description == "Unknown error"


def test_newznab_error_is_stateless_hydra_error():
    assert issubclass(NewznabError, StatelessHydraError)
    assert isinstance(NewznabError(900), StatelessHydraError)


def test_newznab_error_custom_description_is_kept():
    error = NewznabError(100, "nope")

    assert error.code == 100
    assert error.description == "nope"


# --- render_error -----------------------------------------------------------


def test_render_error_known_code_exact_string():
    assert render_error(910) == '<error code="910" description="API hit limit reached"/>\n'


def test_render_error_custom_description_exact_string():
    assert render_error(100, "bad key") == '<error code="100" description="bad key"/>\n'


def test_render_error_escapes_description():
    rendered = render_error(300, 'a & "b" <c>')

    assert rendered == '<error code="300" description="a &amp; &quot;b&quot; &lt;c&gt;"/>\n'
    _parse(rendered)  # still well-formed


def test_render_error_unknown_code_normalizes_to_900():
    assert render_error(9999) == '<error code="900" description="Unknown error"/>\n'
    # A caller-supplied description is preserved, matching NewznabError.
    assert render_error(9999, "custom") == '<error code="900" description="custom"/>\n'


# --- parse_upstream_error ---------------------------------------------------


def test_parse_upstream_error_reads_code_and_description():
    assert parse_upstream_error('<error code="202" description="No such function"/>') == (
        202,
        "No such function",
    )


def test_parse_upstream_error_accepts_bytes_and_xml_declaration():
    body = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<error code="202" description="No such function"/>'
    )
    assert parse_upstream_error(body) == (202, "No such function")


def test_parse_upstream_error_single_quotes_and_reordered_attributes():
    assert parse_upstream_error("<error description='gone' code='300'/>") == (300, "gone")


def test_parse_upstream_error_unparseable_code_falls_back_to_900():
    assert parse_upstream_error('<error code="abc" description="weird"/>') == (900, "weird")


def test_parse_upstream_error_non_error_documents_return_none():
    assert parse_upstream_error("<rss><channel/></rss>") is None
    assert parse_upstream_error(b"PK\x03\x04fake-nzb") is None
    assert parse_upstream_error("") is None
    # ``<error>`` without attributes is not a usable Newznab error document.
    assert parse_upstream_error("<error/>") is None


# --- render_caps ------------------------------------------------------------


def test_render_caps_xml_is_well_formed_with_expected_structure():
    xml = render_caps({"search", "tvsearch", "movie", "music", "book"})
    root = _parse(xml)

    assert root.tag == "caps"
    server = root.find("server")
    assert server is not None
    assert server.get("appversion") == __version__
    assert server.get("version") == "2.0"
    assert server.get("title") == "stateless-hydra"
    assert server.get("strapline") == "stateless-hydra"
    assert root.find("searching") is not None
    assert root.find("categories") is not None


def test_render_caps_appversion_tracks_package_version():
    # Pin the caps ``server`` element to the runtime ``__version__`` so a
    # semantic-release bump can never silently leave clients with a stale
    # appversion (the value is no longer a hardcoded literal).
    server = _parse(render_caps({"search"})).find("server")

    assert server is not None
    assert server.get("appversion") == __version__


def test_render_caps_unknown_search_type_raises_value_error():
    with pytest.raises(ValueError, match="unknown search type"):
        render_caps({"search", "bogus"})
    with pytest.raises(ValueError, match="unknown search type"):
        render_caps({"bogus"}, o="json")


def test_render_caps_only_advertises_requested_search_types():
    xml = render_caps({"search", "tvsearch"})
    root = _parse(xml)
    searching = root.find("searching")

    assert searching is not None
    tags = [child.tag for child in searching]
    assert tags == ["search", "tv-search"]
    assert searching.find("search").get("supportedParams") == "q"
    assert searching.find("tv-search").get("supportedParams") == "q,season,ep"
    assert searching.find("movie-search") is None
    assert searching.find("music-search") is None
    assert searching.find("book-search") is None


def test_render_caps_categories_are_nested_parents_and_subcats():
    root = _parse(render_caps({"search"}, o="xml"))
    categories = root.find("categories")

    tv = categories.find("category[@id='5000']")
    assert tv is not None
    assert tv.get("name") == "TV"
    assert tv.find("subcat[@id='5040']").get("name") == "TV/HD"

    movies = categories.find("category[@id='2000']")
    assert movies.get("name") == "Movies"
    assert movies.find("subcat[@id='2040']").get("name") == "Movies/HD"


def test_categories_table_has_required_entries():
    assert CATEGORIES[5000] == "TV"
    assert CATEGORIES[5040] == "TV/HD"
    assert CATEGORIES[2000] == "Movies"
    assert CATEGORIES[2040] == "Movies/HD"
    assert CATEGORIES[2030] == "Movies/SD"
    assert CATEGORIES[2060] == "Movies/3D"
    assert CATEGORIES[5060] == "TV/Sport"
    assert CATEGORIES[7020] == "Other/E-Book"
    # Parents are ids divisible by 1000 and there is a subcat for each.
    assert {cid for cid in CATEGORIES if cid % 1000 == 0} == {
        1000,
        2000,
        3000,
        4000,
        5000,
        6000,
        7000,
    }


def test_render_caps_json_exact_shape():
    caps = render_caps({"search", "tvsearch"}, o="json")

    assert set(caps) == {"caps"}
    assert caps["caps"]["server"] == {
        "appversion": __version__,
        "version": "2.0",
        "title": "stateless-hydra",
        "strapline": "stateless-hydra",
    }
    assert list(caps["caps"]["searching"]) == ["search", "tv-search"]
    assert caps["caps"]["searching"]["search"] == {
        "available": "yes",
        "supportedParams": "q",
    }
    assert caps["caps"]["searching"]["tv-search"] == {
        "available": "yes",
        "supportedParams": "q,season,ep",
    }

    tv = next(c for c in caps["caps"]["categories"] if c["id"] == "5000")
    assert set(tv) == {"id", "name", "subcat"}
    assert tv["name"] == "TV"
    assert {"id": "5040", "name": "TV/HD"} in tv["subcat"]


# --- render_results ---------------------------------------------------------


def _sample_item(**overrides) -> ResultItem:
    base = {
        "title": "Some.Release.1080p",
        "guid": "ix:abc123",
        "link": "https://indexer.invalid/getnzb/abc123.nzb",
        "pub_date": "Sun, 04 Oct 2026 12:00:00 +0000",
        "category": "5040",
        "size": 1073741824,
        "description": "A very nice release",
        "attributes": {"seeders": "42", "imdb": "tt1234567"},
    }
    base.update(overrides)
    return ResultItem(**base)


def test_render_results_xml_namespaces_response_and_item():
    item = _sample_item()
    xml = render_results([item], total=2, offset=1)
    root = _parse(xml)

    assert root.tag == "rss"
    assert root.get("version") == "2.0"
    assert set(root.nsmap) == {"atom", "newznab", "torznab"}

    response = root.find(f".//{{{_NEWZNAB_NS}}}response")
    assert response is not None
    assert response.get("offset") == "1"
    assert response.get("total") == "2"

    channel = root.find("channel")
    assert channel.findtext("title") == "stateless-hydra"
    assert channel.findtext("description") == "stateless-hydra aggregated results"
    assert channel.findtext("language") == "en-us"
    assert channel.findtext("webMaster") == "admin@stateless-hydra.invalid"
    assert channel.findtext("category") == "search"

    item_el = root.find(".//item")
    assert item_el.findtext("title") == item.title
    guid_el = item_el.find("guid")
    assert guid_el.text == item.guid
    assert guid_el.get("isPermaLink") == "false"
    assert item_el.findtext("link") == item.link
    assert item_el.findtext("pubDate") == item.pub_date
    assert item_el.findtext("category") == item.category
    assert item_el.findtext("size") == str(item.size)
    assert item_el.findtext("description") == item.description

    attrs = {el.get("name"): el.get("value") for el in item_el.findall(f"{{{_TORZNAB_NS}}}attr")}
    assert attrs == item.attributes


def test_render_results_round_trips_through_parser():
    item = _sample_item()
    xml = render_results([item], total=1, offset=0)

    parsed = parse_indexer_rss(xml)

    assert parsed.total == 1
    assert parsed.offset == 0
    assert parsed.items == [item]


def test_render_results_omits_size_when_none():
    item = _sample_item(size=None, description=None)
    root = _parse(render_results([item], total=1, offset=0))

    assert root.find(".//item/size") is None
    assert parse_indexer_rss(render_results([item], total=1, offset=0)).items == [item]


def test_render_results_renders_nzb_enclosure_when_url_set():
    # AIOStreams' newznab integration builds the NZB URL from the enclosure and
    # silently drops every item that lacks an ``application/x-nzb`` one, so the
    # presence and shape of this element are load-bearing for compatibility.
    item = _sample_item(enclosure_url="https://hydra.invalid/api?t=getnzb&id=ix%3Aabc123")
    root = _parse(render_results([item], total=1, offset=0))

    enclosure = root.find(".//item/enclosure")
    assert enclosure is not None
    assert enclosure.get("url") == item.enclosure_url
    assert enclosure.get("length") == str(item.size)
    assert enclosure.get("type") == "application/x-nzb"


def test_render_results_omits_enclosure_when_url_none():
    item = _sample_item()
    root = _parse(render_results([item], total=1, offset=0))

    assert root.find(".//item/enclosure") is None


def test_render_results_enclosure_omits_length_when_size_none():
    item = _sample_item(size=None, enclosure_url="https://hydra.invalid/api?t=getnzb&id=x")
    enclosure = _parse(render_results([item], total=1, offset=0)).find(".//item/enclosure")

    assert enclosure is not None
    assert enclosure.get("length") is None


def test_render_results_enclosure_round_trips_through_parser():
    item = _sample_item(enclosure_url="https://hydra.invalid/api?t=getnzb&id=ix%3Aabc123")
    parsed = parse_indexer_rss(render_results([item], total=1, offset=0))

    assert parsed.items == [item]
    assert parsed.items[0].enclosure_url == item.enclosure_url


def test_render_results_json_exact_shape():
    item = _sample_item()
    out = render_results([item], total=2, offset=1, o="json")

    assert set(out) == {"results"}
    channel = out["results"]["channel"]
    assert set(channel) == {"items", "offset", "total"}
    assert channel["offset"] == 1
    assert channel["total"] == 2
    assert channel["items"] == [
        {
            "title": item.title,
            "guid": item.guid,
            "link": item.link,
            "pubDate": item.pub_date,
            "category": item.category,
            "size": item.size,
            "description": item.description,
            "enclosure": item.enclosure_url,
            "attributes": item.attributes,
        }
    ]


# --- parse_indexer_rss ------------------------------------------------------


def test_parse_indexer_rss_reads_response_and_items():
    parsed = parse_indexer_rss(SAMPLE_RSS)

    assert parsed.total == 1234
    assert parsed.offset == 25
    assert len(parsed.items) == 2

    first = parsed.items[0]
    assert first.title == "Some.Release.1080p.WEB-DL"
    assert first.guid == "abc123"
    assert first.link == "https://indexer.invalid/getnzb/abc123.nzb"
    assert first.pub_date == "Sun, 04 Oct 2026 12:00:00 +0000"
    assert first.category == "5040"
    assert first.size == 1073741824
    assert first.description == "A very nice release"
    assert first.attributes == {"seeders": "42", "peers": "50", "imdb": "tt1234567"}


def test_parse_indexer_rss_missing_optional_fields_default_to_none():
    parsed = parse_indexer_rss(SAMPLE_RSS)
    second = parsed.items[1]

    assert second.size is None
    assert second.description is None
    # Category text may carry extra tokens; only the id is kept.
    assert second.category == "2000"
    assert second.attributes == {"seeders": "5"}


def test_parse_indexer_rss_reads_size_from_attr_when_size_element_absent():
    # Classic nZEDb/Newznab indexers (drunkenSlug) omit <size> and report it
    # only as a torznab/newznab ``size`` attribute. Dropping it left item.size
    # unset, so the rendered enclosure had no length and AIOStreams' newznab
    # profile saw size 0.
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"
         xmlns:torznab="http://torznab.com/schemas/2015/feed">
      <channel>
        <newznab:response xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"
                          offset="0" total="1"/>
        <item>
          <title>Some.Release.1080p</title>
          <guid isPermaLink="false">abc123</guid>
          <link>https://indexer.invalid/getnzb/abc123.nzb</link>
          <pubDate>Sun, 04 Oct 2026 12:00:00 +0000</pubDate>
          <category>2040</category>
          <torznab:attr name="size" value="2688419601"/>
        </item>
      </channel>
    </rss>
    """

    item = parse_indexer_rss(rss).items[0]

    assert item.size == 2688419601
    assert item.attributes == {"size": "2688419601"}


def test_parse_indexer_rss_prefers_size_element_over_attr():
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"
         xmlns:torznab="http://torznab.com/schemas/2015/feed">
      <channel>
        <item>
          <title>Some.Release.1080p</title>
          <guid>abc123</guid>
          <link>https://indexer.invalid/getnzb/abc123.nzb</link>
          <pubDate>Sun, 04 Oct 2026 12:00:00 +0000</pubDate>
          <category>2040</category>
          <size>100</size>
          <torznab:attr name="size" value="999"/>
        </item>
      </channel>
    </rss>
    """

    assert parse_indexer_rss(rss).items[0].size == 100


def test_parse_indexer_rss_uses_link_when_guid_is_permalink():
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <item>
          <title>x</title>
          <guid isPermaLink="true">https://indexer.invalid/details/1</guid>
          <link>https://indexer.invalid/details/1</link>
          <pubDate>Sun, 04 Oct 2026 12:00:00 +0000</pubDate>
          <category>5000</category>
        </item>
      </channel>
    </rss>
    """

    item = parse_indexer_rss(rss).items[0]

    assert item.guid == "https://indexer.invalid/details/1"


def test_parse_indexer_rss_tolerates_missing_namespaces():
    rss = """<rss version="2.0"><channel>
      <newznab:response xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"
                        offset="0" total="3"/>
      <item>
        <title>plain</title>
        <guid>g1</guid>
        <link>https://x.invalid/1</link>
        <pubDate>Sun, 04 Oct 2026 12:00:00 +0000</pubDate>
        <category>1000</category>
      </item>
    </channel></rss>"""

    parsed = parse_indexer_rss(rss)

    assert parsed.total == 3
    assert parsed.offset == 0
    assert parsed.items[0].title == "plain"


def test_parse_indexer_rss_matches_attr_by_namespace_not_prefix():
    # The prefix is arbitrary ("foo"); only the namespace URI matters.
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:foo="http://torznab.com/schemas/2015/feed">
      <channel>
        <item>
          <title>t</title>
          <guid>g</guid>
          <link>https://x.invalid/1</link>
          <pubDate>Sun, 04 Oct 2026 12:00:00 +0000</pubDate>
          <category>5040</category>
          <foo:attr name="seeders" value="7"/>
          <foo:attr name="peers" value="8"/>
        </item>
      </channel>
    </rss>"""

    parsed = parse_indexer_rss(rss)

    assert parsed.items[0].attributes == {"seeders": "7", "peers": "8"}


def test_parse_indexer_rss_reads_unqualified_attr_elements():
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <item>
          <title>t</title>
          <guid>g</guid>
          <link>https://x.invalid/1</link>
          <pubDate>Sun, 04 Oct 2026 12:00:00 +0000</pubDate>
          <category>5040</category>
          <attr name="seeders" value="3"/>
        </item>
      </channel>
    </rss>"""

    parsed = parse_indexer_rss(rss)

    assert parsed.items[0].attributes == {"seeders": "3"}


def test_parse_indexer_rss_duplicate_attribute_last_value_wins():
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"
         xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"
         xmlns:torznab="http://torznab.com/schemas/2015/feed">
      <channel>
        <item>
          <title>t</title>
          <guid>g</guid>
          <link>https://x.invalid/1</link>
          <pubDate>Sun, 04 Oct 2026 12:00:00 +0000</pubDate>
          <category>5040</category>
          <torznab:attr name="seeders" value="1"/>
          <newznab:attr name="seeders" value="9"/>
        </item>
      </channel>
    </rss>"""

    parsed = parse_indexer_rss(rss)

    assert parsed.items[0].attributes == {"seeders": "9"}


def test_parse_render_parse_round_trip_preserves_items():
    first_pass = parse_indexer_rss(SAMPLE_RSS)

    rendered = render_results(
        first_pass.items,
        total=first_pass.total or 0,
        offset=first_pass.offset or 0,
    )
    second_pass = parse_indexer_rss(rendered)

    assert second_pass.total == first_pass.total
    assert second_pass.offset == first_pass.offset
    assert second_pass.items == first_pass.items
    for item in second_pass.items:
        assert item.title
        assert item.guid
        assert item.category
        assert item.attributes


def test_parse_indexer_rss_malformed_xml_raises_value_error():
    with pytest.raises(ValueError, match="malformed indexer RSS XML"):
        parse_indexer_rss("<rss><channel></rss>")


# --- canonical_query --------------------------------------------------------


def test_canonical_query_ignores_apikey_and_strips_whitespace():
    result = canonical_query({"apikey": "secret", "q": "  hello ", " cat ": " 5040 "})

    assert result == ("cat=5040", "q=hello")


def test_canonical_query_is_order_independent():
    left = canonical_query({"b": "2", "a": "1"})
    right = canonical_query({"a": "1", "b": "2"})

    assert left == right == ("a=1", "b=2")


def test_canonical_query_empty():
    assert canonical_query({}) == ()
    assert canonical_query({"apikey": "only"}) == ()


def test_canonical_query_handles_none_int_and_empty_keys():
    result = canonical_query({"b": None, "a": 2, "": "x", "   ": "y", "q": "z"})

    assert result == ("a=2", "q=z")


def test_canonical_query_mixed_input_is_deterministic_and_never_raises():
    left = canonical_query({"q": 5000, "missing": None, "cat": " 5040 "})
    right = canonical_query({"cat": " 5040 ", "missing": None, "q": 5000})

    assert left == right == ("cat=5040", "q=5000")


# --- guid composition -------------------------------------------------------


def test_compose_and_split_guid_round_trip():
    guid = compose_guid("nzbgeek", "abc:def")

    assert guid == "nzbgeek:abc:def"
    assert split_guid(guid) == ("nzbgeek", "abc:def")


def test_split_guid_without_colon_returns_none():
    assert split_guid("no-colon") is None


def test_compose_guid_rejects_indexer_name_with_colon():
    with pytest.raises(ValueError, match="must not contain"):
        compose_guid("bad:name", "abc")
