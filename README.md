# stateless-hydra

**A stateless, Kubernetes-native replacement for the nzbhydra2 external API.**

stateless-hydra emulates the nzbhydra2 external API — the Newznab/Torznab HTTP
interface that Sonarr, Radarr, Lidarr, Readarr and similar tools already
speak — but implements it as a stateless proxy/aggregator. It queries multiple
Usenet indexers, normalizes and merges their results, and exposes a single
Newznab endpoint to clients. There is no WebUI and no local database: all
runtime state lives in Redis.

> This project is under active development. The configuration, deployment and
> container contracts below are stable; refer to `AGENTS.md` for the full
> project rules.

## Features

- **Stateless** — no sqlite, no local files, no database. Every pod can be
  killed and recreated at any time, and horizontally scaled.
- **Redis-backed cache and limits** — search responses are cached in Redis and
  the per-indexer daily budgets live there too, so limits are shared across
  replicas.
- **Per-indexer daily limits** — separate API-hit and NZB-pull budgets, each
  with a configurable reset time and IANA timezone (default `00:00` UTC).
  `0` means unlimited.
- **Configuration as data** — app and indexer settings are read from YAML
  ConfigMaps; credentials come only from a Secret. Environment variables
  prefixed with `SH_` override file values.
- **Configurable User-Agent** — set globally, because some indexers rate-limit
  or block unknown agents.
- **Global and per-indexer proxies** — route all indexer traffic through an
  HTTP(S) or SOCKS5 proxy, or override it for a single indexer.
- **Prometheus metrics** — counters and histograms under the `stateless_hydra_`
  prefix, scraped from `/metrics`.
- **Newznab/Torznab compatible** — `caps`, `search`, `tvsearch`, `movie`,
  `music`, `book`, `details` and `getnzb`, in XML and JSON.
- **No WebUI** — it is an API, not an application. Configure it with files.

## Architecture

```
  Sonarr / Radarr / any Newznab client
                 |
                 |  Newznab/Torznab HTTP  (port 5076,  /api?apikey=...)
                 v
        +-------------------+          +----------------------+
        |  stateless-hydra  | <------> |        Redis         |
        |  (stateless pods) |  cache   |  search cache and    |
        +-------------------+  limits  |  daily limit counters|
                 |                    +----------------------+
                 |  fan-out over HTTP (httpx)
                 v
   Usenet indexers  (Newznab / Torznab: NZBgeek, ...)
```

- Clients authenticate against **hydra API keys** (`hydraApiKeys`).
- stateless-hydra fans a search out to the **enabled indexers** that support
  the requested function, using each indexer's own API key (`apiKeys`).
- Results are merged, optionally de-duplicated and cached in Redis.
- `getnzb` downloads are routed back to the owning indexer via the composed
  guid (`<indexer>:<original-guid>`).

## Quickstart (Docker Compose)

The repo ships `docker-compose.yml` plus example config in `config/`.

1. Edit the example config files (all values are placeholders):
   ```sh
   $EDITOR config/indexers.yaml   # add your indexers
   $EDITOR config/api-keys.yaml   # add real indexer keys + a hydra API key
   ```
2. Start the stack:
   ```sh
   docker compose up --build
   ```
3. Verify:
   ```sh
   curl -s "http://localhost:5076/healthz"
   curl -s "http://localhost:5076/api?t=caps&apikey=YOUR_HYDRA_API_KEY"
   ```

The `app` service mounts `./config` read-only at `/config` and talks to the
`redis` service with no persistence (everything in Redis is cache or daily
counters). Point Sonarr/Radarr at `http://localhost:5076/api` with one of the
`hydraApiKeys` values as the API key.

## Kubernetes quickstart

Manifests live in `k8s/` and are wired together with Kustomize:

```sh
kubectl apply -k k8s/
```

Before doing so:

1. Replace the placeholder values in `k8s/secret-api-keys.yaml` — or, better,
   manage that Secret with External Secrets, SOPS/sealed-secrets, or a cloud
   secret manager. **Never commit real keys.**
2. Replace the example indexers in `k8s/configmap-indexers.yaml`.
3. Point Redis at your own endpoint by editing `redis_url` in
   `k8s/configmap-app.yaml` (the Service name in `k8s/redis.yaml` by default).
   For a managed/external Redis, drop `k8s/redis.yaml` and update that value.
   Do **not** set `SH_REDIS_URL` on the Deployment unless you intend it to
   override the ConfigMap.

