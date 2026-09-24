---
type: operations
title: Testing, dependency policy, and release/packaging
description: The hermetic test wrapper, dependency-pinning policy, and how Hermes ships across PyPI/Docker/desktop installers/Homebrew.
tags: [operations, testing, ci, release, packaging]
timestamp: 2026-08-29
---

# Testing, dependency policy, and release/packaging

## Testing — always via `scripts/run_tests.sh`

Never call `pytest` directly. `scripts/run_tests.sh` enforces hermetic
environment parity with CI: unsets credential env vars, forces `TZ=UTC` and
`LANG=C.UTF-8`, runs `-n auto` xdist workers, and loads an in-tree
subprocess-isolation plugin (`tests/_isolate_plugin.py`).

```bash
scripts/run_tests.sh                                  # full suite, CI-parity
scripts/run_tests.sh tests/gateway/                    # one directory
scripts/run_tests.sh tests/agent/test_foo.py::test_x   # one test
scripts/run_tests.sh -v --tb=long                      # pass-through pytest flags
scripts/run_tests.sh --no-isolate tests/foo/           # disable subprocess isolation (faster, for debugging)
```

The wrapper probes `.venv` first, then `venv`, then
`$HOME/.hermes/hermes-agent/venv` (shared-venv worktrees).

### Why the wrapper exists

| | Without wrapper | With wrapper |
|---|---|---|
| Provider API keys | Whatever's in your env (auto-detects pool) | All `*_API_KEY`/`*_TOKEN`/etc. unset |
| `HOME`/`~/.hermes/` | Your real config + auth.json | Temp dir per test |
| Timezone | Local TZ (e.g. PDT) | UTC |
| Locale | Whatever is set | C.UTF-8 |
| xdist workers | `-n auto` = all cores | `-n auto` (safe — isolation prevents cross-worker flakes) |

`tests/conftest.py` also enforces points 1-4 as an autouse fixture so *any* pytest
invocation (including IDE runners) gets hermetic behavior — the wrapper is
belt-and-suspenders on top. The `_isolate_hermes_home` autouse fixture there
redirects `HERMES_HOME` to a temp dir; tests must never hardcode `~/.hermes/`.

### Subprocess-per-test isolation

Every test runs in a freshly spawned Python subprocess via
`tests/_isolate_plugin.py`, using `multiprocessing.get_context("spawn")` (works
identically on Linux/macOS/Windows — no reliance on POSIX `fork`). This means
module-level dicts/sets and `ContextVar`s from one test cannot leak into the
next — the historic `_reset_module_state` autouse fixture is gone. Per-test
overhead is ~0.5–1.0s; xdist amortizes it across cores. `isolate_timeout`
(`pyproject.toml`) caps each test at 30s, killing hangs and surfacing them as
failures. The plugin disables itself in spawned children via the
`HERMES_ISOLATE_CHILD=1` sentinel env var, so there's no fork-bomb risk.

### Don't write change-detector tests

A test that fails whenever data *expected to change* gets updated — model
catalogs, config version numbers, enumeration counts, hardcoded provider-model
lists — adds no behavioral coverage and just breaks CI on routine updates.
Reviewers reject these; the fix is to assert the *relationship* (e.g. "every
catalog entry has a context-length entry") instead of the specific values.

## Dependency pinning policy

Established after the litellm supply-chain compromise (PR #2796, #2810) and
reinforced after the Mini Shai-Hulud worm campaign (May 2026):

| Source type | Treatment | Example |
|---|---|---|
| PyPI package | `>=floor,<next_major` | `"httpx>=0.28.1,<1"` |
| Git URL | Commit SHA | `git+https://...@<40-char-sha>` |
| GitHub Actions | Commit SHA + comment | `uses: actions/checkout@<sha>  # v4` |
| CI-only pip | `==exact` | `pyyaml==6.0.2` |

A bare `>=X.Y.Z` with no ceiling is rejected by CI and reviewers. `pyproject.toml`
itself goes further for direct deps — exact `==X.Y.Z` pins everywhere, so a new
transitive version only reaches a user via a reviewed pin bump + `uv.lock`
regeneration, not silently via PyPI. `requires-python = ">=3.11,<3.14"` is
similarly load-bearing, not cosmetic: 3.14 has no `cp314` wheel yet for some
Rust-backed transitives (e.g. `pydantic-core`), so the ceiling makes `uv` refuse
3.14 outright rather than falling back to a failing source build.

## Release (`scripts/release.py`)

Generates changelogs and creates GitHub releases with **CalVer** tags:

```bash
python scripts/release.py                                        # preview changelog (dry run)
python scripts/release.py --bump minor                           # preview with a semver-style bump
python scripts/release.py --bump minor --publish                 # create the release
python scripts/release.py --bump minor --publish --first-release # first release, no previous tag
python scripts/release.py --bump minor --publish --date 2026.3.15  # override CalVer date (belated release)
```

## Packaging surfaces

Hermes ships through several independent packaging paths, each with its own
build tooling:

- **PyPI / pip install** — `setup.py` (top-level) bundles `skills/` and
  `optional-skills/` as `data_files` via `_data_file_tree()`, alongside the
  `[build-system]`/`[project]` metadata in `pyproject.toml`.
- **Docker** — `docker-compose.yml` / `docker-compose.windows.yml` run the
  `nousresearch/hermes-agent:latest` image as the `gateway` service, mounting
  `~/.hermes` (or `${USERPROFILE}/.hermes` on Windows) into the container. The
  Windows compose file drops `network_mode: host` (unsupported on Docker Desktop
  for Windows) in favor of explicit port mappings. Container internals
  (`docker/entrypoint.sh`, `docker/cont-init.d`, `docker/s6-rc.d`) use an s6
  overlay init system.
- **Electron desktop app** (`apps/desktop/`) — Vite + `tsc -b` build, packaged
  with `electron-builder` (`npm run dist`, `dist:mac`, `dist:mac:dmg`, `dist:win`,
  `dist:win:msi`, `dist:win:nsis`, ...). `scripts/assert-root-install.cjs` and
  `assert-dist-built.cjs` guard the build against a stale/partial install.
- **Bootstrap installer** (`apps/bootstrap-installer/`) — a signed installer built
  with **Tauri** (Rust shell in `src-tauri/`, React/Vite UI) that drives
  `scripts/install.ps1` with a native UI on Windows; `src-tauri/src/{bootstrap,powershell,install_script,update}.rs`
  do the actual orchestration.
- **Homebrew** — formula under `packaging/homebrew/`.
- **Raw install scripts** — `scripts/install.sh` (POSIX), `scripts/install.ps1`
  (Windows PowerShell, also what the Tauri installer wraps), `scripts/install.cmd`.

## Related

- [data-models/config-state-profiles.md](../data-models/config-state-profiles.md) —
  the `HERMES_HOME` isolation these test fixtures redirect around.
- [architecture/cli-tui-gateway.md](../architecture/cli-tui-gateway.md) — what the
  desktop/bootstrap-installer packages actually ship.
