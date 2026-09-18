# CI/CD Setup

This repository runs its test suite in GitHub Actions with coverage reported to
**Codecov**. The workflow is `.github/workflows/test.yml`.

## What the workflow does

Triggers: pushes to `main`, and **all** pull requests.

Runner: `ubuntu-latest`, Python 3.12, Node 18. Steps, in order:

1. Installs `libacl1-dev` (needed to build Girder's dependencies).
2. Builds the web client (`girder_sivacor/web_client`: `npm ci && npm run build`).
3. Installs `tox`.
4. Decodes the Stata license from the `STATA_LIC` secret to `/tmp/licenses/stata.lic.19`
   and runs `sudo chmod 666 /var/run/docker.sock`.
5. Runs `tox -e test`.
6. Uploads coverage to Codecov (`codecov/codecov-action@v5`).

Service containers: `mongo:4.4` on 27017 and `redis:7-alpine` on 6379, both
health-checked before the steps run.

## Required repository secrets

| Secret | Purpose |
|---|---|
| `STATA_LIC` | The Stata license file, **base64-encoded**. Without it every Stata-backed test fails — see below. |
| `CODECOV_TOKEN` | Upload token for Codecov. |

## Environment variables

These are set by the "Run tests with coverage" step. `tox.ini` passes `GIRDER_*`,
`DOCKER_HOST`, `CI`, `GITHUB_*`, and `STATA_LICENSE_HOSTPATH` through via `passenv`
— they are *only* forwarded, never defaulted.

| Variable | CI value |
|---|---|
| `DOCKER_HOST` | `unix:///var/run/docker.sock` |
| `GIRDER_MONGO_URI` | `mongodb://localhost:27017/girder` |
| `GIRDER_HOST` | `0.0.0.0` |
| `GIRDER_WORKER_BROKER` | `redis://locahost/` |
| `GIRDER_WORKER_BACKEND` | `redis://locahost/` |
| `GIRDER_NOTIFICATION_REDIS_URL` | `redis://localhost:6379/` |
| `GIRDER_MAX_CURSOR_TIMEOUT_MS` | `60000` |
| `STATA_LICENSE_HOSTPATH` | `/tmp/licenses/stata.lic.19` |

> `GIRDER_WORKER_BROKER` / `GIRDER_WORKER_BACKEND` really do say `locahost`
> (missing `l`) in the workflow. It is inert today because the suite runs celery
> tasks eagerly and never reaches the broker, but it will bite the moment a test
> stops using `eagerWorkerTasks`.

That the broker is never reached is also a **coverage gap, not just a typo**, and
it has already cost a production bug. `eagerWorkerTasks` sets
`task_eager_propagates`, so a failing task re-raises the original exception
in-process; nothing is ever pickled. A `SubmissionError` that could not survive
that round-trip therefore passed every test — including one that drives a real
container to a real OOM kill — and only showed up on a production worker, as
`UnpickleableExceptionWrapper` with `code` and `detail` stripped off (2026-08-13).

Anything that only misbehaves when it crosses the task boundary — exception
shapes, task arguments, return values — needs a test that invokes the
serialization boundary directly. See `tests/test_error_serialization.py`.

Note that Mongo runs **without authentication** in CI, and `pytest-girder`
defaults to `mongodb://localhost:27017` anyway — so a plain local Mongo needs no
URI override at all.

## The suite runs in parallel

`tox -e test` invokes pytest with `-n 4` (`pytest-xdist`). This takes the suite
from roughly 7.5 minutes to 2.5 minutes; most of the serial wall clock was spent
waiting on Docker and Mongo rather than computing.

Parallelism is safe here because `pytest-girder` derives each test's database name
from a hash of its node id, so every test gets its own database regardless of
which worker runs it, and `boundServer` binds an ephemeral port rather than a
fixed one.

The count is pinned at 4 rather than `auto` because the gain flattens there
(measured: 115s at both `-n 4` and `-n 8`) while Docker contention keeps growing.

To debug a single test without parallelism (note the path is relative to `tests/`):

```sh
tox -e test -- -n 0 test_stata.py::test_dual_stdout
```

## Local testing

```sh
# Start services (no auth needed)
docker run -d --name test-mongo -p 27017:27017 --ulimit nofile=64000:64000 mongo:4.4
docker run -d --name test-redis -p 6379:6379 redis:7-alpine

**The `--ulimit` on mongo is load-bearing above `-n 8`, and its absence fails
confusingly.** `pytest-girder` gives every test its own database, so a wide run holds
hundreds of WiredTiger files open at once. Against a default-limit mongo (1024 fds),
`-n 16` makes mongod **abort mid-run** — `Too many open files` → `WT_PANIC` →
`fassert()` → exit 14 — and every remaining test then errors against a dead database,
which reads like a mass code regression rather than a resource limit. 64000 is
MongoDB's own documented recommendation. Measured 2026-08-21.

The `Too many index builds running simultaneously` line that shows up in the same log
is **INFO and benign**: mongo throttling itself back to 3 concurrent builds, which
resolves on its own. It is not the failure, and raising
`maxNumActiveUserIndexBuilds` does not help.

export DOCKER_HOST="unix:///var/run/docker.sock"
export GIRDER_NOTIFICATION_REDIS_URL="redis://localhost:6379/"
# Required, or every Stata-backed test fails with a misleading `assert 4 == 3`
export STATA_LICENSE_HOSTPATH="/path/to/deploy-dev/volumes/licenses/stata.lic.19"

tox -e test

docker stop test-mongo test-redis && docker rm test-mongo test-redis
```

Coverage is written three ways: `term-missing` to the terminal, `coverage.xml`
(consumed by Codecov), and `htmlcov/` for local browsing.

## Troubleshooting

There are two distinct classes of failure that look like code regressions and are not.

### 1. Missing Stata license

**This is the single most common cause of a "broken" local run, and it is not a code problem.**

The canonical setup block lives in `CLAUDE.md` in this repo ("Running the tests — do this exactly").
Use it; do not reconstruct the commands from this file.

The check that settles it:

```sh
tox -e test -- test_stata.py     # must print "5 passed"
```

If that does not print 5 passed, the environment is wrong and **the full suite's failure list
carries no information** — stop and fix the environment first.

Tests that submit real `dataeditors/stata18_5-mp` jobs fail without a license, most as
`assert 4 == 3` (`JobStatus.ERROR` vs `SUCCESS`) and a few on unrelated-looking string assertions.
None of them mention licensing at the assertion, which is what makes them easy to misread.

**There is deliberately no count here.** This file, `CLAUDE.md` and the git history each ended up
claiming a different one (12, 13, and the 14 actually observed on 2026-09-18) because every new
Stata-backed test invalidates them all silently. Use the check line, which cannot drift.

### 2. Timing-sensitive Docker tests — intermittent failures under load

Several tests assert on wall-clock behaviour of short-lived containers. Under CPU
contention a collector thread may not be scheduled before the container it is
watching exits, so the assertion fails with nothing actually broken.

`test_orphan_cleanup.py::test_short_containers_are_still_sampled[0.2]` is
**skipped** for exactly this reason — it passed serially but failed 3 of 4 runs at
`-n 4`. The `1` and `3` parameters still cover the defect the test guards (a 1s
container produced zero rows, 3s produced one).

If a *new* intermittent failure appears, check whether it is timing-dependent
before assuming a regression: re-run it alone with `-n 0`. If it passes in
isolation and fails under load, it belongs to this class. The likeliest
candidates are in `test_reaper.py` and `test_performance_data.py`.

### Docker socket permissions

```sh
sudo chmod 666 /var/run/docker.sock
```

### Service connectivity

```sh
mongosh mongodb://localhost:27017 --eval "db.runCommand({ping: 1})"
redis-cli -h localhost -p 6379 ping
```

## Badges

```markdown
[![Test and Coverage](https://github.com/SIVACOR/girder-sivacor/actions/workflows/test.yml/badge.svg)](https://github.com/SIVACOR/girder-sivacor/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/SIVACOR/girder-sivacor/branch/main/graph/badge.svg)](https://codecov.io/gh/SIVACOR/girder-sivacor)
```