The Deployment mounts the three config files as read-only sub-paths at
`/config/app.yaml`, `/config/indexers.yaml` and `/config/api-keys.yaml`,
runs as non-root with a read-only root filesystem (an `emptyDir` is mounted at
`/tmp`), and exposes `livenessProbe`/`startupProbe` on `/healthz` and
`readinessProbe` on `/readyz`.

### Troubleshooting: `/readyz` returns 503

If `/readyz` returns `503` and the logs say `Name or service not known`, the
app cannot resolve/reach Redis. Check that `redis_url` in the app-config
ConfigMap (`k8s/configmap-app.yaml`) matches your Redis Service DNS name.
Remember that `SH_*` environment variables override the config file, so do
**not** set `SH_REDIS_URL` on the Deployment unless you mean it to win; the
ConfigMap is the single source of truth in Kubernetes.

## Configuration reference

stateless-hydra reads three YAML files. Paths default to `/config/*.yaml` and
can be overridden with `SH_APP_CONFIG`, `SH_INDEXERS_FILE` and
`SH_API_KEYS_FILE`.

### `app.yaml` — application settings

All keys are optional; defaults are shown. Environment variables (`SH_*`)
override file values.

| Key | Default | Env var | Description |
| --- | --- | --- | --- |
| `log_level` | `INFO` | `SH_LOG_LEVEL` | Log verbosity (`DEBUG`…`CRITICAL`). |
| `user_agent` | `stateless-hydra/0.1.0` | `SH_USER_AGENT` | User-Agent sent to indexers. |
| `redis_url` | `redis://localhost:6379/0` | `SH_REDIS_URL` | Redis backing store URL. |
| `cache_ttl_seconds` | `900` | `SH_CACHE_TTL_SECONDS` | Default search cache TTL; `0` disables caching. |
| `dedupe_by_title` | `false` | `SH_DEDUPE_BY_TITLE` | Collapse identical titles across indexers. |
| `max_results_per_indexer` | `100` | `SH_MAX_RESULTS_PER_INDEXER` | Max results requested per indexer. |
| `global_proxy_url` | unset | `SH_GLOBAL_PROXY_URL` | Default proxy for indexer requests: `http://`, `https://`, `socks5://` (or `socks5h://`). |
| `host` | `0.0.0.0` | `SH_HOST` | Bind interface. |
| `port` | `5076` | `SH_PORT` | Bind port (nzbhydra2's default). |
| `app_config` | `/config/app.yaml` | `SH_APP_CONFIG` | Path to this file. |
| `indexers_file` | `/config/indexers.yaml` | `SH_INDEXERS_FILE` | Path to the indexers file. |
| `api_keys_file` | `/config/api-keys.yaml` | `SH_API_KEYS_FILE` | Path to the secrets file. |

Proxy URLs are passed straight to httpx, which supports `http://`, `https://`,
`socks5://` and `socks5h://`. SOCKS support is compiled into the image via the
`httpx[socks]` extra (socksio), so the same schemes work for both
`global_proxy_url` and per-indexer `proxyUrl`.

### `indexers.yaml` — indexer definitions

Top-level shape: `{ indexers: [ ... ] }`.

| Key | Default | Description |
| --- | --- | --- |
| `name` | *(required)* | Unique indexer id used in logs, metrics and guids. Must not contain `:`. |
| `enabled` | `true` | Whether this indexer participates in searches. |
| `host` | *(required)* | Scheme + host, no trailing slash, e.g. `https://api.nzbgeek.info`. |
| `apiPath` | `/api` | API path on the indexer. |
| `apiKeyRef` | *(required)* | Name looked up in `apiKeys` in `api-keys.yaml`. |
| `apiHitLimit` | `0` | Daily search-request budget; `0` = unlimited. |
| `nzbPullLimit` | `0` | Daily NZB-download budget; `0` = unlimited. |
| `resetTime` | `00:00` | Daily reset time, strict `HH:MM` 24-hour. |
| `resetTimezone` | `UTC` | IANA timezone for `resetTime` (validated). |
| `timeoutSeconds` | `30.0` | Upstream request timeout. |
| `cacheTtlSeconds` | `null` | Per-indexer cache TTL; `null` = global default, `<= 0` disables. |
| `searchTypes` | `["search"]` | Any of `search`, `tvsearch`, `movie`, `music`, `book`. |
| `categories` | `null` (all) | List of Newznab category ids to search. |
| `proxyUrl` | `null` | Per-indexer proxy; overrides `global_proxy_url` (same supported schemes). |
| `forceGetnzbRebuild` | `false` | Force the `t=getnzb&id=<guid>` rebuild path even when the guid is a URL. Set `true` when a URL-shaped guid points at a details page rather than the `.nzb` (see [NZB download resolution](#nzb-download-resolution)). |

**Reset semantics.** A fresh daily counter is used for each indexer. The
counter key includes the current date *in the indexer's `resetTimezone`*, so
the new day begins at `resetTime` in that timezone (default `00:00` UTC). A
limit of `0` means unlimited but usage is still counted for metrics.

### `api-keys.yaml` — secrets

> **This file contains secrets. In production it is a Kubernetes Secret
> mounted at `/config/api-keys.yaml`, and it must never be committed with real
> values.** The checked-in copy has `CHANGE_ME` placeholders only.

| Key | Shape | Description |
| --- | --- | --- |
| `apiKeys` | `{ref: "real-key"}` | Maps each indexer's `apiKeyRef` to its real API key. |
| `hydraApiKeys` | `["key", ...]` | API keys clients present to `/api`. Must contain at least one entry (the app refuses to start otherwise). |

## How limits work

Each enabled indexer has two independent, daily, Redis-backed budgets:

- **API hits** (`apiHitLimit`) — one unit per search request sent to the
  indexer.
- **NZB pulls** (`nzbPullLimit`) — one unit per NZB download served from the
  indexer.

When an indexer reaches a limit it is **skipped until its configured reset
time**, so a single exhausted indexer does not break searches as long as
others still have budget. When a client requests a download from an indexer
whose pull budget is exhausted, the API responds with Newznab error
**`930` (Download limit reached)**. When a search cannot be served because
every candidate indexer is out of API-hit budget, it responds with **`910`
(API hit limit reached)**. Because the counters live in Redis, all replicas
share one budget — adding pods does not multiply it.

## NZB download resolution

A search result carries a **composed guid** of the form `INDEXER:GUID`. When a
client requests `t=getnzb&id=INDEXER:GUID`, stateless-hydra resolves the
download from the owning indexer using this heuristic:

- **Non-URL guid** (for example `xyz` or a plain details id) — the download is
  rebuilt as `t=getnzb&id=<guid>` against the indexer's Newznab API. This is
  the normal case for Newznab/Torznab indexers.
- **URL-shaped guid** (`http://…` / `https://…`) — the URL is fetched
  **directly**, because some indexers (altHUB among others) advertise the
  `.nzb` download itself as the guid. Rebuilding that as `t=getnzb` would make
  the indexer answer "no such function".

The heuristic assumes a URL-shaped guid *is* the NZB. Some indexers instead put
a **details-page URL** in the guid; fetching it returns HTML, not an NZB. For
those, set `forceGetnzbRebuild: true` on the indexer. The flag forces the
`t=getnzb&id=<guid>` rebuild path even for URL-shaped guids, so the download is
requested from the indexer's API rather than fetched from the guid URL:

```yaml
indexers:
  - name: details_page_indexer
    host: "https://details.example.net"
    apiKeyRef: "details_page_indexer_key"
    forceGetnzbRebuild: true   # guid is a details URL, not the .nzb
```

When **not** to set it: if the guid is a working direct `.nzb` URL (altHUB-style),
leave the flag at its default `false` so the URL is fetched directly; forcing a
rebuild there would make the indexer answer "no such function" and break the
download.

The flag only affects `t=getnzb`. `t=search` and `t=details` are unchanged.
Upstream Newznab error documents are detected and translated on **both** paths,
so a misconfigured flag surfaces a clear Newznab error instead of streaming
HTML or an error body as a fake NZB.

## Caching

Search responses are cached in Redis keyed by the **normalized query** (the
request parameters with `apikey` removed, values trimmed and pairs sorted, so
parameter order does not matter). The TTL is the indexer's `cacheTtlSeconds`
when set, otherwise the global `cache_ttl_seconds`; a non-positive TTL disables
caching. The cache shortens indexer API usage, which is why it is a core part
of the limits story. Clearing Redis is always safe: it only costs a cold cache
and a reset of the current day's counters.

## Metrics

Prometheus metrics are exposed at `/metrics` under the `stateless_hydra_`
prefix.

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `stateless_hydra_search_requests_total` | counter | `function` | Search requests handled, by Newznab function. |
| `stateless_hydra_search_duration_seconds` | histogram | `function` | End-to-end search latency. |
| `stateless_hydra_indexer_api_hits_total` | counter | `indexer` | Search requests sent to each indexer. |
| `stateless_hydra_indexer_nzb_pulls_total` | counter | `indexer` | NZB downloads served from each indexer. |
| `stateless_hydra_indexer_errors_total` | counter | `indexer` | Upstream errors per indexer. |
| `stateless_hydra_indexer_limit_reached_total` | counter | `indexer`, `kind` | Times a budget was hit (`kind` = `api` or `nzb`). |
| `stateless_hydra_cache_hits_total` | counter | `indexer` | Cache hits per indexer. |
| `stateless_hydra_cache_misses_total` | counter | `indexer` | Cache misses per indexer. |
| `stateless_hydra_limit_remaining` | gauge | `indexer`, `kind` | Remaining budget for the current window. |

## API usage examples

Replace `YOUR_HYDRA_API_KEY` with a value from `hydraApiKeys`. All endpoints
are under `/api`; authentication is via the `apikey` query parameter.

```sh
# Capability document (also useful to confirm auth and configuration)
curl -s "http://localhost:5076/api?t=caps&apikey=YOUR_HYDRA_API_KEY"

# General search
curl -s "http://localhost:5076/api?t=search&q=ubuntu&apikey=YOUR_HYDRA_API_KEY"

# TV search with season/episode
curl -s "http://localhost:5076/api?t=tvsearch&q=show+name&season=2&ep=5&apikey=YOUR_HYDRA_API_KEY"

# Movie search by IMDb/TMDb id
curl -s "http://localhost:5076/api?t=movie&imdbid=0111161&apikey=YOUR_HYDRA_API_KEY"

# Music and book searches
curl -s "http://localhost:5076/api?t=music&artist=artist&album=album&apikey=YOUR_HYDRA_API_KEY"
curl -s "http://localhost:5076/api?t=book&title=title&author=author&apikey=YOUR_HYDRA_API_KEY"

# Item details, then download the NZB (id is the guid from a result)
curl -s "http://localhost:5076/api?t=details&id=INDEXER:GUID&apikey=YOUR_HYDRA_API_KEY"
curl -s "http://localhost:5076/api?t=getnzb&id=INDEXER:GUID&apikey=YOUR_HYDRA_API_KEY" -o item.nzb
```

### Sonarr / Radarr / etc.

Add stateless-hydra as a **Newznab** indexer:

- **URL:** `http://<host>:5076/api`
- **API key:** one of the values in `hydraApiKeys`
- **Categories:** select what your indexers provide (Newznab standard ids).

## Error codes

Errors are returned as Newznab XML `<error code="..." description="..."/>`
with **HTTP 200** (the Newznab convention clients expect):

| Code | Meaning |
| --- | --- |
| `100` | Incorrect user credentials — missing/unknown `hydraApiKeys` entry. |
| `200` | Missing parameter. |
| `201` | Incorrect parameter. |
| `202` | No such function. |
| `203` | Function not available. |
| `300` | No such item (unknown guid / details not found). |
| `900` | Unknown error. |
| `910` | API hit limit reached. |
| `930` | Download limit reached. |

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```sh
uv sync            # create .venv and install dependencies
uv run pytest      # run the test suite (fakeredis + respx, no live services)
```

Quality gates before every commit (see `AGENTS.md` rule 8):

```sh
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

Commits follow [Conventional Commits](https://www.conventionalcommits.org/)
(`type(scope): subject`, types `feat`, `fix`, `test`, `docs`, `chore`,
`refactor`, `build`, `ci`), one logical change per commit. Read `AGENTS.md`
for the full set of project rules.

## Release & CI

Versioning is driven by Conventional Commits through `python-semantic-release`
(configured in `pyproject.toml`): `feat` → minor, `fix` → patch, `BREAKING
CHANGE` → major, with tags of the form `v{version}`. Container images are
published to the GitHub Container Registry (ghcr.io) by the CI release
pipeline.
