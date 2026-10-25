# TrialMatch

TrialMatch is a Hackachusetts 2026 Healthcare & Biotech (H5) research-access prototype. It connects patient-facing clinical-trial discovery and preliminary screening with researcher-facing Massachusetts access analysis and constrained outreach planning.

> **Clinical safety:** TrialMatch is not a medical device, diagnostic service, or definitive eligibility determination system. Screening results are preliminary, source-grounded, and must be confirmed by study staff. Only synthetic patient profiles may be used in development and demonstrations.

## Product surfaces

- **Participant Portal:** Search recruiting Massachusetts studies, review source-backed descriptions, and compare fictional example facts with published protocol requirements while preserving unresolved questions and evidence links.
- **Researcher Dashboard:** Explore Massachusetts trial-site distribution and aggregate Environmental Justice indicators.
- **Recruitment Optimizer:** Select modeled outreach locations under explicit resource constraints. Outputs estimate modeled coverage, not observed enrollment outcomes.

The integrated application includes the clinical discovery/screening API, persisted Massachusetts geography, a condition-independent SciPy MILP outreach optimizer, the Participant Portal, and the Researcher Workspace. Clinical storage supports SQLite and PostgreSQL through `TRIALMATCH_DATABASE_URL`; schema migration and snapshot population are explicit operations, and process health remains independent of data readiness.

## Architecture

TrialMatch is a modular monolith with a Next.js frontend and FastAPI backend:

```text
TrialMatch/
├── .kiro/                     # Committed Kiro steering and workstream specs
├── backend/
│   ├── app/
│   │   ├── clinical/          # Developer A ownership
│   │   ├── geography/         # Developer B ownership
│   │   ├── optimizer/         # Developer B ownership
│   │   └── main.py            # Shared composition and health endpoint
│   ├── data/                  # Tracked manifest; local raw/derived data ignored
│   └── tests/                 # Tests mirror module ownership
├── contracts/api.md           # Shared API contract
├── docs/                      # Architecture, provenance, schedule, limitations
├── frontend/                  # Shared Next.js/TypeScript/Tailwind shell
├── AGENTS.md                  # Ownership and collaboration rules
└── pyproject.toml             # Python dependencies and tooling
```

See [architecture](docs/architecture.md), [API contracts](contracts/api.md), and [agent ownership](AGENTS.md).

## Tech stack

- Frontend: Next.js, TypeScript, Tailwind CSS, MapLibre GL JS
- Backend: Python 3.11, FastAPI, Pydantic
- Storage/computation: SQLite or PostgreSQL 17, pandas, NumPy, SciPy MILP
- Testing: pytest, API/contract tests, property-based tests where meaningful, frontend type/build checks
- AI: configurable provider, Pydantic-validated structured output, cached extraction, deterministic rule evaluation

The architecture intentionally excludes Kubernetes, Redis, Celery, custom model training, and independently deployed microservices.

## Data sources

1. **ClinicalTrials.gov API v2:** Recruiting studies with Massachusetts locations. The provided URL uses `pageSize=20` and is not a complete dataset. Complete ingestion must follow `nextPageToken`; detailed records come from `/api/v2/studies/{NCTId}`.
2. **MassGIS EJ2020 ArcGIS service:** Aggregate Environmental Justice block-group fields. The provided query caps each response at 100 records and must be paginated with ArcGIS offsets.

Expected challenge descriptions (approximately 3,131 studies and 2,604 flagged block groups) are not verified counts. Actual ingestion must record acquisition time, schema results, observed counts, duplicate identifiers, missing values, filtering, and exclusions. See [data provenance](docs/data-provenance.md) and the machine-readable [source manifest](backend/data/manifest.json).

## Local setup

Prerequisites: Python 3.11, Node.js 20+, and npm.

### Backend

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
cp .env.example .env
python -m backend.app.clinical.cli migrate
python -m backend.app.clinical.cli readiness  # exits nonzero until studies are populated
uvicorn backend.app.main:app --reload
```

Backend processes and clinical CLI commands load the repository-root `.env` with `python-dotenv`; an already-exported process variable always takes precedence. When `--database` is omitted, every clinical command uses `TRIALMATCH_DATABASE_URL` and the backend-neutral repository factory. The legacy `--database backend/data/trialmatch.sqlite3` option remains available as an explicit SQLite path override. Normal startup and workload commands never create or migrate clinical storage; run `migrate` explicitly, then populate a complete snapshot.

`GET http://localhost:8000/api/health` reports process liveness and remains independent of storage. `GET http://localhost:8000/api/readiness` returns 200 only when the configured clinical schema is current and contains at least one study; empty or unavailable storage returns a sanitized 503. The equivalent non-secret operator check is `python -m backend.app.clinical.cli readiness`.

### Frontend

```bash
cd frontend
npm ci
npm run dev
```

Environment variables are documented in `.env.example`. Copy it to `.env` or the framework-specific local environment file; never commit populated secrets. `TRIALMATCH_API_BASE_URL` is server-only and preferred for production; `NEXT_PUBLIC_API_BASE_URL` is browser-visible and intended only for local/direct-browser development.

## Deployment and release

- [Render FastAPI and Vercel Next.js deployment](docs/deployment.md)
- [End-to-end demo/release checklist](docs/release-checklist.md)

Clinical schema setup and ingestion are explicit one-off commands; neither application startup nor ordinary serving requests create an empty database. Keep runtime credentials only in the deployment environment, verify the configured store with `/api/readiness`, and do not commit generated clinical data. This change does not add or modify deployment configuration. Vercel uses same-origin proxying for clinical, geography, and optimizer requests. CI runs the complete backend suite plus frontend tests, typecheck, and production build from clean dependency installs.

## Validation

Run finite checks from the repository root:

```bash
python -m compileall backend
pytest
cd frontend && npm test && npm run typecheck && npm run build
```

Feature branches add tests alongside implementation. Never report a test, ingestion, integration, or demo as successful unless it was executed and verified.

## Branch workflow and deadline

The common scaffold is created on `main`. Both developers branch from the same verified scaffold commit:

```bash
git switch main
git pull --ff-only origin main
git switch -c feature/clinical-intelligence   # Developer A
# or: git switch -c feature/geo-optimization # Developer B
```

Internal feature freeze is **October 24, 2026 at 6:30 PM Eastern**. The detailed sprint schedule is in [docs/sprints.md](docs/sprints.md).

## Documentation

- [Deployment](docs/deployment.md)
- [Release checklist](docs/release-checklist.md)
- [Architecture](docs/architecture.md)
- [Data provenance and validation](docs/data-provenance.md)
- [API contracts](contracts/api.md)
- [Sprint plan](docs/sprints.md)
- [Kiro usage](docs/kiro-usage.md)
- [Limitations and safety](docs/limitations.md)
- [Clinical Intelligence spec](.kiro/specs/clinical-intelligence/requirements.md)
- [Geographic/Optimization spec](.kiro/specs/geo-optimization/requirements.md)
