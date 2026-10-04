# stateless-hydra — Project Rules

stateless-hydra emulates the nzbhydra2 external API (Newznab/Torznab compatible)
as a stateless, Kubernetes-native proxy/aggregator for Usenet indexers.

## Stack
- Python 3.12+ (pinned via .python-version; uv-managed)
- FastAPI + uvicorn (async), httpx, redis-py (asyncio), pydantic v2 + pydantic-settings,
  prometheus-client, lxml
- Package manager: uv. Dev deps: pytest, pytest-asyncio, respx, fakeredis, ruff.

## Rules
1. Stateless: NO local persistence (no sqlite/files/dbs). All runtime state lives in Redis.
2. Secrets: Usenet indexer API keys live ONLY in the api-keys secret file; code may only
   reference keys by name (apiKeyRef). Never commit real secrets.
3. Async everywhere: async/await only in request-handling paths; no blocking calls.
4. Config: files + env vars with prefix SH_; fail fast with clear errors on invalid config.
5. Errors: Newznab-style `<error code="..." description="..."/>` XML with HTTP 200
   (codes: 100 auth, 200 missing param, 201 bad param, 202 unknown function, 203 function
   unavailable, 300 no such item, 900 unknown, 910 API hit limit reached, 930 NZB download
   limit reached).
6. Limits: daily indexer API-hit and NZB-pull limits tracked in Redis; when hit, skip the
   indexer until its configured reset time (default 00:00 UTC per indexer).
7. Testing: unit tests for every module; never require live Redis or real indexers
   (fakeredis + respx/httpx MockTransport only).
8. Quality gates before every commit:
   `uv run ruff check . && uv run ruff format --check . && uv run pytest`
9. Commits: Conventional Commits — type(scope): subject. Types: feat, fix, test, docs,
   chore, refactor, build, ci. One logical change per commit.
10. Layout: source in src/stateless_hydra/, tests in tests/, config examples in config/
    (later task), k8s manifests in k8s/ (later task), CI in .github/workflows/ (later task).
