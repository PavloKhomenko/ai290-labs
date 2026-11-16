# ukwiki-patrol

Ukrainian Wikipedia receives about 2,700 non-bot article edits per day, and about 1.5% of them are later reverted, mostly as vandalism or test edits.
Volunteer patrollers review recent changes in chronological order, so a harmful edit waits in the same queue as thousands of harmless ones.
ukwiki-patrol scores every new edit with the probability that it will be reverted and serves a ranked patrol queue.
The model is retrained weekly, because editing behaviour and vandal tactics keep changing.
The goal is that most edits that end up reverted appear in the top 10% of the queue within minutes of being saved.

Design document: [docs/design.md](docs/design.md).

## Quick start

Requires Docker, [uv](https://docs.astral.sh/uv/), Python 3.12+, GNU make.

```bash
make setup   # creates .env from .env.example, installs the pinned environment (uv.lock)
make lint    # ruff
make up      # MinIO :9000/:9001, PostgreSQL :5433, MLflow :5001, Airflow :8080
```

| Service | URL | Login |
|---|---|---|
| Airflow | http://localhost:8080 | none (local SimpleAuthManager, all admins) |
| MLflow | http://localhost:5001 | none |
| MinIO console | http://localhost:9001 | `minioadmin` / `minioadmin` (from `.env`) |

## Data

<!-- DATA -->

## Pipeline

`dags/ukwiki_daily.py`, one run per UTC day:
`extract → validate → transform → load → build_features → quality_check`.
The logic lives in `src/ukwiki_patrol/pipeline.py`; the DAG only wires it.

<!-- PIPELINE -->

## Baseline and model

<!-- MODEL -->

## Maturity scorecard

| Property | Evidence a reviewer checks | Added in |
|---|---|---|
| Repository reproducible from a clean clone | CI is green; `make setup` and `make lint` succeed on a fresh clone | Lab 1 |
| Design document exists and is current | `docs/design.md` follows the template, every section filled or marked as an assumption | Lab 1 |
<!-- SCORECARD -->
