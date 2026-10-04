"""Tests for the configuration layer.

All YAML fixtures are written inline into ``tmp_path`` so tests never depend on
files under ``config/`` (which a later task owns).
"""

import pytest

from stateless_hydra.config import (
    AppSettings,
    load_api_keys,
    load_indexers,
    load_settings,
    resolve_indexer_key,
)
from stateless_hydra.exceptions import ConfigError

FULL_INDEXERS_YAML = """
indexers:
  - name: nzbgeek
    enabled: true
    host: https://api.nzbgeek.info
    apiPath: /api
    apiKeyRef: nzbgeek
    apiHitLimit: 1000
    nzbPullLimit: 500
    resetTime: "12:30"
    resetTimezone: Europe/Berlin
    timeoutSeconds: 15
    cacheTtlSeconds: 60
    searchTypes: [search, tvsearch]
    categories: [2000, 5000]
    proxyUrl: http://proxy.corp:3128
"""

MINIMAL_INDEXERS_YAML = """
indexers:
  - name: minimalist
    host: https://api.example.invalid
    apiKeyRef: minimal
"""

DUPLICATE_INDEXERS_YAML = """
indexers:
  - name: nzbgeek
    host: https://api.nzbgeek.info
    apiKeyRef: nzbgeek
  - name: nzbgeek
    host: https://api.other.invalid
    apiKeyRef: other
"""

BAD_RESET_TIME_YAML = """
indexers:
  - name: bad
    host: https://api.example.invalid
    apiKeyRef: bad
    resetTime: "25:99"
"""

BAD_TIMEZONE_YAML = """
indexers:
  - name: bad
    host: https://api.example.invalid
    apiKeyRef: bad
    resetTimezone: Mars/Olympus
"""

UNKNOWN_TOP_LEVEL_KEYS_YAML = """
someUnknownKey: ignored
indexers:
  - name: anzb
    host: https://api.example.invalid
    apiKeyRef: anzb
"""

MISSING_INDEXERS_KEY_YAML = """
someUnknownKey: ignored
"""

NON_LIST_INDEXERS_YAML = """
indexers: not-a-list
"""

EMPTY_INDEXERS_YAML = """
indexers: []
"""

API_KEYS_YAML = """
apiKeys:
  nzbgeek: secret-key-123
  minimal: minimal-secret
hydraApiKeys:
  - client-key-1
  - client-key-2
"""


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_full_indexers_yaml_loads_all_fields(tmp_path):
    path = _write(tmp_path, "indexers.yaml", FULL_INDEXERS_YAML)
    indexers = load_indexers(path)

    assert len(indexers) == 1
    indexer = indexers[0]
    assert indexer.name == "nzbgeek"
    assert indexer.enabled is True
    assert indexer.host == "https://api.nzbgeek.info"
    assert indexer.api_path == "/api"
    assert indexer.api_key_ref == "nzbgeek"
    assert indexer.api_hit_limit == 1000
    assert indexer.nzb_pull_limit == 500
    assert indexer.reset_time == "12:30"
    assert indexer.reset_timezone == "Europe/Berlin"
    assert indexer.timeout_seconds == 15
    assert indexer.cache_ttl_seconds == 60
    assert indexer.search_types == ["search", "tvsearch"]
    assert indexer.categories == [2000, 5000]
    assert indexer.proxy_url == "http://proxy.corp:3128"


def test_indexer_defaults_applied(tmp_path):
    path = _write(tmp_path, "indexers.yaml", MINIMAL_INDEXERS_YAML)
    indexer = load_indexers(path)[0]

    assert indexer.enabled is True
    assert indexer.api_path == "/api"
    assert indexer.api_hit_limit == 0
    assert indexer.nzb_pull_limit == 0
    assert indexer.reset_time == "00:00"
    assert indexer.reset_timezone == "UTC"
    assert indexer.timeout_seconds == 30.0
    assert indexer.cache_ttl_seconds is None
    assert indexer.search_types == ["search"]
    assert indexer.categories is None
    assert indexer.proxy_url is None


def test_unknown_top_level_keys_are_ignored(tmp_path):
    path = _write(tmp_path, "indexers.yaml", UNKNOWN_TOP_LEVEL_KEYS_YAML)
    indexers = load_indexers(path)
    assert [indexer.name for indexer in indexers] == ["anzb"]


def test_missing_indexers_key_raises_config_error(tmp_path):
    path = _write(tmp_path, "indexers.yaml", MISSING_INDEXERS_KEY_YAML)
    with pytest.raises(ConfigError):
        load_indexers(path)


def test_non_list_indexers_raises_config_error(tmp_path):
    path = _write(tmp_path, "indexers.yaml", NON_LIST_INDEXERS_YAML)
    with pytest.raises(ConfigError):
        load_indexers(path)


