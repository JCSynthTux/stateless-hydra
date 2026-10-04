"""Shared pytest fixtures for the stateless-hydra test suite.

These fixtures build temporary YAML config files and a ``fakeredis``-backed
application, so no test ever needs a live Redis or a real indexer. They are
module-global because the integration tests exercise the assembled FastAPI app.
"""

from __future__ import annotations

import pytest
from fakeredis import aioredis
from fastapi.testclient import TestClient

from stateless_hydra.config import AppSettings
from stateless_hydra.main import create_app

INDEXERS_YAML = """
indexers:
  - name: nzbgeek
    enabled: true
    host: https://nzbgeek.example.com
    apiPath: /api
    apiKeyRef: geek_key
    apiHitLimit: 2
    nzbPullLimit: 1
    searchTypes: [search, tvsearch]
  - name: slug
    enabled: true
    host: https://slug.example.com
    apiPath: /api
    apiKeyRef: slug_key
    apiHitLimit: 0
    nzbPullLimit: 0
    searchTypes: [search]
  - name: ghost
    enabled: false
    host: https://ghost.example.com
    apiPath: /api
    apiKeyRef: ghost_key
    searchTypes: [search]
"""

API_KEYS_YAML = """
apiKeys:
  geek_key: geek-secret
  slug_key: slug-secret
  ghost_key: ghost-secret
hydraApiKeys:
  - test-key
"""

APP_YAML = """
log_level: WARNING
"""

# A well-formed Newznab/Torznab RSS feed: two items with namespaces, pubDates,
# categories, sizes and torznab attributes, plus a channel-level response.
SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"
     xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>Sample</title>
    <newznab:response offset="0" total="2"/>
    <item>
      <title>Sample One</title>
      <guid isPermaLink="false">guid-one</guid>
      <link>https://nzbgeek.example.com/details/guid-one</link>
      <pubDate>Mon, 02 Oct 2023 12:00:00 GMT</pubDate>
      <category>5040</category>
      <size>123456</size>
      <description>First sample</description>
      <torznab:attr name="seeders" value="10"/>
    </item>
    <item>
      <title>Sample Two</title>
      <guid isPermaLink="false">guid-two</guid>
      <link>https://nzbgeek.example.com/details/guid-two</link>
      <pubDate>Sun, 01 Oct 2023 12:00:00 GMT</pubDate>
      <category>2040</category>
      <size>654321</size>
      <description>Second sample</description>
      <torznab:attr name="seeders" value="5"/>
    </item>
  </channel>
</rss>
"""


@pytest.fixture
def test_dir(tmp_path):
    """A directory holding valid indexers.yaml / api-keys.yaml / app.yaml."""
    (tmp_path / "indexers.yaml").write_text(INDEXERS_YAML, encoding="utf-8")
    (tmp_path / "api-keys.yaml").write_text(API_KEYS_YAML, encoding="utf-8")
    (tmp_path / "app.yaml").write_text(APP_YAML, encoding="utf-8")
    return tmp_path


@pytest.fixture
def settings(test_dir) -> AppSettings:
    """Application settings pointing at the temporary config files."""
    return AppSettings(
        indexers_file=str(test_dir / "indexers.yaml"),
        api_keys_file=str(test_dir / "api-keys.yaml"),
        user_agent="stateless-hydra-test/0.0",
        cache_ttl_seconds=60,
        dedupe_by_title=False,
        max_results_per_indexer=10,
        log_level="CRITICAL",
    )


@pytest.fixture
def fake_redis():
    """An in-process fakeredis client (async, like the app uses)."""
    return aioredis.FakeRedis()


@pytest.fixture
def sample_rss() -> str:
    """The shared two-item Newznab/Torznab sample feed."""
    return SAMPLE_RSS


@pytest.fixture
def app(settings, fake_redis):
    """A configured FastAPI app backed by ``fake_redis``."""
    return create_app(settings, redis_client=fake_redis)


@pytest.fixture
def client(app):
    """A TestClient running the app lifespan (no live services required)."""
    with TestClient(app) as test_client:
        yield test_client
