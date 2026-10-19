# ukwiki-patrol

Ukrainian Wikipedia receives about 2,700 non-bot article edits per day, and about 1.5% of them are later reverted, mostly as vandalism or test edits.
Volunteer patrollers review recent changes in chronological order, so a harmful edit waits in the same queue as thousands of harmless ones.
ukwiki-patrol scores every new edit with the probability that it will be reverted and serves a ranked patrol queue.
The goal is that most edits that end up reverted appear in the top 10% of the queue within minutes of being saved.

Design document: [docs/design.md](docs/design.md).

## Quick start

```bash
make setup   # uv sync: installs the pinned environment
make lint    # ruff check .
uv run pre-commit install   # optional: run the linter on every commit
```

## Maturity scorecard

| Property | Evidence a reviewer checks | Added in |
|---|---|---|
| Repository reproducible from a clean clone | CI is green; `make setup` and `make lint` succeed on a fresh clone | Lab 1 |
| Design document exists and is current | `docs/design.md` follows the template, every section filled or marked as an assumption | Lab 1 |
