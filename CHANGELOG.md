# CHANGELOG


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
