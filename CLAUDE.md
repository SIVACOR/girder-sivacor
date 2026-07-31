# girder-sivacor — working notes

Architecture, endpoints, and the cross-repo picture live in the workspace guide at
`../CLAUDE.md`. This file covers only what bites you when running the tests here.

## Before running the test suite: export the Stata license

**`STATA_LICENSE_HOSTPATH` must be exported, or 12 tests fail in a way that looks
like a code regression.**

```sh
export STATA_LICENSE_HOSTPATH=/path/to/deploy-dev/volumes/licenses/stata.lic.19
```

Exactly 12 tests submit real jobs against `dataeditors/stata18_5-mp`: all 5 of
`test_stata.py`, 5 of the 7 in `test_email_notifications.py`,
`test_multistage.py::test_multistage_run`, and
`test_ignore.py::test_ignore[test_stata.tar.gz]`.

Without a license Stata exits non-zero *inside the container*, so the real error
stays in the captured job log and never mentions licensing. Most of the 12 surface
as `assert 4 == 3` (`JobStatus.ERROR` vs `SUCCESS`); three fail on
unrelated-looking string assertions instead. `lib.py` only bind-mounts the license
when the variable is set, and `tox.ini` only forwards it via `passenv` — nothing
defaults it.

Do not start debugging Stata-backed test failures until this is exported. Verify
with `tox -e test -- test_stata.py` — **5 passed** means it is wired up.

## Commands

```sh
tox -e lint                         # ruff check
tox -e test                         # pytest, runs from tests/, -n 4 (xdist)
tox -e test -- -n 0 test_foo.py     # serial, for debugging one test
```

`tox.ini` sets `changedir = tests`, so **posargs paths are relative to `tests/`** —
passing `tests/test_foo.py` silently collects nothing and exits 4.

Needs MongoDB (27017, no auth), Redis (6379), and a usable Docker socket. Many
tests really do build and run containers; the suite is Docker-bound, not CPU-bound.

## Test suite gotchas

- **The suite runs in parallel** (`-n 4`). This is safe because `pytest-girder`
  names each test's database from a hash of its node id, so isolation is per-test,
  not per-process.
- **Timing-sensitive Docker tests can fail under load** without anything being
  broken. `test_orphan_cleanup.py::test_short_containers_are_still_sampled[0.2]`
  is skipped for this reason. Before treating a new intermittent failure as a
  regression, re-run it with `-n 0`; if it passes alone and fails under load, it
  is contention, not code. `test_reaper.py` and `test_performance_data.py` are the
  likeliest to join this category.
- CI reports coverage to **Codecov**, not Coveralls, despite some stale references
  elsewhere in the repo.

Fuller detail, including the CI workflow and required secrets, is in `CI_SETUP.md`.

## Scratch files — not part of the plugin

`models/volume.py` is dead code (imports nonexistent modules, 0% coverage). Root
`aaa.py` / `ddd.py` / `debug.py` / `ala.py` / `foobar/`, the `*.patch` files,
`auth/orcid.bak`, and non-`.mako` files in `mail_templates/` are all scratch.

`tro_utils.patch` patches the *installed* `tro-utils` so `sha256_for_file`
tolerates symlinks and missing files.
