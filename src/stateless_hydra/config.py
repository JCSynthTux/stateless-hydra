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
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .exceptions import ConfigError
from .newznab import MAX_FEED_ITEMS

# Minimum length required of the NZB token signing key. The key is HMAC key
# material; a short/guessable key would let a client forge download tokens.
_MIN_SIGNING_KEY_LENGTH = 32

# Default ceiling for an upstream response body. A malicious or broken indexer
# can stream arbitrarily many bytes; buffering them unbounded would let a few
# large responses exhaust the pod's 256Mi memory limit. 16 MiB is generous for
# a Newznab RSS page or an NZB payload while keeping the worst case bounded.
DEFAULT_MAX_UPSTREAM_BODY_BYTES = 16 * 1024 * 1024

# Default location of the YAML app configuration. Also the ``app_config`` field
# default, kept as a module constant so ``load_settings`` can locate the file
# before constructing (and validating) any settings object.
_DEFAULT_APP_CONFIG = "/config/app.yaml"

_VALID_SEARCH_TYPES = {"search", "tvsearch", "movie", "music", "book"}
_RESET_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Placeholder prefix used by the shipped secret templates. Any api-keys value
# that is this literal, or starts with it (``CHANGE_ME_BEFORE_DEPLOY``), is
# rejected: the templates are public, so a service booted with an unreplaced
# placeholder would accept a publicly known client API key. Matching is
# case-insensitive, so ``change_me`` cannot slip through. Deliberately narrow:
# short or "test"-style values are legitimate operator choices.
_PLACEHOLDER_PREFIX = "CHANGE_ME"


def _is_placeholder_secret(value: str) -> bool:
    """Return True when ``value`` is a shipped-template placeholder."""
    return value.strip().upper().startswith(_PLACEHOLDER_PREFIX)


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
    # True -> always rebuild t=<downloadFunction>&id=<guid>, even for URL-shaped
    # guids. False -> fetch URL-shaped guids directly (altHUB-style .nzb URLs).
    force_getnzb_rebuild: bool = Field(default=False, alias="forceGetnzbRebuild")
    # Upstream Newznab function used by the rebuild path. "getnzb" is the
    # standard function; classic nZEDb/Newznab indexers (drunkenSlug, altHUB)
    # only implement "get".
    download_function: Literal["getnzb", "get"] = Field(default="getnzb", alias="downloadFunction")
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
    # HMAC key material for NZB download tokens, carried inside the same secret
    # file so a Kubernetes Secret can ship both the indexer keys and the signing
    # key in one mounted volume. An app.yaml value or SH_NZB_TOKEN_SIGNING_KEY
    # environment variable overrides it (see resolve_nzb_token_signing_key).
    nzb_token_signing_key: str | None = Field(default=None, alias="nzbTokenSigningKey")

    @field_validator("hydra_api_keys")
    @classmethod
    def _reject_placeholder_hydra_api_keys(cls, value: list[str]) -> list[str]:
        """Refuse client API keys left at a shipped-template placeholder.

        An unreplaced placeholder is public knowledge, so accepting it would
        make the sole auth factor on ``/api`` trivially guessable.
        """
        offenders = [index for index, key in enumerate(value) if _is_placeholder_secret(key)]
        if offenders:
            raise ValueError(
                "hydraApiKeys entries at index "
                f"{offenders} are unreplaced placeholders ({_PLACEHOLDER_PREFIX}...); "
                "replace every placeholder with a real random client API key"
            )
        return value

    @field_validator("api_keys")
    @classmethod
    def _reject_placeholder_api_keys(cls, value: dict[str, str]) -> dict[str, str]:
        """Refuse indexer API key values left at a shipped-template placeholder.

        Only the values are secrets; the dict keys are ``apiKeyRef`` names.
        Offending *refs* are reported rather than values so a real key is never
        echoed into a startup error or the logs.
        """
        offenders = [ref for ref, key in value.items() if _is_placeholder_secret(key)]
        if offenders:
            raise ValueError(
                "apiKeys values for refs "
                f"{offenders} are unreplaced placeholders ({_PLACEHOLDER_PREFIX}...); "
                "replace every placeholder with the real upstream indexer API key"
            )
        return value


