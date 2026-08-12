# Deploying the SIVACOR worker

## Two kinds of worker

Submission tasks used to run on the same worker as Girder's own async tasks,
sharing its MongoDB and assetstore. They no longer do, and the two roles now
have to be deployed separately:

**The Girder core worker** consumes `local`. Girder hardcodes that queue name
(`@app.task(queue='local')` in `girder/tasks.py`) for `deleteFolderTask`,
`copyFolderTask`, `importDataTask` and friends, and those genuinely do reach
into the database and the assetstore. It keeps `GIRDER_MONGO_URI` and the
assetstore mount, and it must stay colocated with them. Nothing here changes it.

**The SIVACOR submission worker** consumes `sivacor` plus a private queue. It
reaches Girder only over the REST API, so it needs neither Mongo nor the
assetstore — just HTTP to Girder, the broker, Redis for log streaming, a Docker
socket, and the GPG keyring (the TRO is still signed on the worker).

One image can serve both roles; only `-Q` and the mounts differ.

> `local` belonging to Girder core is why the SIVACOR dispatch queue is named
> `sivacor` rather than reusing `local`.

## Queues

A submission downloads its replication package into a local directory and every
later step assumes that directory is still there, so all of one submission's
tasks have to run on one machine. A submission worker therefore consumes two
queues:

* a **shared dispatch queue**, `sivacor` by default (`SIVACOR_DISPATCH_QUEUE`),
  which new submissions are published to and which celery load-balances; and
* a **private queue** named `sivacor.<node name>`, which the first task of a
  submission redirects the rest of the chain to.

Start the worker with both. The private queue name is derived from the celery
node name, so the default `celery@$(hostname)` lines up with:

```
celery -A girder_worker.app worker -Q sivacor,sivacor.$(hostname) --concurrency=2
```

Set `SIVACOR_WORKER_QUEUE` if you name the private queue something else.

> If the worker's `-Q` and the plugin's `SIVACOR_DISPATCH_QUEUE` disagree,
> submissions queue up in Redis and never run, with nothing logged anywhere.
>
> In a compose/stack file, pass the flag and its value as **separate** list
> entries. A single `"-Q sivacor,sivacor-worker-01"` token is parsed by click as
> `-Q` with the value `" sivacor,…"` — celery splits on commas without
> stripping, so the worker silently binds a queue named `" sivacor"`. Check the
> startup banner: the queue names must have no leading space. The same trap
> applies to the core worker's `-Q local`, where it also makes Girder's
> `is_local_worker_available()` return False and folder deletes fail with 503.

## Authentication

`POST /sivacor/submit_job` mints an admin-scoped Girder token (7 days, see
`WORKER_TOKEN_DAYS` in `rest.py`) and attaches it to the celery message as the
`girder_client_token` header, alongside `girder_api_url`. `girder_worker` copies
both onto each task, so nothing has to be configured on the worker for the
submission pipeline itself.

Set `worker.api_url` in Girder if the URL the worker should call back on differs
from the server's own root — otherwise `getApiUrl()` is used.

## Retention sweep

The 12-hourly `cleanup_submissions` task removes jobs by Mongo query, which the
REST API cannot express, so the sweep itself now runs on the server behind
`POST /sivacor/cleanup` (admin only). The periodic task is only the timer, and a
timer has no submission to inherit a token from — so give **one** submission
worker a standing credential:

```
GIRDER_API_URL=https://girder.sivacor.org/api/v1
GIRDER_API_KEY=<api key for an admin account>
```

Without them the task logs that it is skipping the sweep and does nothing.

`add_periodic_task` only fires under a `celery beat` scheduler. If the
deployment runs no beat — `deploy-dev` currently does not — nothing triggers the
sweep at all, and `POST /sivacor/cleanup` has to be driven from outside (cron,
or by hand).