def test_empty_indexers_list_loads_to_empty(tmp_path):
    path = _write(tmp_path, "indexers.yaml", EMPTY_INDEXERS_YAML)
    assert load_indexers(path) == []


def test_invalid_reset_time_raises_config_error(tmp_path):
    path = _write(tmp_path, "indexers.yaml", BAD_RESET_TIME_YAML)
    with pytest.raises(ConfigError):
        load_indexers(path)


@pytest.mark.parametrize("value", ["1:30", " 12:30 ", "24:00", "+1:30", "12:60"])
def test_reset_time_strict_format_rejects_invalid(tmp_path, value):
    path = _write(
        tmp_path,
        "indexers.yaml",
        f"indexers:\n  - name: bad\n    host: https://api.example.invalid\n"
        f'    apiKeyRef: bad\n    resetTime: "{value}"\n',
    )
    with pytest.raises(ConfigError):
        load_indexers(path)


def test_invalid_reset_timezone_raises_config_error(tmp_path):
    path = _write(tmp_path, "indexers.yaml", BAD_TIMEZONE_YAML)
    with pytest.raises(ConfigError):
        load_indexers(path)


def test_duplicate_indexer_names_raise_config_error(tmp_path):
    path = _write(tmp_path, "indexers.yaml", DUPLICATE_INDEXERS_YAML)
    with pytest.raises(ConfigError):
        load_indexers(path)


def test_missing_indexers_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_indexers(str(tmp_path / "does-not-exist.yaml"))


def test_resolve_indexer_key_missing_ref_raises_config_error(tmp_path):
    indexers_path = _write(tmp_path, "indexers.yaml", MINIMAL_INDEXERS_YAML)
    api_keys_path = _write(tmp_path, "api-keys.yaml", "apiKeys: {}\n")

    indexer = load_indexers(indexers_path)[0]
    api_keys = load_api_keys(api_keys_path)

    with pytest.raises(ConfigError):
        resolve_indexer_key(indexer, api_keys)


def test_resolve_indexer_key_returns_secret(tmp_path):
    indexers_path = _write(tmp_path, "indexers.yaml", MINIMAL_INDEXERS_YAML)
    api_keys_path = _write(tmp_path, "api-keys.yaml", API_KEYS_YAML)

    indexer = load_indexers(indexers_path)[0]
    api_keys = load_api_keys(api_keys_path)

    assert resolve_indexer_key(indexer, api_keys) == "minimal-secret"


def test_api_keys_file_with_hydra_keys_loads(tmp_path):
    path = _write(tmp_path, "api-keys.yaml", API_KEYS_YAML)
    api_keys = load_api_keys(path)

    assert api_keys.api_keys == {"nzbgeek": "secret-key-123", "minimal": "minimal-secret"}
    assert api_keys.hydra_api_keys == ["client-key-1", "client-key-2"]


def test_missing_api_keys_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_api_keys(str(tmp_path / "missing-api-keys.yaml"))


def _clear_sh_env(monkeypatch):
    for field_name in AppSettings.model_fields:
        monkeypatch.delenv(f"SH_{field_name.upper()}", raising=False)


def test_settings_defaults_when_nothing_set(tmp_path, monkeypatch):
    _clear_sh_env(monkeypatch)
    settings = load_settings(str(tmp_path / "missing-app.yaml"))

    assert settings.log_level == "INFO"
    assert settings.user_agent == "stateless-hydra/0.1.0"
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.cache_ttl_seconds == 900
    assert settings.dedupe_by_title is False
    assert settings.max_results_per_indexer == 100
    assert settings.global_proxy_url is None
    assert settings.host == "0.0.0.0"
    assert settings.port == 5076


def test_settings_yaml_values_load(tmp_path, monkeypatch):
    _clear_sh_env(monkeypatch)
    path = _write(
        tmp_path,
        "app.yaml",
        "log_level: DEBUG\nport: 6000\nredis_url: redis://redis:6379/2\n",
    )
    settings = load_settings(path)

    assert settings.log_level == "DEBUG"
    assert settings.port == 6000
    assert settings.redis_url == "redis://redis:6379/2"


def test_env_overrides_yaml(tmp_path, monkeypatch):
    _clear_sh_env(monkeypatch)
    path = _write(
        tmp_path,
        "app.yaml",
        "port: 6000\nlog_level: DEBUG\nmax_results_per_indexer: 50\n",
    )
    monkeypatch.setenv("SH_PORT", "9999")
    monkeypatch.setenv("SH_LOG_LEVEL", "WARNING")

    settings = load_settings(path)

    assert settings.port == 9999
    assert settings.log_level == "WARNING"
    assert settings.max_results_per_indexer == 50


def test_invalid_env_value_raises_config_error(tmp_path, monkeypatch):
    _clear_sh_env(monkeypatch)
    monkeypatch.setenv("SH_PORT", "notanint")

    with pytest.raises(ConfigError):
        load_settings(str(tmp_path / "missing-app.yaml"))
