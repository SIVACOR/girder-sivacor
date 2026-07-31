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
| `STATA_LIC` | The Stata license file, **base64-encoded**. Without it, 12 tests fail — see below. |
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
docker run -d --name test-mongo -p 27017:27017 mongo:4.4
docker run -d --name test-redis -p 6379:6379 redis:7-alpine

export DOCKER_HOST="unix:///var/run/docker.sock"
export GIRDER_NOTIFICATION_REDIS_URL="redis://localhost:6379/"
# Required, or 12 Stata-backed tests fail with a misleading `assert 4 == 3`
export STATA_LICENSE_HOSTPATH="/path/to/deploy-dev/volumes/licenses/stata.lic.19"

tox -e test

docker stop test-mongo test-redis && docker rm test-mongo test-redis
```

Coverage is written three ways: `term-missing` to the terminal, `coverage.xml`
(consumed by Codecov), and `htmlcov/` for local browsing.

## Troubleshooting

There are two distinct classes of failure that look like code regressions and are not.

### 1. Missing Stata license — 12 deterministic failures

**Always export `STATA_LICENSE_HOSTPATH` before running the suite.** This is the
single most common cause of a "broken" local test run. Exactly 12 tests submit
real jobs against `dataeditors/stata18_5-mp`, and without a license Stata exits
non-zero inside the container:

- all 5 of `test_stata.py`
- 5 of the 7 in `test_email_notifications.py` — all except
  `test_email_content_for_multistage_job` and
  `test_email_urls_follow_the_deployment_domain`
- `test_multistage.py::test_multistage_run`
- `test_ignore.py::test_ignore[test_stata.tar.gz]`

The failures do **not** mention licensing. `lib.py` only bind-mounts the license
when the variable is set, so the license error stays buried in the captured job
log. Nine of the twelve surface as `assert 4 == 3` (`JobStatus.ERROR` vs
`SUCCESS`); the other three (`test_stata.py::test_error_detection`,
`test_stata.py::test_secrets`,
`test_email_notifications.py::test_email_notification_error_handling`) fail on
unrelated-looking string assertions, which makes them even easier to misread as
real regressions.

```sh
export STATA_LICENSE_HOSTPATH=/path/to/deploy-dev/volumes/licenses/stata.lic.19
```

Verify with `tox -e test -- test_stata.py`: **5 passed** means it is wired up.
(Paths in posargs are relative to `tests/`, because `tox.ini` sets
`changedir = tests`.)

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
