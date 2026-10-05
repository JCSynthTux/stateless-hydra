# CHANGELOG


## v0.3.4 (2026-10-05)

### Bug Fixes

- **api**: Strip tt prefix from imdbid before forwarding to indexers
  ([`84e56db`](https://github.com/JCSynthTux/stateless-hydra/commit/84e56dbb9d7f957a5e7885daf595f29603d9b16d))

Clients (Radarr, Sonarr, AIOStreams) send the canonical tt-prefixed IMDb id, but Newznab's
  movie-search spec documents the numeric form and nZEDb-based indexers such as miatrix compare the
  value verbatim. A tt-prefixed imdbid matched nothing, so miatrix answered movie searches with an
  empty feed. Strip a leading case-insensitive tt (as nzbhydra2 does in
  Newznab.extendQueryUrlWithSearchIds) while preserving any leading zeros.

- **api**: Surface upstream error XML as indexer errors instead of silent zero results
  ([`847f20a`](https://github.com/JCSynthTux/stateless-hydra/commit/847f20aa4f3cada276b99c63ab893a33495fd9a2))

Indexers such as nZEDb-based miatrix answer a failed search with HTTP 200 and a Newznab <error
  code=... description=.../> body. That document has no channel, so parse_indexer_rss turned it into
  a valid-but-empty feed: the indexer looked like a successful search with no matches, no warning
  was logged and no error metric moved. Detect the upstream error document in the search parse path
  so the indexer is skipped, a WARNING with the code/description is logged, the error counter is
  incremented and the body is not cached. A genuinely empty feed (total="0") is still treated as a
  successful search.


## v0.3.3 (2026-10-05)

### Bug Fixes

- **api**: Advertise t=get download URLs so AIOStreams hashes items distinctly
  ([`3a1eb07`](https://github.com/JCSynthTux/stateless-hydra/commit/3a1eb07b8e37c1950e97f740821dbc03328e1684))

AIOStreams identifies an NZB by hashing its URL. Its hashNzbUrl knows the standard /api?t=get&id=...
  shape and keeps t and id, but does not know t=getnzb, so it fell back to stripping the whole
  query. Every item then hashed to the same /api value, collapsing all results into one release: the
  native usenet library conflated them and deduplication kept a single (wrong) file.

Advertise t=get (with the release identity in id) in the item link and enclosure, matching the
  newznab standard and nzbhydra2's own download link. t=getnzb remains accepted server-side for
  older clients, and get routes to the same handler.

- **newznab**: Render NZB attributes in the newznab namespace
  ([`75477f5`](https://github.com/JCSynthTux/stateless-hydra/commit/75477f5a347db3c7abc36407e1055a761732313c))

This feed serves Usenet (NZB) results, and nzbhydra2 renders NZB attributes as newznab:attr (torrent
  results get torznab:attr). Emitting torznab:attr made every attribute invisible to AIOStreams'
  nzbhydra/ newznab profile, which reads only newznab:attr -- including the language/subs attributes
  that drive language filters.

Emit newznab:attr for all result attributes (nzbhydra2 parity) and assert torznab:attr is no longer
  emitted.


## v0.3.2 (2026-10-05)

### Bug Fixes

- Derive package version from installed metadata so caps reports the real version
  ([`53af9a1`](https://github.com/JCSynthTux/stateless-hydra/commit/53af9a151d28e9e57a517f746f5c5a9aa60e0cfe))


## v0.3.1 (2026-10-05)

### Bug Fixes

- **newznab**: Carry indexer-reported size into items and nzb enclosures
  ([`def49de`](https://github.com/JCSynthTux/stateless-hydra/commit/def49de0d85b558f4bc014f3973992cfa46695fd))

Classic nZEDb/Newznab indexers such as drunkenSlug report the release size only as a torznab/newznab
  ``size`` attribute, never as a ``<size>`` element. The parser read only the element, so
  ``ResultItem.size`` stayed None, the rendered ``<enclosure>`` omitted ``length``, and newznab
  clients (AIOStreams' NEWZNAB profile keeps ``size``) saw size 0.

Read the size from the ``size`` attribute as a fallback and add an API regression test that runs a
  small port of AIOStreams' newznab drop rules (title required, enclosure with a type containing
  "nzb" required) over a search response, asserts every rendered item survives, and follows the
  advertised enclosure through the Redis token store.


## v0.3.0 (2026-10-05)

### Chores

- Sync uv.lock with pyproject version 0.2.0
  ([`aa59199`](https://github.com/JCSynthTux/stateless-hydra/commit/aa5919934404679500451d255b208846f476d071))

semantic-release bumps project.version in pyproject.toml but does not relock, so uv.lock recorded
  the editable stateless-hydra package at the old 0.1.0. That mismatch made `uv run` rewrite uv.lock
  on every local invocation (dirty tree) and made `uv lock --check` fail.

Regenerate the lock so it matches pyproject at commit time. The resolved dependency graph is
  unchanged; only the root package version moves.

Note: CI is not broken by this drift. Both lint and test jobs use `uv sync --frozen`, which uses the
  lock as written and tolerates a root-package version mismatch (verified locally on uv 0.12.23 and
  in the GitHub Actions log for 43bf939, where it installed stateless-hydra==0.1.3 from a lock
  recording 0.1.0). `--frozen` stays intentional; it only fails when the dependency set itself
  changes without a relock. Expect this one-line drift to reappear after each future release until
  relocking is automated in the release pipeline.

### Documentation

- Document token-based download resolution and indexer guidance
  ([`2571149`](https://github.com/JCSynthTux/stateless-hydra/commit/25711499b4b64275c3e91ba16ffe2ff0a6967513))

### Features

- **api**: Resolve NZB downloads via short-lived Redis tokens without leaking upstream URLs
  ([`f98ab30`](https://github.com/JCSynthTux/stateless-hydra/commit/f98ab303020e60762316a9f4f1a110116fedfe81))

- **config**: Add per-indexer downloadFunction option (get/getnzb) for NZB rebuilds
  ([`95fef13`](https://github.com/JCSynthTux/stateless-hydra/commit/95fef132f0013a4c3d3404c1947557729448062b))


## v0.2.0 (2026-10-05)

### Documentation

- Document NZB download resolution and forceGetnzbRebuild with examples
  ([`43bf939`](https://github.com/JCSynthTux/stateless-hydra/commit/43bf939a029fc23d3513d560715f9da5f657501f))

### Features

- **config**: Add per-indexer forceGetnzbRebuild flag for details-page URL guids
  ([`0a5243c`](https://github.com/JCSynthTux/stateless-hydra/commit/0a5243c73df6f72cf15c64869445197410694677))


## v0.1.3 (2026-10-05)

### Bug Fixes

- **api**: Fetch URL-shaped guids directly and detect upstream error XML in getnzb
  ([`fb9d4ba`](https://github.com/JCSynthTux/stateless-hydra/commit/fb9d4ba6d0f768e78f882a52d79d24d629d905c5))


## v0.1.2 (2026-10-04)

### Bug Fixes

- **newznab**: Render nzb enclosure so AIOStreams keeps search results
  ([`1f071ad`](https://github.com/JCSynthTux/stateless-hydra/commit/1f071ad5405a07ce7b5fb9c84959b8282552e436))

AIOStreams' newznab integration builds the NZB URL from the item's <enclosure> and silently drops
  every item that lacks one whose type contains "nzb"; its scanner also never drops items for any
  other reason. nzbhydra2 always emits an enclosure, so a real nzbhydra2 endpoint works while our
  feed parsed to zero streams despite a correct totalResults.

Add ResultItem.enclosure_url and render <enclosure url=... length=... type="application/x-nzb"/>
  when set. Search items now advertise our own /api?t=getnzb&id=<composed guid>&apikey=<caller key>
  as both <link> and the enclosure, matching nzbhydra2 and keeping downloads routed through the
  limit tracker instead of leaking the upstream indexer key via <link>.

The parser reads <enclosure url> back so render->parse round-trips hold.

### Documentation

- **deploy**: Correct ConfigMap header comment about env precedence
  ([`355ffa4`](https://github.com/JCSynthTux/stateless-hydra/commit/355ffa40fc61b70bad3e37234938155b4ebf61d7))

The header claimed SH_* env vars are how the Deployment points redis_url at the in-cluster Service.
  The Deployment deliberately sets no SH_REDIS_URL now, so the ConfigMap is authoritative; only note
  the standard SH_* override precedence and that redis_url should be edited here.


## v0.1.1 (2026-10-04)

### Bug Fixes

- **api**: Quiet readiness logging and add redis connection timeouts
  ([`2c30cec`](https://github.com/JCSynthTux/stateless-hydra/commit/2c30cecaad9e399f699bb021a2e0211ca365031c))

Log a single concise WARNING line per /readyz failure (with the exception message) instead of a full
  traceback on every probe cycle; the traceback is still available via logger.debug(...,
  exc_info=True).

Construct the app-owned redis.asyncio client with socket_connect_timeout=3.0 and socket_timeout=5.0
  so a misconfigured/unreachable Redis fails fast rather than hanging the readiness probe. Injected
  clients are passed through untouched, so tests keep their behavior.

- **deploy**: Stop overriding ConfigMap redis_url via image and deployment env
  ([`c40498c`](https://github.com/JCSynthTux/stateless-hydra/commit/c40498c96a00932fe9ba36503dd97e94af67ce9b))

The Docker image baked SH_REDIS_URL=redis://redis:6379/0 and the k8s Deployment set the same env
  var. Because SH_* env vars always override the config file, a user editing redis_url in the app
  ConfigMap still connected to redis:6379, failing readiness with 'Name or service not known'.

Remove the baked ENV default and the Deployment env block so the mounted app.yaml ConfigMap is the
  single source of truth for redis_url in k8s. docker-compose keeps its own SH_REDIS_URL
  (self-consistent with its redis service). Update the related comments in configmap-app.yaml,
  config/app.yaml and redis.yaml to point at the ConfigMap, noting SH_* env remains an override.

### Build System

- **deps**: Bump docker/login-action from 3 to 4
  ([`2ee62e9`](https://github.com/JCSynthTux/stateless-hydra/commit/2ee62e94605b0647463e1b240f4e7e7e7e57223a))

Bumps [docker/login-action](https://github.com/docker/login-action) from 3 to 4. - [Release
  notes](https://github.com/docker/login-action/releases) -
  [Commits](https://github.com/docker/login-action/compare/v3...v4)

--- updated-dependencies: - dependency-name: docker/login-action dependency-version: '4'

dependency-type: direct:production

update-type: version-update:semver-major ...

Signed-off-by: dependabot[bot] <support@github.com>

- **deps**: Bump docker/metadata-action from 5 to 6
  ([`f0aeb34`](https://github.com/JCSynthTux/stateless-hydra/commit/f0aeb34db57929e62e0caba4bc08b0ee598746a5))

Bumps [docker/metadata-action](https://github.com/docker/metadata-action) from 5 to 6. - [Release
  notes](https://github.com/docker/metadata-action/releases) -
  [Commits](https://github.com/docker/metadata-action/compare/v5...v6)

--- updated-dependencies: - dependency-name: docker/metadata-action dependency-version: '6'

dependency-type: direct:production

update-type: version-update:semver-major ...

Signed-off-by: dependabot[bot] <support@github.com>

### Chores

- Ignore GitHub token file
  ([`8f614e1`](https://github.com/JCSynthTux/stateless-hydra/commit/8f614e10a221fd2d836d93f78175b3a765a8b7fc))

### Documentation

- Add redis connectivity troubleshooting and prefer ConfigMap over env
  ([`6a83076`](https://github.com/JCSynthTux/stateless-hydra/commit/6a83076da1b38fcf7d26ec7f26c1f94dc9398c70))

Document that /readyz returning 503 with 'Name or service not known' means the app cannot reach
  Redis: check that redis_url in the app ConfigMap matches the Redis Service DNS name, and remember
  SH_* env vars override the config file. Point Kubernetes users at the ConfigMap redis_url instead
  of SH_REDIS_URL.


## v0.1.0 (2026-10-04)

### Bug Fixes

- **api**: Enforce enabled flag on details/getnzb and harden search edge cases
  ([`4247bb3`](https://github.com/JCSynthTux/stateless-hydra/commit/4247bb3345a254bcb85e7f5a6a0d9e0e321b307c))

- **build**: Support SOCKS proxies and honor SH_HOST/SH_PORT in the image
  ([`8792723`](https://github.com/JCSynthTux/stateless-hydra/commit/879272307874413878b23e1f12f9791ed6930e63))

- **ci**: Restructure release handoff so semantic-release tags publish the image
  ([`9bdf386`](https://github.com/JCSynthTux/stateless-hydra/commit/9bdf3865fcc4d8da770c2d842c5d90dde7af944e))

- **config**: Wrap all config failures in ConfigError and tighten validation
  ([`7e415b5`](https://github.com/JCSynthTux/stateless-hydra/commit/7e415b578fd1eb12cef1e95e118222fa9effa493))

- Wrap AppSettings() fallback construction in load_settings so invalid SH_* env values raise
  ConfigError instead of a raw pydantic ValidationError - Require an explicit 'indexers:' key in
  load_indexers; reject non-list values while still accepting an empty list - Enforce strict HH:MM
  reset_time via regex (rejects '1:30', ' 12:30 ', '24:00') - Replace mutable Field defaults with
  default_factory in IndexerConfig and ApiKeysFile - Remove dead 'Hello from stateless-hydra' main()
  stub from __init__.py and add module docstring/__version__ - Extend tests for invalid env,
  missing/non-list indexers key, empty list and strict reset_time formats

- **deploy**: Relax secret mount mode for fsGroup compatibility
  ([`563c3d3`](https://github.com/JCSynthTux/stateless-hydra/commit/563c3d32ef2a860a584a1121ad6780ff05cab83b))

- **limits**: Scope limit keys to reset window to prevent bypass at non-midnight resets
  ([`b579586`](https://github.com/JCSynthTux/stateless-hydra/commit/b579586f444d52c365128bcceca99893851a0bae))

- **newznab**: Align error handling, harden query normalization and pin namespace tolerance
  ([`9ceec42`](https://github.com/JCSynthTux/stateless-hydra/commit/9ceec4275f0aa39059c57a8fc0c0cd29b67dd42c))

### Build System

- Add Dockerfile, compose stack and container hygiene
  ([`97154a7`](https://github.com/JCSynthTux/stateless-hydra/commit/97154a710a310dad7eade63847846515355552d5))

### Continuous Integration

- Add dependabot config for GitHub Actions updates
  ([`b34aeb9`](https://github.com/JCSynthTux/stateless-hydra/commit/b34aeb962de9e8c977c32e2f43e37643294cc37b))

- Add lint, test and image-build workflow
  ([`7c54e39`](https://github.com/JCSynthTux/stateless-hydra/commit/7c54e39785b0d0994f04dcde59533fe1d92ef7b0))

- Add semantic-release and GHCR publish workflow
  ([`de9dd2b`](https://github.com/JCSynthTux/stateless-hydra/commit/de9dd2b8f9b9b4e20d2d30d8b3b27482564fe07b))

### Documentation

- Add configuration reference, quickstarts and architecture docs
  ([`77bf764`](https://github.com/JCSynthTux/stateless-hydra/commit/77bf764303ae518d7c5554feaa589f235470d667))

- Align README proxy and release wording with implementation
  ([`3e9cd41`](https://github.com/JCSynthTux/stateless-hydra/commit/3e9cd41ca3f9e102c3b4fb77e154db7f9105a036))

### Features

- Bootstrap stateless-hydra project skeleton and config layer
  ([`3d16bff`](https://github.com/JCSynthTux/stateless-hydra/commit/3d16bfff1cee8cc8e82186b2db04d5d0178991a9))

- Initialize uv-managed Python 3.12 package with hatchling backend - Add runtime deps (FastAPI,
  httpx, redis, pydantic-settings, prometheus-client, lxml, pyyaml) and dev deps (pytest,
  pytest-asyncio, respx, fakeredis, ruff) - Add ruff, pytest and semantic-release tooling config -
  Add AGENTS.md project rules - Add config layer: exception hierarchy, pydantic v2 models for
  indexers, api keys and app settings, YAML/env loaders, key resolution, logging setup - Add unit
  tests for configuration loading

- **api**: Add FastAPI app with Newznab endpoint, health and metrics routes
  ([`d98e6a0`](https://github.com/JCSynthTux/stateless-hydra/commit/d98e6a0d562989d17d6a8ddb9335adf8830e1758))

- **cache**: Add Redis-backed query cache
  ([`df35ee1`](https://github.com/JCSynthTux/stateless-hydra/commit/df35ee110394411fed007807bb8bfc951a635068))

- **deploy**: Add Kubernetes manifests for stateless-hydra and redis
  ([`8d14766`](https://github.com/JCSynthTux/stateless-hydra/commit/8d14766d15a8be785b163800526013ae2030e36d))

- **indexer**: Add per-indexer HTTP client with proxy and User-Agent support
  ([`dd6e397`](https://github.com/JCSynthTux/stateless-hydra/commit/dd6e3971c459b71b1cbe9ec14aacd6826e207ed0))

- **limits**: Add Redis-backed daily API/NZB limit tracking
  ([`b50daad`](https://github.com/JCSynthTux/stateless-hydra/commit/b50daadea73840c4978a9bd3c1ed4bca04678f09))

- **metrics**: Add Prometheus metrics registry
  ([`cfe00d3`](https://github.com/JCSynthTux/stateless-hydra/commit/cfe00d382f7e54c032a1d67e004f115da454e2cb))

- **newznab**: Add Newznab/Torznab XML rendering, parsing and query normalization
  ([`3442d12`](https://github.com/JCSynthTux/stateless-hydra/commit/3442d123bd1ee39765df36d64855e62329ba40c8))
