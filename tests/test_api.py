"""Integration tests for the assembled FastAPI application.

The app is built from temporary YAML config with ``fakeredis`` as the Redis
backend; every upstream indexer call is intercepted by ``respx``. No live
services are required.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fakeredis import aioredis
from fastapi.testclient import TestClient
from lxml import etree

from stateless_hydra.config import AppSettings
from stateless_hydra.exceptions import ConfigError
from stateless_hydra.main import create_app

API = "/api"
KEY = "test-key"
GEEK = "https://nzbgeek.example.com/api"
SLUG = "https://slug.example.com/api"


def _slug_rss(base: str) -> str:
    """A variant of the sample feed with distinct titles/guids/descriptions."""
    return (
        base.replace("Sample One", "Slug One")
        .replace("Sample Two", "Slug Two")
        .replace("guid-one", "sguid-one")
        .replace("guid-two", "sguid-two")
        .replace("Mon, 02 Oct 2023 12:00:00 GMT", "Wed, 04 Oct 2023 12:00:00 GMT")
        .replace("Sun, 01 Oct 2023 12:00:00 GMT", "Sat, 30 Sep 2023 12:00:00 GMT")
    )


def _items(response_text: str) -> list:
    root = etree.fromstring(response_text.encode("utf-8"))
    return root.findall(".//item")


def _guids(response_text: str) -> list[str]:
    return [element.findtext("guid") for element in _items(response_text)]


def _enclosures(response_text: str) -> list:
    """The ``<enclosure>`` element of each item (``None`` when absent)."""
    return [item.find("enclosure") for item in _items(response_text)]


def _response_total(response_text: str) -> str | None:
    root = etree.fromstring(response_text.encode("utf-8"))
    for element in root.iter():
        local = element.tag.rsplit("}", 1)[-1]
        if local == "response":
            return element.get("total")
    return None


# --- authentication and dispatch --------------------------------------------


def test_missing_api_key(client):
    response = client.get(API, params={"t": "search", "q": "ubuntu"})

    assert response.status_code == 200
    assert 'code="100"' in response.text


def test_incorrect_api_key(client):
    response = client.get(API, params={"t": "caps", "apikey": "nope"})

    assert response.status_code == 200
    assert 'code="100"' in response.text


def test_missing_t_yields_200(client):
    response = client.get(API, params={"apikey": KEY})

    assert response.status_code == 200
    assert 'code="200"' in response.text


def test_unknown_t_yields_202(client):
    response = client.get(API, params={"t": "wat", "apikey": KEY})

    assert response.status_code == 200
    assert 'code="202"' in response.text


def test_caps_xml(client):
    response = client.get(API, params={"t": "caps", "apikey": KEY})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "<caps>" in response.text
    assert "<searching>" in response.text
    assert "tv-search" in response.text


def test_caps_json(client):
    response = client.get(API, params={"t": "caps", "apikey": KEY, "o": "json"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    data = response.json()
    assert "searching" in data["caps"]
    assert "search" in data["caps"]["searching"]


# --- search aggregation ------------------------------------------------------


@respx.mock
def test_search_aggregates_both_indexers_and_sorts_by_pubdate(client, sample_rss):
    respx.get(GEEK).mock(return_value=httpx.Response(200, text=sample_rss))
    respx.get(SLUG).mock(return_value=httpx.Response(200, text=_slug_rss(sample_rss)))

    response = client.get(API, params={"t": "search", "q": "ubuntu", "apikey": KEY})

    assert response.status_code == 200
    assert len(_items(response.text)) == 4
    assert _response_total(response.text) == "4"
    # Newest first: slug (04 Oct), geek (02 Oct), geek (01 Oct), slug (30 Sep).
    assert _guids(response.text) == [
        "slug:sguid-one",
        "nzbgeek:guid-one",
        "nzbgeek:guid-two",
        "slug:sguid-two",
    ]


@respx.mock
def test_search_skips_disabled_indexer(client, sample_rss):
    respx.get(GEEK).mock(return_value=httpx.Response(200, text=sample_rss))
    respx.get(SLUG).mock(return_value=httpx.Response(200, text=_slug_rss(sample_rss)))
    ghost = respx.get("https://ghost.example.com/api").mock(
        return_value=httpx.Response(200, text=sample_rss)
    )

    response = client.get(API, params={"t": "search", "q": "ubuntu", "apikey": KEY})

    assert len(_items(response.text)) == 4
    assert ghost.call_count == 0


@respx.mock
def test_search_limit_slices_items_but_keeps_total(client, sample_rss):
    respx.get(GEEK).mock(return_value=httpx.Response(200, text=sample_rss))
    respx.get(SLUG).mock(return_value=httpx.Response(200, text=_slug_rss(sample_rss)))

    response = client.get(API, params={"t": "search", "q": "ubuntu", "apikey": KEY, "limit": "1"})

    assert len(_items(response.text)) == 1
    assert _response_total(response.text) == "4"


@respx.mock
def test_search_items_carry_nzb_enclosure_pointing_at_our_getnzb(client, sample_rss):
    # AIOStreams' newznab integration reads the NZB URL from <enclosure> and
    # silently drops every item without an ``application/x-nzb`` one. The URL
    # must be our own getnzb endpoint (composed guid) so downloads stay routed
    # through the limit tracker instead of leaking an upstream indexer key.
    respx.get(GEEK).mock(return_value=httpx.Response(200, text=sample_rss))
    respx.get(SLUG).mock(return_value=httpx.Response(200, text=_slug_rss(sample_rss)))

    response = client.get(API, params={"t": "search", "q": "ubuntu", "apikey": KEY})

    enclosures = _enclosures(response.text)
    assert all(enclosure is not None for enclosure in enclosures)
    assert all(enclosure.get("type") == "application/x-nzb" for enclosure in enclosures)
    # The enclosure URL targets our own /api and carries the composed guid,
    # url-encoded, plus the caller's key.
    first = enclosures[0]
    params = dict(httpx.URL(first.get("url")).params)
    assert first.get("url").startswith(f"http://testserver{API}?")
    assert params["t"] == "getnzb"
    assert params["id"] == "slug:sguid-one"
    assert params["apikey"] == KEY
    assert first.get("length") == "123456"


@respx.mock
def test_enclosure_url_round_trips_through_getnzb(client, sample_rss):
    def _geek(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("t") == "getnzb":
            return httpx.Response(200, content=b"real-nzb")
        return httpx.Response(200, text=sample_rss)

    respx.get(GEEK).mock(side_effect=_geek)
    respx.get(SLUG).mock(return_value=httpx.Response(200, text=_slug_rss(sample_rss)))

    search = client.get(
        API, params={"t": "search", "q": "ubuntu", "apikey": KEY, "indexer": "nzbgeek"}
    )
    enclosure = _enclosures(search.text)[0]
    # Follow the advertised download URL. With respx installed, TestClient must
    # be given a relative path so the request reaches the ASGI app; the parsed
    # path+query is behaviourally identical to what a real client fetches.
    url = httpx.URL(enclosure.get("url"))
    download = client.get(url.path, params=dict(url.params))

    assert download.status_code == 200
    assert download.content == b"real-nzb"
    assert download.headers["content-type"] == "application/x-nzb"


@respx.mock
def test_search_json_shape(client, sample_rss):
    respx.get(GEEK).mock(return_value=httpx.Response(200, text=sample_rss))
    respx.get(SLUG).mock(return_value=httpx.Response(200, text=_slug_rss(sample_rss)))

    response = client.get(API, params={"t": "search", "q": "ubuntu", "apikey": KEY, "o": "json"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    data = response.json()
    assert data["results"]["channel"]["total"] == 4
    items = data["results"]["channel"]["items"]
    assert len(items) == 4
    assert items[0]["guid"] == "slug:sguid-one"


@respx.mock
def test_missing_search_parameter_yields_200(client):
    response = client.get(API, params={"t": "search", "apikey": KEY})

    assert response.status_code == 200
    assert 'code="200"' in response.text


def test_bad_offset_yields_201(client):
    response = client.get(API, params={"t": "search", "q": "ubuntu", "apikey": KEY, "offset": "-1"})

    assert response.status_code == 200
    assert 'code="201"' in response.text


@respx.mock
def test_limit_zero_returns_empty_result_without_upstream(client, sample_rss):
    geek = respx.get(GEEK).mock(return_value=httpx.Response(200, text=sample_rss))
    slug = respx.get(SLUG).mock(return_value=httpx.Response(200, text=_slug_rss(sample_rss)))

    response = client.get(API, params={"t": "search", "q": "ubuntu", "apikey": KEY, "limit": "0"})

    assert response.status_code == 200
    assert _items(response.text) == []
    assert _response_total(response.text) == "0"
    assert geek.call_count == 0
    assert slug.call_count == 0


@respx.mock
def test_music_track_is_a_valid_search_param_and_is_forwarded(client, sample_rss):
    geek = respx.get(GEEK).mock(return_value=httpx.Response(200, text=sample_rss))
    respx.get(SLUG).mock(return_value=httpx.Response(200, text=_slug_rss(sample_rss)))

    response = client.get(API, params={"t": "music", "track": "mysong", "apikey": KEY})

    assert response.status_code == 200
    assert "Missing query parameter" not in response.text
    assert _items(response.text)
    assert geek.calls[0].request.url.params.get("track") == "mysong"


@respx.mock
def test_upstream_limit_is_capped_by_max_results_per_indexer(client, sample_rss):
    geek = respx.get(GEEK).mock(return_value=httpx.Response(200, text=sample_rss))
    respx.get(SLUG).mock(return_value=httpx.Response(200, text=_slug_rss(sample_rss)))

    client.get(API, params={"t": "search", "q": "ubuntu", "apikey": KEY})

    assert geek.calls[0].request.url.params.get("limit") == "10"


@respx.mock
def test_offset_slices_items_but_keeps_total(client, sample_rss):
    respx.get(GEEK).mock(return_value=httpx.Response(200, text=sample_rss))
    respx.get(SLUG).mock(return_value=httpx.Response(200, text=_slug_rss(sample_rss)))

    response = client.get(
        API,
        params={"t": "search", "q": "ubuntu", "apikey": KEY, "indexer": "nzbgeek", "offset": "1"},
    )

    assert len(_items(response.text)) == 1
    assert _response_total(response.text) == "2"
    assert _guids(response.text) == ["nzbgeek:guid-two"]


@respx.mock
def test_no_eligible_indexer_yields_203(client, sample_rss):
    # Filtering to the disabled "ghost" indexer leaves no eligible indexer.
    ghost = respx.get("https://ghost.example.com/api").mock(
        return_value=httpx.Response(200, text=sample_rss)
    )

    response = client.get(
        API, params={"t": "search", "q": "ubuntu", "apikey": KEY, "indexer": "ghost"}
    )

    assert response.status_code == 200
    assert 'code="203"' in response.text
    assert ghost.call_count == 0


# --- caching -----------------------------------------------------------------


@respx.mock
def test_second_identical_search_is_served_from_cache(client, sample_rss):
    geek = respx.get(GEEK).mock(return_value=httpx.Response(200, text=sample_rss))
    slug = respx.get(SLUG).mock(return_value=httpx.Response(200, text=_slug_rss(sample_rss)))

    params = {"t": "search", "q": "ubuntu", "apikey": KEY}
    client.get(API, params=params)
    client.get(API, params=params)

    assert geek.call_count == 1
    assert slug.call_count == 1
    metrics = client.get("/metrics").text
    assert 'stateless_hydra_cache_hits_total{indexer="nzbgeek"} 1.0' in metrics


@respx.mock
def test_different_query_is_a_cache_miss(client, sample_rss):
    geek = respx.get(GEEK).mock(return_value=httpx.Response(200, text=sample_rss))
    respx.get(SLUG).mock(return_value=httpx.Response(200, text=_slug_rss(sample_rss)))

    client.get(API, params={"t": "search", "q": "ubuntu", "apikey": KEY})
    client.get(API, params={"t": "search", "q": "debian", "apikey": KEY})

    assert geek.call_count == 2


@respx.mock
def test_malformed_upstream_response_is_not_cached(client, sample_rss):
    geek = respx.get(GEEK).mock(return_value=httpx.Response(200, text="<rss><channel>"))
    respx.get(SLUG).mock(return_value=httpx.Response(200, text=_slug_rss(sample_rss)))

    params = {"t": "search", "q": "ubuntu", "apikey": KEY}
    first = client.get(API, params=params)
    second = client.get(API, params=params)

    assert first.status_code == 200
    assert second.status_code == 200
    # The malformed payload must not be cached: the second search re-queries it.
    assert geek.call_count == 2


# --- API hit limits ----------------------------------------------------------


@respx.mock
def test_exhausted_indexer_is_skipped_but_other_answers(client, sample_rss):
    geek = respx.get(GEEK).mock(return_value=httpx.Response(200, text=sample_rss))
    slug = respx.get(SLUG).mock(return_value=httpx.Response(200, text=_slug_rss(sample_rss)))

    for query in ("one", "two", "three"):
        response = client.get(API, params={"t": "search", "q": query, "apikey": KEY})
        assert response.status_code == 200

    assert geek.call_count == 2
    assert slug.call_count == 3


@respx.mock
def test_exhausted_indexer_filtered_alone_yields_910(client, sample_rss):
    respx.get(GEEK).mock(return_value=httpx.Response(200, text=sample_rss))
    respx.get(SLUG).mock(return_value=httpx.Response(200, text=_slug_rss(sample_rss)))

    for query in ("one", "two"):
        client.get(API, params={"t": "search", "q": query, "apikey": KEY})

    response = client.get(
        API, params={"t": "search", "q": "three", "apikey": KEY, "indexer": "nzbgeek"}
    )

    assert response.status_code == 200
    assert 'code="910"' in response.text


# --- NZB download limits -----------------------------------------------------


@respx.mock
def test_getnzb_succeeds_then_hits_930(client):
    respx.get(GEEK, params={"t": "getnzb", "id": "xyz"}).mock(
        return_value=httpx.Response(200, content=b"fake-nzb")
    )

    first = client.get(API, params={"t": "getnzb", "id": "nzbgeek:xyz", "apikey": KEY})

    assert first.status_code == 200
    assert first.content == b"fake-nzb"
    assert first.headers["content-type"] == "application/x-nzb"
    assert 'filename="xyz.nzb"' in first.headers["content-disposition"]

    second = client.get(API, params={"t": "getnzb", "id": "nzbgeek:xyz", "apikey": KEY})

    assert second.status_code == 200
    assert 'code="930"' in second.text


def test_getnzb_bad_guid_yields_300(client):
    response = client.get(API, params={"t": "getnzb", "id": "nocolon", "apikey": KEY})

    assert response.status_code == 200
    assert 'code="300"' in response.text


@respx.mock
def test_getnzb_disabled_indexer_yields_300_without_upstream(client):
    ghost = respx.get("https://ghost.example.com/api").mock(
        return_value=httpx.Response(200, content=b"should-not-be-served")
    )

    response = client.get(API, params={"t": "getnzb", "id": "ghost:xyz", "apikey": KEY})

    assert response.status_code == 200
    assert 'code="300"' in response.text
    assert ghost.call_count == 0


def test_getnzb_missing_id_yields_200(client):
    response = client.get(API, params={"t": "getnzb", "apikey": KEY})

    assert response.status_code == 200
    assert 'code="200"' in response.text


@respx.mock
def test_getnzb_fetches_url_shaped_guid_directly(client):
    # altHUB (and similar indexers) advertise the download itself as the guid:
    # a full URL with query parameters. Rebuilding that as t=getnzb&id=<url>
    # makes the indexer answer "no such function", so the URL must be fetched
    # verbatim instead.
    url_guid = "https://api.althub.co.za/getnzb/abc.nzb&i=1&r=2"
    rss = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
        "<channel><title>Sample</title>"
        '<newznab:response offset="0" total="1"/>'
        "<item>"
        "<title>URL Guid Item</title>"
        '<guid isPermaLink="false">https://api.althub.co.za/getnzb/abc.nzb&amp;i=1&amp;r=2</guid>'
        "<link>https://api.althub.co.za/details/abc</link>"
        "<pubDate>Mon, 02 Oct 2023 12:00:00 GMT</pubDate>"
        "<category>5040</category>"
        "<size>123456</size>"
        "</item></channel></rss>"
    )
    # search serves the feed with the URL guid; any t=getnzb request would be
    # the bug being fixed, so record it and never serve it.
    newznab_route = respx.get(GEEK, params={"t": "getnzb"}).mock(
        return_value=httpx.Response(200, content=b"wrong-route")
    )
    respx.get(GEEK, params={"t": "search"}).mock(return_value=httpx.Response(200, text=rss))
    respx.get(SLUG).mock(return_value=httpx.Response(200, text="<rss><channel/></rss>"))
    direct = respx.get(url_guid).mock(return_value=httpx.Response(200, content=b"direct-nzb"))

    search = client.get(
        API, params={"t": "search", "q": "ubuntu", "apikey": KEY, "indexer": "nzbgeek"}
    )
    enclosure = _enclosures(search.text)[0]
    guid = dict(httpx.URL(enclosure.get("url")).params)["id"]
    assert guid == f"nzbgeek:{url_guid}"

    url = httpx.URL(enclosure.get("url"))
    response = client.get(url.path, params=dict(url.params))

    assert response.status_code == 200
    assert response.content == b"direct-nzb"
    assert response.headers["content-type"] == "application/x-nzb"
    assert direct.call_count == 1
    assert newznab_route.call_count == 0


@respx.mock
def test_getnzb_direct_url_upstream_error_xml_is_not_streamed(client):
    # A 200 body that is actually a Newznab error document must surface as our
    # own error, never as an application/x-nzb stream.
    url_guid = "https://api.althub.co.za/getnzb/abc.nzb&i=1&r=2"
    respx.get(url_guid).mock(
        return_value=httpx.Response(
            200,
            content=b'<?xml version="1.0"?>\n<error code="202" description="No such function"/>',
            headers={"content-type": "text/xml"},
        )
    )

    response = client.get(
        API,
        params={"t": "getnzb", "id": f"nzbgeek:{url_guid}", "apikey": KEY},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert 'code="202"' in response.text
    assert "No such function" in response.text
    assert response.content != b"direct-nzb"


@respx.mock
def test_getnzb_legacy_non_url_guid_still_uses_newznab_route(client):
    route = respx.get(GEEK, params={"t": "getnzb", "id": "xyz"}).mock(
        return_value=httpx.Response(200, content=b"legacy-nzb")
    )

    response = client.get(API, params={"t": "getnzb", "id": "nzbgeek:xyz", "apikey": KEY})

    assert response.status_code == 200
    assert response.content == b"legacy-nzb"
    assert response.headers["content-type"] == "application/x-nzb"
    assert route.call_count == 1


# --- details -----------------------------------------------------------------


@respx.mock
def test_details_is_proxied_verbatim(client):
    detail_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<rss><channel><item><title>Detail Item</title></item></channel></rss>"
    )
    respx.get(GEEK, params={"t": "details", "id": "xyz"}).mock(
        return_value=httpx.Response(200, text=detail_xml)
    )

    response = client.get(API, params={"t": "details", "id": "nzbgeek:xyz", "apikey": KEY})

    assert response.status_code == 200
    assert response.text == detail_xml
    assert response.headers["content-type"].startswith("application/xml")


def test_details_unknown_indexer_yields_300(client):
    response = client.get(API, params={"t": "details", "id": "nonexistent:xyz", "apikey": KEY})

    assert response.status_code == 200
    assert 'code="300"' in response.text


@respx.mock
def test_details_disabled_indexer_yields_300_without_upstream(client):
    ghost = respx.get("https://ghost.example.com/api").mock(
        return_value=httpx.Response(200, text="<rss/>")
    )

    response = client.get(API, params={"t": "details", "id": "ghost:xyz", "apikey": KEY})

    assert response.status_code == 200
    assert 'code="300"' in response.text
    assert ghost.call_count == 0


@respx.mock
def test_details_404_yields_300(client):
    respx.get(GEEK, params={"t": "details", "id": "xyz"}).mock(
        return_value=httpx.Response(404, text="gone")
    )

    response = client.get(API, params={"t": "details", "id": "nzbgeek:xyz", "apikey": KEY})

    assert response.status_code == 200
    assert 'code="300"' in response.text


@respx.mock
def test_details_200_upstream_error_xml_surfaces_its_code(client):
    respx.get(GEEK, params={"t": "details", "id": "xyz"}).mock(
        return_value=httpx.Response(
            200, content=b'<?xml version="1.0"?>\n<error code="300" description="No such item"/>'
        )
    )

    response = client.get(API, params={"t": "details", "id": "nzbgeek:xyz", "apikey": KEY})

    assert response.status_code == 200
    assert 'code="300"' in response.text
    assert "No such item" in response.text


def test_details_missing_id_yields_200(client):
    response = client.get(API, params={"t": "details", "apikey": KEY})

    assert response.status_code == 200
    assert 'code="200"' in response.text


# --- upstream errors ---------------------------------------------------------


@respx.mock
def test_failing_indexer_is_skipped(client, sample_rss):
    respx.get(GEEK).mock(return_value=httpx.Response(200, text=sample_rss))
    respx.get(SLUG).mock(return_value=httpx.Response(500, text="boom"))

    response = client.get(API, params={"t": "search", "q": "ubuntu", "apikey": KEY})

    assert response.status_code == 200
    assert _guids(response.text) == ["nzbgeek:guid-one", "nzbgeek:guid-two"]
    metrics = client.get("/metrics").text
    assert 'stateless_hydra_indexer_errors_total{indexer="slug"} 1.0' in metrics


@respx.mock
def test_all_indexers_failing_yields_900(client):
    respx.get(GEEK).mock(return_value=httpx.Response(500, text="boom"))
    respx.get(SLUG).mock(return_value=httpx.Response(500, text="boom"))

    response = client.get(API, params={"t": "search", "q": "ubuntu", "apikey": KEY})

    assert response.status_code == 200
    assert 'code="900"' in response.text


# --- dedupe ------------------------------------------------------------------


@respx.mock
def test_dedupe_by_title_drops_cross_indexer_duplicates(settings, fake_redis, sample_rss):
    dedupe_settings = settings.model_copy(update={"dedupe_by_title": True})
    dedupe_app = create_app(dedupe_settings, redis_client=fake_redis)
    respx.get(GEEK).mock(return_value=httpx.Response(200, text=sample_rss))
    respx.get(SLUG).mock(return_value=httpx.Response(200, text=sample_rss))

    with TestClient(dedupe_app) as dedupe_client:
        response = dedupe_client.get(API, params={"t": "search", "q": "ubuntu", "apikey": KEY})

    assert len(_items(response.text)) == 2
    assert _response_total(response.text) == "2"


@respx.mock
def test_dedupe_keeps_items_with_empty_titles(settings, fake_redis):
    dedupe_settings = settings.model_copy(update={"dedupe_by_title": True})
    dedupe_app = create_app(dedupe_settings, redis_client=fake_redis)
    empty_title_feed = (
        '<rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
        '<channel><newznab:response offset="0" total="2"/>'
        '<item><title></title><guid isPermaLink="false">e1</guid>'
        "<link>https://example.com/e1</link>"
        "<pubDate>Mon, 02 Oct 2023 12:00:00 GMT</pubDate><category>5040</category></item>"
        '<item><title></title><guid isPermaLink="false">e2</guid>'
        "<link>https://example.com/e2</link>"
        "<pubDate>Sun, 01 Oct 2023 12:00:00 GMT</pubDate><category>5040</category></item>"
        "</channel></rss>"
    )
    respx.get(GEEK).mock(return_value=httpx.Response(200, text=empty_title_feed))
    respx.get(SLUG).mock(return_value=httpx.Response(200, text=empty_title_feed))

    with TestClient(dedupe_app) as dedupe_client:
        response = dedupe_client.get(API, params={"t": "search", "q": "ubuntu", "apikey": KEY})

    # Empty titles carry no identity: all four items (two per indexer) survive.
    assert len(_items(response.text)) == 4
    assert _response_total(response.text) == "4"


# --- metrics and probes ------------------------------------------------------


@respx.mock
def test_metrics_endpoint_exposes_application_metrics(client, sample_rss):
    respx.get(GEEK).mock(return_value=httpx.Response(200, text=sample_rss))
    respx.get(SLUG).mock(return_value=httpx.Response(200, text=_slug_rss(sample_rss)))

    client.get(API, params={"t": "search", "q": "ubuntu", "apikey": KEY})
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "stateless_hydra_" in response.text
    assert 'stateless_hydra_search_requests_total{function="search"}' in response.text


def test_healthz(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_ok(client):
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readyz_not_ready_when_redis_ping_fails(settings):
    class BrokenRedis:
        async def ping(self):
            raise ConnectionError("redis down")

    broken_app = create_app(settings, redis_client=BrokenRedis())

    with TestClient(broken_app) as broken_client:
        response = broken_client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not ready"}


def test_unhandled_error_returns_newznab_900(settings, fake_redis):
    exploding_app = create_app(settings, redis_client=fake_redis)

    class ExplodingCache:
        async def get(self, *_args, **_kwargs):
            raise RuntimeError("secret internals")

    exploding_app.state.cache = ExplodingCache()

    with TestClient(exploding_app, raise_server_exceptions=False) as exploding_client:
        response = exploding_client.get(API, params={"t": "search", "q": "x", "apikey": KEY})

    assert response.status_code == 200
    assert 'code="900"' in response.text
    assert "secret internals" not in response.text


# --- configuration -----------------------------------------------------------


def test_empty_hydra_api_keys_raises_config_error(tmp_path):
    (tmp_path / "indexers.yaml").write_text(
        "indexers:\n  - name: ix\n    host: https://ix.example.com\n    apiKeyRef: anything\n",
        encoding="utf-8",
    )
    (tmp_path / "api-keys.yaml").write_text("hydraApiKeys: []\n", encoding="utf-8")
    settings = AppSettings(
        indexers_file=str(tmp_path / "indexers.yaml"),
        api_keys_file=str(tmp_path / "api-keys.yaml"),
    )

    with pytest.raises(ConfigError):
        create_app(settings, redis_client=aioredis.FakeRedis())