class AppSettings(BaseSettings):
    """Application-wide settings from YAML plus ``SH_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="SH_", extra="ignore")

    log_level: str = "INFO"
    user_agent: str = "stateless-hydra/0.1.0"
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = 900
    dedupe_by_title: bool = False
    max_results_per_indexer: int = 100
    # Hard ceiling on bytes buffered from any single upstream response. Applies
    # to every indexer search/details/NZB fetch (and every redirect hop).
    max_upstream_body_bytes: int = DEFAULT_MAX_UPSTREAM_BODY_BYTES
    # Per-client daily envelopes over the shared per-indexer budgets. 0 means
    # unlimited (and writes no Redis keys). They reset at UTC midnight and are
    # keyed by a hash of the presented client API key. Without them one
    # authenticated client could burn a shared indexer budget for every peer.
    per_client_api_hit_limit: int = Field(default=0, ge=0)
    per_client_nzb_pull_limit: int = Field(default=0, ge=0)
    global_proxy_url: str | None = None
    host: str = "0.0.0.0"
    port: int = 5076
    # Optional externally reachable base URL (e.g. the ingress hostname). When
    # set, advertised download links are built from it instead of the request's
    # Host header, which the client controls. Must be http(s) with a host and
    # may carry a path; a query, fragment, userinfo, or invalid port would be
    # spliced into every advertised link, so those are rejected. Trailing
    # slashes are stripped so links are built as base + path.
    public_base_url: str | None = None
    app_config: str = _DEFAULT_APP_CONFIG
    indexers_file: str = "/config/indexers.yaml"
    api_keys_file: str = "/config/api-keys.yaml"
    # Override for the secret HMAC key used to sign deterministic NZB download
    # tokens. It is optional: when empty the ``nzbTokenSigningKey`` field of the
    # api-keys.yaml secret file is used instead (the primary source in the
    # shipped Kubernetes manifests). ``resolve_nzb_token_signing_key`` applies
    # the final precedence (this value > api-keys file > fail fast) once the
    # api-keys file has been read in ``create_app``.
    nzb_token_signing_key: str = ""

    @field_validator("nzb_token_signing_key")
    @classmethod
    def _strip_nzb_token_signing_key(cls, value: str) -> str:
        # Store the stripped value: whitespace is not part of the key material,
        # and the length check below must agree with what tokens.py signs with.
        return value.strip()

    @field_validator("max_results_per_indexer")
    @classmethod
    def _validate_max_results_per_indexer(cls, value: int) -> int:
        # The feed parser refuses an upstream response with more than
        # MAX_FEED_ITEMS items, so requesting more can only waste upstream
        # quota: a compliant indexer's 1000-item page would be discarded whole
        # and the search would degrade to the catch-up scale, not the requested
        # count. Refuse the misconfiguration at startup instead.
        if value < 1:
            raise ValueError(
                "max_results_per_indexer must be at least 1; set it via "
                "SH_MAX_RESULTS_PER_INDEXER or the max_results_per_indexer field "
                "in app.yaml"
            )
        if value > MAX_FEED_ITEMS:
            raise ValueError(
                f"max_results_per_indexer must not exceed the {MAX_FEED_ITEMS}-item "
                "feed cap (the upstream feed parser refuses larger responses); set "
                "it via SH_MAX_RESULTS_PER_INDEXER or the max_results_per_indexer "
                "field in app.yaml"
            )
        return value

    @field_validator("max_upstream_body_bytes")
    @classmethod
    def _validate_max_upstream_body_bytes(cls, value: int) -> int:
        # A non-positive cap would reject every response (or disable the guard
        # entirely, depending on the comparison), so refuse it at startup.
        if value <= 0:
            raise ValueError(
                "max_upstream_body_bytes must be greater than 0; set it via "
                "SH_MAX_UPSTREAM_BODY_BYTES or the max_upstream_body_bytes field "
                "in app.yaml"
            )
        return value

    @field_validator("public_base_url")
    @classmethod
    def _validate_public_base_url(cls, value: str | None) -> str | None:
        # Links are built from this base, so it must be a well-formed absolute
        # HTTP(S) URL. An empty value is treated as "unset"; whitespace is
        # rejected rather than silently trimmed so a malformed value (e.g. a
        # stray space or trailing junk) fails fast at startup instead of
        # producing broken download links.
        if not value:
            return None
        if any(char.isspace() for char in value):
            raise ValueError(
                "public_base_url must not contain whitespace; set it via "
                "SH_PUBLIC_BASE_URL or the public_base_url field in app.yaml"
            )
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise ValueError(
                f"public_base_url is not a valid URL: {value!r}; set it via "
                "SH_PUBLIC_BASE_URL or the public_base_url field in app.yaml"
            ) from exc
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "public_base_url must be an absolute http:// or https:// URL "
                "with a host (e.g. https://hydra.example.com); set it via "
                "SH_PUBLIC_BASE_URL or the public_base_url field in app.yaml"
            )
        # ``netloc`` alone is not enough: ``https://:8080`` has one but no host
        # at all, and links built from it would be unusable.
        if not parsed.hostname:
            raise ValueError(
                "public_base_url must name a host, not just a port or userinfo "
                "(e.g. https://hydra.example.com); set it via "
                "SH_PUBLIC_BASE_URL or the public_base_url field in app.yaml"
            )
        # A query or fragment would be spliced verbatim into every advertised
        # download link, so a base carrying one is a misconfiguration.
        if parsed.query or parsed.fragment:
            raise ValueError(
                "public_base_url must not contain a query string or fragment; "
                "set it via SH_PUBLIC_BASE_URL or the public_base_url field in "
                "app.yaml"
            )
        # Credentials in the base would leak into every advertised link (and
        # would not authenticate anything), so reject userinfo outright.
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            raise ValueError(
                "public_base_url must not contain userinfo (user:password@); "
                "set it via SH_PUBLIC_BASE_URL or the public_base_url field in "
                "app.yaml"
            )
        # ``urlsplit`` accepts a non-numeric port; ``.port`` is what validates it
        # and raises ValueError on garbage.
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError(
                f"public_base_url has an invalid port: {value!r}; set it via "
                "SH_PUBLIC_BASE_URL or the public_base_url field in app.yaml"
            ) from exc
        return value.rstrip("/")

    @model_validator(mode="after")
    def _validate_nzb_token_signing_key(self) -> AppSettings:
        # An empty value is allowed here: the api-keys.yaml file (the primary
        # source) has not been read yet, and create_app resolves the effective
        # key from both sources. Only a *provided but too short* value is
        # rejected immediately, so a truncated override fails fast at load time.
        if 0 < len(self.nzb_token_signing_key) < _MIN_SIGNING_KEY_LENGTH:
            raise ValueError(
                "nzb_token_signing_key must be at least "
                f"{_MIN_SIGNING_KEY_LENGTH} characters; set the "
                "nzbTokenSigningKey field in the api-keys.yaml secret file, or "
                "override it with the SH_NZB_TOKEN_SIGNING_KEY environment "
                "variable"
            )
        return self


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
    """Load :class:`AppSettings` from an app YAML file plus ``SH_*`` env vars.

    Path resolution: an explicit ``app_config`` argument wins; otherwise the
    ``SH_APP_CONFIG`` environment variable is honored; otherwise the documented
    default path (``/config/app.yaml``) is used. A missing file is not an error
    -- defaults and environment variables are used instead.

    The resolved YAML mapping and the explicitly-set ``SH_*`` environment
    variables are merged (environment values override YAML), then validated
    exactly once by constructing :class:`AppSettings`. A signing key supplied
    only in the YAML file is accepted, as is an environment-only key. The final
    check -- including falling back to the ``nzbTokenSigningKey`` field of the
    api-keys.yaml secret file and failing fast when no source provides a key --
    happens in ``create_app`` via :func:`resolve_nzb_token_signing_key` once that
    file has been read.
    """
    if app_config is not None:
        path = Path(app_config)
    else:
        path = Path(os.environ.get("SH_APP_CONFIG", _DEFAULT_APP_CONFIG))

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


def resolve_nzb_token_signing_key(settings_key: str, api_keys_key: str | None) -> str:
    """Resolve the NZB token signing key from its two possible sources.

    Precedence: an explicitly configured ``settings_key`` (the
    ``nzb_token_signing_key`` value from app.yaml or the
    ``SH_NZB_TOKEN_SIGNING_KEY`` environment variable) wins; when it is empty
    the ``nzbTokenSigningKey`` field of the api-keys.yaml secret file is used.
    Both values are stripped before the length check so leading/trailing
    whitespace never counts as key material. Fails fast with
    :class:`~stateless_hydra.exceptions.ConfigError` naming both sources when
    neither provides at least ``_MIN_SIGNING_KEY_LENGTH`` characters.
    """
    explicit = settings_key.strip()
    from_api_keys = (api_keys_key or "").strip()
    effective = explicit or from_api_keys
    if len(effective) < _MIN_SIGNING_KEY_LENGTH:
        raise ConfigError(
            "NZB token signing key is missing or shorter than "
            f"{_MIN_SIGNING_KEY_LENGTH} characters; set the nzbTokenSigningKey "
            "field in the api-keys.yaml secret file (e.g. "
            "k8s/secret-api-keys.yaml, mounted at /config/api-keys.yaml), or "
            "override it with the SH_NZB_TOKEN_SIGNING_KEY environment variable"
        )
    return effective


def resolve_indexer_key(indexer: IndexerConfig, api_keys: ApiKeysFile) -> str:
    """Resolve an indexer's ``apiKeyRef`` to the secret key value."""
    try:
        return api_keys.api_keys[indexer.api_key_ref]
    except KeyError as exc:
        raise ConfigError(
            f"API key reference {indexer.api_key_ref!r} for indexer {indexer.name!r} "
            f"not found in api-keys file"
        ) from exc


