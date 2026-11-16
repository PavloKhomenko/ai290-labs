-include .env
export

.PHONY: setup lint up down data-version data-pull train promote reproduce corpus index llm-baseline rag-eval

setup:
	[ -f .env ] || cp .env.example .env
	uv sync

lint:
	uv run ruff check .

up:
	docker compose up -d --build

down:
	docker compose down

# Version point: snapshot raw and feature partitions written by the DAG, push them to MinIO.
data-version:
	uv run dvc add data/raw data/features
	uv run dvc push
	git add data/raw.dvc data/features.dvc data/.gitignore

data-pull:
	uv run dvc pull

train:
	uv run python -m ukwiki_patrol.train

promote:
	uv run python -c "import mlflow; mlflow.MlflowClient().set_registered_model_alias('ukwiki-patrol', 'champion', '$(VERSION)')"

reproduce:
	uv run python -m ukwiki_patrol.reproduce $(RUN_ID)

# Lab 3: LLM component. Settings live in configs/llm.yaml; SET="key=value ..." overrides one run.
corpus:
	uv run python -m ukwiki_patrol.llm.corpus

index:
	uv run python -m ukwiki_patrol.llm.rag index $(addprefix --set ,$(SET))

llm-baseline:
	uv run python -m ukwiki_patrol.llm.evaluate --closed-book

rag-eval:
	uv run python -m ukwiki_patrol.llm.evaluate $(addprefix --set ,$(SET))
