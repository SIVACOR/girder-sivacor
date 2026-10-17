# girder-sivacor — working notes

Architecture, endpoints, and the cross-repo picture live in the workspace guide at
`../CLAUDE.md`. This file covers only what bites you when running the tests here.

## Running the tests — do this exactly, before anything else

**Copy this block verbatim.** There is no shorter correct version. A bare `tox -e test`, or any
direct `pytest`, produces a dozen failures that look precisely like a code regression and are not.

```sh
docker rm -f test-mongo test-redis 2>/dev/null            # stale containers hold the names
docker run -d --name test-mongo -p 27017:27017 --ulimit nofile=64000:64000 mongo:4.4
docker run -d --name test-redis -p 6379:6379 redis:7-alpine

export DOCKER_HOST="unix:///var/run/docker.sock"
export GIRDER_NOTIFICATION_REDIS_URL="redis://localhost:6379/"
export STATA_LICENSE_HOSTPATH="/home/xarth/codes/sivacor/deploy-dev/volumes/licenses/stata.lic.19"

tox -e test -- test_stata.py    # CHECK FIRST: must print "5 passed"
tox -e test                     # only meaningful once the line above passes
```

### The check line is the whole point

`tox -e test -- test_stata.py` printing **5 passed** is the one signal that the environment is
wired up. Run it *before* the full suite, every time.

**If it does not print 5 passed, stop. Fix the environment. Do not read the full suite's output,
and do not begin diagnosing a regression** — a broken environment and a broken commit are not
distinguishable from the failure list, and the failure list is where the time goes.

This file used to try to make that distinction *for* you by naming how many tests fail without a
license. Three documents ended up carrying three different numbers (12, 13, and the 14 actually
observed on 2026-09-18) because each new Stata-backed test silently invalidated all of them —
`test_memory_limit.py::test_an_oom_killed_analysis_is_reported_as_one[stata]` was the one that
broke it last. **A count is a symptom-matcher and it rots.** The check line cannot rot, so it
replaced the count deliberately; please do not reintroduce one.

### Why each line is there

- **`STATA_LICENSE_HOSTPATH`** — the path above exists on this machine and is gitignored, so it
  never arrives with a fresh clone. Without it, every test that submits a real
  `dataeditors/stata18_5-mp` job fails as `assert 4 == 3` (`JobStatus.ERROR` vs `SUCCESS`), and a
  few on unrelated-looking string assertions. None of them mention licensing at the assertion.
  Since the run-time license fetch landed, `lib.py::stata_license_mount_source` does raise a clear
  `ValueError` naming the missing setting, and it reaches the job log — so if you *are* already
  staring at a failure, read the captured log rather than the assertion.
- **`tox`, not `pytest`** — only `tox.ini`'s `passenv` forwards `STATA_LICENSE_HOSTPATH`, and it
  sets `changedir = tests`, so posargs are relative to `tests/`. Passing `tests/test_foo.py`
  silently collects nothing and exits 4.
- **`--ulimit nofile=64000:64000`** — load-bearing above `-n 8`. Every test gets its own database,
  so a wide run holds hundreds of WiredTiger files open; a default-limit mongod aborts
  (`Too many open files` → `WT_PANIC` → exit 14) and every remaining test then errors against a
  dead database, reading like a mass regression. 64000 is MongoDB's own recommendation.
- **`docker rm -f` first** — a stale `test-mongo` from a previous session keeps the name, the new
  `docker run` fails, and the suite runs against nothing.

There is a second way to satisfy the license — the `sivacor.stata_license` Girder setting, holding
the license text server-side and materialised into the job's `tmp_dir` at run time. That is what
ephemeral production workers use, since they have no license on disk. **No test seeds it**, so it
is not an option for the suite.

### Expected healthy result

Everything passes, with no `xfailed` and no `xpassed`. (Until 2026-10-15 the healthy
state was **2 `xfailed`** in `test_girder_upload_race.py`; upstream fixed the bug and
those are plain tests now — see the section on them below.)

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

- **The suite runs in parallel** (`-n 4` by default). This is safe because
  `pytest-girder` names each test's database from a hash of its node id, so isolation
  is per-test, not per-process.
- **The default 4 is sized for CI, not for a workstation.** CI runs on a 4-vCPU
  `ubuntu-latest`. On a big local box, `export PYTEST_XDIST_N=16` (or `-- -n 16`) is
  **~2.5x faster** — measured 2026-08-21 on 32 threads: 122-132s at `-n 4` against
  49-53s at `-n 16`, flattening from 16 to 32. tox.ini's `commands` line carries the
  full table and the reasoning.
