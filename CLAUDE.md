# girder-sivacor — working notes

Architecture, endpoints, and the cross-repo picture live in the workspace guide at
`../CLAUDE.md`. This file covers only what bites you when running the tests here.

## Before running the test suite: provide the Stata license

**Without a Stata license, 13 tests fail in a way that looks like a code
regression.** Counted by running the suite without one on 2026-08-01, not estimated.

```sh
export STATA_LICENSE_HOSTPATH=/path/to/deploy-dev/volumes/licenses/stata.lic.19
```

Exactly 13 tests submit real jobs against `dataeditors/stata18_5-mp`: all 5 of
`test_stata.py`, 5 of the 7 in `test_email_notifications.py`,
`test_multistage.py::test_multistage_run`,
`test_ignore.py::test_ignore[test_stata.tar.gz]`, and
`test_concurrent_submissions.py::test_submit_job_allowed_after_previous_finished`
— that last one is easy to miss, because nothing in its name suggests Stata; it
just happens to run a submission end to end.

**11 of the 13 surface as `assert 4 == 3`** (`JobStatus.ERROR` vs `SUCCESS`). The
other two fail on unrelated-looking string assertions:
`test_stata.py::test_secrets` (`assert 'SECRET_REDACTED' in ...`) and
`test_stata.py::test_error_detection` (`assert 'stdout_file_id' in ...`).

**The underlying error now names the problem, which it did not used to.** Since the
run-time license fetch landed, `lib.py::stata_license_mount_source` raises *before
the container is created*:

```
ValueError: A Stata image was requested but this deployment has no Stata license:
set the 'sivacor.stata_license' Girder setting, or STATA_LICENSE_HOSTPATH on the worker.
```

That text reaches the job log, so a failing test's captured output says what is
wrong. The top-level assertion is still the unhelpful `assert 4 == 3`, so the advice
below stands — but if you do look at the log, the answer is now in it.

**Two ways to satisfy it**, and the tests only use the first:

- `STATA_LICENSE_HOSTPATH` — a path on this host. `tox.ini` forwards it via
  `passenv` and nothing defaults it, so it must be exported.
- the `sivacor.stata_license` Girder setting — the license text, stored server-side
  and materialised into the job's `tmp_dir` at run time. This is what ephemeral
  workers use in production (they have no license on disk), but no test seeds it.

Do not start debugging Stata-backed test failures until one of those is in place.
Verify with `tox -e test -- test_stata.py` — **5 passed** means it is wired up.

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
