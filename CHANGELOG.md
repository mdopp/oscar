# Changelog

## [1.1.0](https://github.com/mdopp/oscar/compare/v1.0.0...v1.1.0) (2026-05-12)


### Features

* **connectors:** cloud-llm connector with Anthropic + Google backends ([8d35682](https://github.com/mdopp/oscar/commit/8d356820a0447c9ffc390ad4a0254f73c494a036))
* **debug:** runtime debug-mode toggle via Postgres + debug-set skill ([2f3192e](https://github.com/mdopp/oscar/commit/2f3192e9a5920a34e5f726d475968e200f10d8bb))
* **deployment:** add cpu-local and cloud deployment modes ([734b3da](https://github.com/mdopp/oscar/commit/734b3da229fb34dd61ede96d8bd0b6e1116afe02))
* **mcp:** .mcp.json so Claude Code can query OSCAR's live state ([cea6d33](https://github.com/mdopp/oscar/commit/cea6d336a0868988f607c76a0759c6c507d3988f))
* **skills:** audit.query — generic filter over OSCAR's audit tables ([d524aa0](https://github.com/mdopp/oscar/commit/d524aa065a02586f6b53da0f75a6c72ef066e973))
* **skills:** timer and alarm with shared oscar_time_jobs library ([a3c7c2d](https://github.com/mdopp/oscar/commit/a3c7c2d9c3ba4055898690750376178005a2783e))

## 1.0.0 (2026-05-12)


### Features

* **connectors:** oscar-connectors template + weather connector (Phase 1) ([c1d4a6e](https://github.com/mdopp/oscar/commit/c1d4a6ec520db76a051de231a8d4d46ea3cd01b0))
* **oscar-brain:** pod-yaml, init schema, weekly pg_dump (Phase 0) ([aa3471b](https://github.com/mdopp/oscar/commit/aa3471b4f46de68c2f667f1c78c501d1a202aff1))
* **oscar-brain:** signal-cli sidecar + identity.link skill (Phase 1) ([ab37c68](https://github.com/mdopp/oscar/commit/ab37c68fe875baf8d8a45a1c587c195421e44682))
* **oscar-voice:** wyoming services + gatekeeper orchestrator (Phase 0) ([62350a3](https://github.com/mdopp/oscar/commit/62350a3a1aa2f14474aec314b32099d3e0acb412))
* **skills:** light skill — HA-MCP light control for Phase 0 E2E ([11f5f36](https://github.com/mdopp/oscar/commit/11f5f36156800c2b86e6c44202bd64c73c4424dc))


### Bug Fixes

* **connector-weather:** use re.compile for httpx_mock url match ([dbeeafc](https://github.com/mdopp/oscar/commit/dbeeafcf8f3bc6815679997940d70e940e9a0e3b))


### Documentation

* add project README and MIT LICENSE ([93fb178](https://github.com/mdopp/oscar/commit/93fb17826bad6624bf3857c9ffedab02f77ff814))


### CI/CD

* build OSCAR images on GHCR via GitHub Actions ([37912b3](https://github.com/mdopp/oscar/commit/37912b3c9f6af0e8f48469feeb9f1abbabbcaabb))
* configure release-please for conventional-commits releases ([24a21f1](https://github.com/mdopp/oscar/commit/24a21f1f0ef189ac5fb866271a8a4dfa2a2d3d14))
* pre-commit hooks (whitespace, syntax checks, ruff) ([ea4aa2f](https://github.com/mdopp/oscar/commit/ea4aa2fab77179555b3b9c33e60d57a20a1e1db6))
* pytest workflow for OSCAR Python packages ([6783fe2](https://github.com/mdopp/oscar/commit/6783fe2c879dcb8f5c58b3b3317f69c669355b86))