- **Above `-n 8`, mongo needs `--ulimit nofile=64000:64000` or it aborts.** Every
  test gets its own database, so a wide run opens hundreds of WiredTiger files;
  a default-limit mongo hits 1024 fds and dies with `WT_PANIC`/exit 14, after which
  every remaining test errors against a dead database and it looks like a mass code
  regression. `CI_SETUP.md` has the corrected `docker run`. The
  `Too many index builds` line alongside it is INFO and is not the cause.
- **Timing-sensitive Docker tests can fail under load** without anything being
  broken. `test_orphan_cleanup.py::test_short_containers_are_still_sampled[0.2]`
  is skipped for this reason. Before treating a new intermittent failure as a
  regression, re-run it with `-n 0`; if it passes alone and fails under load, it
  is contention, not code. `test_reaper.py` and `test_performance_data.py` are the
  likeliest to join this category.
- CI reports coverage to **Codecov**, not Coveralls, despite some stale references
  elsewhere in the repo.

Fuller detail, including the CI workflow and required secrets, is in `CI_SETUP.md`.

## Execution records — the one store that outlives a submission

`models/execution_record.py` holds anonymous per-run telemetry that survives
both user deletion and the retention sweep. It is kept indefinitely, and that is
only defensible because it is not personal data.

`telemetry.py::sanitize_record` is the boundary that guarantees it: an
allow-list that rebuilds the document field by field on the server, with the
failure `detail` validated **per error code** rather than by charset. A charset
loose enough for `rocker/r-ver:4.6.1` is also loose enough for
`/home/jane/thesis.dta`, which is why that distinction exists. Treat
`tests/test_execution_records.py` as boundary tests: they assert what must *not*
survive.

`errors.py` is the other half. `SubmissionError` carries a researcher-facing
message (free to quote their filenames and Stata log lines — it goes to the job
log, which is deleted with the submission) *and* a `code`/`detail` pair that is
safe to keep forever. Unclassified exceptions keep only their class name.

Do not add an identifier "just for debugging". There is deliberately no key
joining a record to a user, job or folder, and that absence is what keeps these
out of scope for erasure requests. While a submission exists its job log has the
full story.

The rationale is written up for review in
`../development_notes/compliance/LEGITIMATE_INTERESTS_ASSESSMENT.md`.

## Deleting a submission — what goes, and what is kept redacted

`chain.py` is the only definition of "this submission's chain", and it is a
`$or`: the head step (`prepare_submission`) is published positionally and
carries the submission's job id at `args[3]`, every step after it gets the
previous step's return value and carries it at `args[0]["job_id"]`. Two
spellings of that existed until girder-sivacor#129 and the narrower one was the
one doing the deleting, so the retention sweep had been leaving the head of
every expired chain behind since the pipeline was written.

The two paths differ on purpose:

* **The retention sweep deletes the chain outright**, with everything else.
* **`DELETE /sivacor/submission/:id` redacts it and keeps the skeleton.** The
  log, the `jobInfoSpec` token, `args` (workspace paths, the main file's name,
  the secrets envelope) and the creator's id all go; `title`, `status`,
  `timestamps`, the celery parent link, the image each stage ran, the
  traceback and a scrubbed copy of the submission job's narration stay. That
  set is not a guess — it is every column the appendix of
  `../development_notes/incidents/2026-10-12-dns-pull-kills-run.md` found a
  past write-up had actually depended on.

Three things to know before changing any of it:

* **`redact_job` is an allow-list applied as an unset of its complement.** A
  field a future girder_worker invents is dropped with no one remembering to
  drop it. Same rule as `telemetry.sanitize_record`, same reason.
* **A traceback has its own field now** (`meta.sivacorFailure`, written by
  `PUT /sivacor/failure/:id`). girder_worker's `gw_task_failure` is *replaced*,
  not merely guarded, in `worker_plugin/job_status.py`: upstream appends the
  traceback to the job log, which is where the researcher's container stdout
  also goes, and a mixed stream can be neither kept nor dropped. The message
  survives only for the exception types `errors.PLATFORM_EXCEPTIONS` names —
  a `SubmissionError`'s text is the researcher's by contract.
* **`narration.py` will rot, and is built to rot loudly.** It is a template
  per log line the platform writes, and a line nobody listed is dropped and
  counted.
  `test_chain_redaction.py::test_the_platforms_narration_survives_the_submission`
  runs a real submission and fails if that count is non-zero, naming the line.
  **If you add a line to a submission job's log, add its template**; that test
  is the only thing standing between a new line and its silent disappearance
  from every future post-mortem.

The backfill is `POST /sivacor/redact_orphans` (admin). Bounded, idempotent,
and paged by job id rather than by offset so a page that redacts nothing still
advances — pass the returned `next` back as `after` until it is null. It never
touches a chain whose submission job still exists.

