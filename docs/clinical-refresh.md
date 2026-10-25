# ClinicalTrials.gov manual refresh

The clinical snapshot is refreshed manually and is published only when the complete recruiting-Massachusetts index cohort has a validated individual study record. A failed or interrupted run leaves the previously published snapshot unchanged and retains validated detail caches plus an atomic cohort checkpoint for the next run.

Clinical commands load the repository-root `.env` without overriding variables already exported by the shell or process manager. By default they select SQLite or PostgreSQL through `TRIALMATCH_DATABASE_URL` and the backend-neutral repository factory. Database URLs and credentials are never included in readiness output.

## Initialize or migrate the schema

Schema changes are always explicit and non-destructive. From the repository root, with the project virtual environment active, run:

```bash
python -m backend.app.clinical.cli migrate
```

For PostgreSQL, this applies only pending ordered migrations. For SQLite, it runs the current SQLite initializer and schema repair. Normal API startup, readiness checks, and workload commands do not create or migrate storage.

A legacy SQLite path can still be selected explicitly:

```bash
python -m backend.app.clinical.cli migrate \
  --database backend/data/trialmatch.sqlite3
```

`--database` accepts a SQLite filesystem path, not a database URL. When it is omitted, `TRIALMATCH_DATABASE_URL` is authoritative.

## Run a refresh

```bash
python -m backend.app.clinical.cli ingest \
  --cache-dir backend/data/cache/clinicaltrials \
  --checkpoint backend/data/cache/clinicaltrials/ingestion_checkpoint.json \
  --report backend/data/cache/clinicaltrials/latest_ingestion_report.json \
  --page-size 1000 \
  --max-concurrency 4 \
  --min-request-interval-seconds 0.2 \
  --timeout-seconds 30 \
  --max-retries 3 \
  --max-retry-delay-seconds 60
```

This ordinary command resumes a matching checkpoint and reuses only detail caches whose schema, content hash, NCT ID, status, source-update value, and exact index-record hash validate. Use `--refresh` only when every detail must be fetched again from ClinicalTrials.gov; it bypasses valid detail caches but retains the same all-or-nothing publication rule.

The command performs deterministic source ingestion only. It does not invoke an LLM, AI extraction, summarization, or eligibility interpretation. Do not run the separate `extract` or `summarize` commands as part of this refresh.

A zero exit code means the full detail cohort was published. A nonzero exit code means the report is partial or failed; inspect `errors`, `incomplete_detail_ids`, `ma_unverified_records`, retry/rate-limit counts, and publication fields. `index_retrieved_at` records when a complete or partial index fetch finished, before detail retrieval began. `published_at` is set only after the transactional snapshot publication succeeds and is otherwise null; on a completed publication it is never earlier than `index_retrieved_at`. Do not replace or delete the prior database snapshot after a nonzero result. Records returned by the Massachusetts index that lack an explicit `MA` or `Massachusetts` detail location are reported and block publication. Non-Massachusetts locations and each facility's source recruitment status remain preserved in the study detail.

## Verify readiness

After ingestion, run the non-secret CLI check:

```bash
python -m backend.app.clinical.cli readiness
```

It exits zero only when the configured schema is current and the serving snapshot contains studies. Empty, outdated, invalid, or unavailable storage returns a nonzero exit with sanitized JSON that does not contain a URL, path, host, username, password, or raw driver error. The API equivalent is `GET /api/readiness`, which returns HTTP 200 only for the same ready state and sanitized HTTP 503 otherwise. `GET /api/health` remains a separate process-liveness check and does not inspect clinical storage.

## Operational recommendation

Run the command manually before demonstrations and after material source-data changes. For routine operations, an external operator may schedule it weekly during a low-traffic window, with alerting on a nonzero exit code and review of the JSON report before downstream site export. This repository intentionally installs no scheduler, cron entry, hosted job, or deployment automation.
