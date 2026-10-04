"""Configuration models and loaders for stateless-hydra.

Configuration comes from two sources:

* YAML files (``app.yaml``, ``indexers.yaml``, ``api-keys.yaml``)
* environment variables prefixed with ``SH_``

For :class:`AppSettings`, YAML values form the base and ``SH_*`` environment
variables override them. Loading never touches the network or local state, and
all failures raise :class:`~stateless_hydra.exceptions.ConfigError` with a clear
message so the process can fail fast at startup.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .exceptions import ConfigError

_VALID_SEARCH_TYPES = {"search", "tvsearch", "movie", "music", "book"}
_RESET_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ProxyConfig(BaseModel):
    """HTTP(S) proxy settings."""

    url: str | None = None  # full URL e.g. "http://proxy.corp:3128"


class IndexerConfig(BaseModel):
    """Configuration for a single Usenet indexer."""

    model_config = ConfigDict(populate_by_name=True)

    name: str  # unique, used as indexer id everywhere
    enabled: bool = True
    host: str  # scheme + host, no trailing slash, e.g. https://api.nzbgeek.info
    api_path: str = Field(default="/api", alias="apiPath")
    api_key_ref: str = Field(alias="apiKeyRef")  # name looked up in api-keys file
    api_hit_limit: int = Field(default=0, alias="apiHitLimit", ge=0)  # 0 = unlimited
    nzb_pull_limit: int = Field(default=0, alias="nzbPullLimit", ge=0)  # 0 = unlimited
    reset_time: str = Field(default="00:00", alias="resetTime")  # HH:MM, validated
    reset_timezone: str = Field(default="UTC", alias="resetTimezone")  # IANA name, validated
    timeout_seconds: float = Field(default=30.0, alias="timeoutSeconds", gt=0)
    # None -> global default; <=0 -> disabled
    cache_ttl_seconds: int | None = Field(default=None, alias="cacheTtlSeconds")
    search_types: list[str] = Field(default_factory=lambda: ["search"], alias="searchTypes")
    categories: list[int] | None = Field(default=None, alias="categories")  # None -> all
    proxy_url: str | None = Field(default=None, alias="proxyUrl")  # per-indexer override

    @field_validator("reset_time")
    @classmethod
    def _validate_reset_time(cls, value: str) -> str:
        if _RESET_TIME_PATTERN.fullmatch(value) is None:
            raise ValueError("reset_time must be in strict HH:MM 24-hour format")
        return value

    @field_validator("reset_timezone")
    @classmethod
    def _validate_reset_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value

    @field_validator("search_types")
    @classmethod
    def _validate_search_types(cls, value: list[str]) -> list[str]:
        invalid = [item for item in value if item not in _VALID_SEARCH_TYPES]
        if invalid:
            allowed = ", ".join(sorted(_VALID_SEARCH_TYPES))
            raise ValueError(f"invalid search types {invalid}; allowed values: {allowed}")
        return value


class ApiKeysFile(BaseModel):
    """Contents of the ``api-keys.yaml`` secret file."""

    model_config = ConfigDict(populate_by_name=True)

    api_keys: dict[str, str] = Field(default_factory=dict, alias="apiKeys")  # refs -> keys
    hydra_api_keys: list[str] = Field(default_factory=list, alias="hydraApiKeys")  # client keys


class AppSettings(BaseSettings):
    """Application-wide settings from YAML plus ``SH_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="SH_", extra="ignore")

    log_level: str = "INFO"
    user_agent: str = "stateless-hydra/0.1.0"
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = 900
    dedupe_by_title: bool = False
    max_results_per_indexer: int = 100
    global_proxy_url: str | None = None
    host: str = "0.0.0.0"
    port: int = 5076
    app_config: str = "/config/app.yaml"
    indexers_file: str = "/config/indexers.yaml"
    api_keys_file: str = "/config/api-keys.yaml"


def _env_overrides() -> dict[str, str]:
    """Return explicitly-set ``SH_*`` environment values keyed by field name."""
    overrides: dict[str, str] = {}
    for field_name in AppSettings.model_fields:
        env_key = f"SH_{field_name.upper()}"
        if env_key in os.environ:
            overrides[field_name] = os.environ[env_key]
    return overrides


def _read_yaml_mapping(path: Path, description: str) -> dict:
    """Read a YAML file and require that it contains a mapping (or is empty)."""
    if not path.is_file():
        raise ConfigError(f"{description} not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read {description} {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {description} {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{description} {path} must contain a YAML mapping")
    return data


def load_settings(app_config: str | None = None) -> AppSettings:
    """Load :class:`AppSettings`.

    YAML values from ``app_config`` form the base; ``SH_*`` environment
    variables always win. When ``app_config`` is ``None`` the configured
    default path (``app_config`` field) is used. A missing file is not an
    error: environment variables and defaults are used instead.
    """
    try:
        defaults = AppSettings()
    except ValidationError as exc:
        raise ConfigError(f"invalid application settings (environment): {exc}") from exc

    path = Path(app_config) if app_config is not None else Path(defaults.app_config)

    yaml_data: dict = {}
    if path.is_file():
        yaml_data = _read_yaml_mapping(path, "app config file")

    merged = {**yaml_data, **_env_overrides()}
    try:
        return AppSettings(**merged)
    except ValidationError as exc:
        raise ConfigError(f"invalid application settings: {exc}") from exc


def load_indexers(path: str) -> list[IndexerConfig]:
    """Load indexer definitions from ``{indexers: [...]}`` YAML."""
    data = _read_yaml_mapping(Path(path), "indexers config file")
    if "indexers" not in data:
        raise ConfigError(f"indexers config file {path} must contain an 'indexers' key")
    raw = data["indexers"]
    if not isinstance(raw, list):
        raise ConfigError(f"indexers config file {path} must have an 'indexers' list")

    indexers: list[IndexerConfig] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError(f"each entry under 'indexers' in {path} must be a mapping")
        try:
            indexer = IndexerConfig.model_validate(item)
        except ValidationError as exc:
            raise ConfigError(f"invalid indexer configuration in {path}: {exc}") from exc
        if indexer.name in seen:
            raise ConfigError(f"duplicate indexer name {indexer.name!r} in {path}")
        seen.add(indexer.name)
        indexers.append(indexer)
    return indexers


def load_api_keys(path: str) -> ApiKeysFile:
    """Load the ``api-keys.yaml`` secret file."""
    data = _read_yaml_mapping(Path(path), "API keys file")
    try:
        return ApiKeysFile.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid API keys file {path}: {exc}") from exc


def resolve_indexer_key(indexer: IndexerConfig, api_keys: ApiKeysFile) -> str:
    """Resolve an indexer's ``apiKeyRef`` to the secret key value."""
    try:
        return api_keys.api_keys[indexer.api_key_ref]
    except KeyError as exc:
        raise ConfigError(
            f"API key reference {indexer.api_key_ref!r} for indexer {indexer.name!r} "
            f"not found in api-keys file"
        ) from exc


def setup_logging(settings: AppSettings) -> None:
    """Configure stdlib logging to stderr at ``settings.log_level``."""
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
        force=True,
    )