**Measured against the 2026-10-16 production dump restored into `deploy-dev`**,
which is the only way any of this was checked and is worth repeating before
running it anywhere real (`deploy-dev/restore_dump.sh`):

| | issue #129 (2026-10-14) | restored dump |
|---|---|---|
| celery chain jobs | 1,636 | **5,175** |
| oldest | 2026-08-05 | **2025-11-14** |
| orphaned | 1,450 (89 %) | **3,191 (62 %)** |
| belonging to a live submission | 186 | 1,373 |
| `userId` on the document | *"is `None`"* | **set on all 5,175** |

The issue's counts are all low and its `userId` claim is wrong, so do not size
anything from them. Two consequences in the code: the pseudonym is taken from
`userId` first and only falls back to scraping `docker:logs:<id>` out of the
log, and 25 chain jobs carry an `args[3]` that is not a valid ObjectId — a
malformed id cannot name a live submission, so they are redacted rather than
skipped.

**611 `type: "celery"` jobs are left untouched and still hold a Girder-Token**:
441 are Girder's own `deleteFolderTask` (titled `<unnamed job>`, args
`[folderId, progress, userId]`) and 170 are the retired `Cleanup submission
folder`, 169 of which still have a log. Neither is "this submission's chain" by
`chain_query`'s definition, so the backfill counts them as `unknown` and does
not touch them. That is the fail-closed answer and it is also an unclosed gap —
it is not in #129's scope, and it is a real one.

`sivacor.pseudonym_salt` is generated on first use and **deleting it is the
kill switch**: every pseudonym ever written becomes unlinkable to every other,
which is the property that makes keeping them defensible at all.

## `test_girder_upload_race.py` — upstream's bug, now pinned as fixed

That file tests *Girder's* upload model, not ours: a failed chunk's rollback
truncated other chunks' bytes and the upload still finalised, leaving a file
document larger than its blob. **Fixed upstream in girder/girder#3934, first
released in girder 5.0.19**, which `setup.py` now sets as the floor — that floor
is what these tests protect, so a failure here means the floor slipped or
upstream regressed, not that SIVACOR broke.

Both tests were `xfail(strict=True)` until 2026-10-15; they are plain assertions
now, so the suite reports **no xfailed and no xpassed**. The SIVACOR-side
mitigation in `rest.py` (`verify_upload_complete`, and
`GET /sivacor/upload_integrity`) is **kept**: it covers deployments still running
below the floor and is the only thing that detects a short blob already on disk.

**Upstream fixed only the half these tests reproduce.** A rejected chunk that
interleaves with a legitimate one still finalises a file at exactly its
document's length with another chunk's bytes inside it — measured against
released 5.0.19, invisible to any length check and catchable only by a full
re-hash (`tools/assetstore_integrity.py --verify-hash`). Do not read these two
passing tests as "that bug is gone". Two artifacts live in the girder checkout:
`test/test_upload_chunk_interleave.py` shows it and
`test/test_upload_chunk_invariant.py` is what a fix has to pass. Background and
the open decision: `../development_notes/08_girder_upload_race_plan.md`.

## `tools/` — operator scripts, and not scratch

`tools/assetstore_integrity.py` compares every Girder file document's `size`
against the blob it points at, and separately looks for uploads whose
`received` counter has outrun their temp file. Read-only. It exists because
Girder can finalise an upload whose stored copy is *shorter* than its
document, and nothing else in the stack notices until something reads the whole
file — see `../development_notes/08_girder_upload_race_plan.md`.

Run it inside the girder container, which already has pymongo and the
assetstore mounted:

```sh
docker exec -i $(docker ps -qf name=wt_girder) \
    python3 - < tools/assetstore_integrity.py --verify-hash
```

Two results worth knowing before you read its output: a missing temp file is
ordinary debris (an abandoned upload outlives its temp file), which is why
those are counted rather than listed; and its first production run reported 0
short blobs among 177 documents, because the two known-corrupt files had
already gone away with their deleted submissions.

Unlike the files below, this directory *is* part of the repo.

## Scratch files — not part of the plugin

Root `aaa.py` / `ddd.py` / `debug.py` / `ala.py` / `foobar/`, `auth/orcid.bak`,
and non-`.mako` files in `mail_templates/` are all scratch.
(`models/volume.py`, previously listed here as dead code, was deleted on
2026-08-06. `tro_utils.patch`, which made the installed `tro-utils` tolerate
symlinks and missing files in `sha256_for_file`, is gone too -- tro-utils
handles both itself, and the plugin now requires >=0.5.0.)