_APIKEY_QUERY_PATTERN = re.compile(r"(apikey=)[^&\s\"']+", re.IGNORECASE)


class _ApiKeyRedactionFilter(logging.Filter):
    """Scrub ``apikey`` values from log records.

    Uvicorn's access logger writes the full request line, query string
    included, so a client's ``apikey`` would otherwise be written to
    stdout/stderr (and on to any log shipper) in cleartext on every request.
    The filter rewrites the record's arguments/message before formatting. It
    also protects the app's own records if they ever embed a request URL.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: self._redact(value) for key, value in record.args.items()}
        elif isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        return True

    @staticmethod
    def _redact(value: object) -> object:
        if isinstance(value, str):
            return _APIKEY_QUERY_PATTERN.sub(r"\1REDACTED", value)
        return value


def _install_access_log_redaction() -> None:
    """Attach the apikey-scrubbing filter to uvicorn's access logger once."""
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(existing, _ApiKeyRedactionFilter) for existing in access_logger.filters):
        access_logger.addFilter(_ApiKeyRedactionFilter())


def setup_logging(settings: AppSettings, *, force: bool = False) -> None:
    """Configure stdlib logging to stderr at ``settings.log_level``.

    ``force`` is passed through to :func:`logging.basicConfig`. It defaults to
    ``False`` so that importing/building the app never clobbers an existing
    logging configuration (uvicorn or pytest); process-owning entry points such
    as ``python -m stateless_hydra`` may opt into forcing.

    ``httpx``/``httpcore`` are pinned to ``WARNING``: at ``INFO`` httpx logs the
    full request URL, which would leak upstream indexer API keys into the logs.
    Uvicorn's access logger is additionally filtered so client ``apikey`` query
    values never reach the logs (see :class:`_ApiKeyRedactionFilter`).
    """
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
        force=force,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _install_access_log_redaction()
