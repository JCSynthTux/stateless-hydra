# CHANGELOG


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
