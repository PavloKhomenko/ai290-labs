from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from functools import cache
from pathlib import Path

import requests
import yaml
from sqlalchemy import create_engine, text

CONFIG = Path("configs/llm.yaml")


def load_config(overrides: list[str] = ()) -> dict:
    """configs/llm.yaml with `section.key=value` overrides (values parsed as YAML)."""
    cfg = yaml.safe_load(CONFIG.read_text())
    for item in overrides:
        path, value = item.split("=", 1)
        *parents, leaf = path.split(".")
        node = cfg
        for p in parents:
            node = node[p]
        if leaf not in node:
            raise SystemExit(f"unknown config key {path}")
        node[leaf] = yaml.safe_load(value)
    return cfg


def file_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


def index_hash(cfg: dict) -> str:
    key = json.dumps({"corpus": file_hash(cfg["corpus"]), **cfg["index"]}, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def engine():
    url = os.environ.get("WAREHOUSE_URL") or (
        f"postgresql+psycopg2://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        "@localhost:5433/warehouse"
    )
    return create_engine(url)


@cache
def embedder(name: str):
    from sentence_transformers import SentenceTransformer  # slow import, only when embedding

    return SentenceTransformer(name, device="cpu")


def embed(cfg: dict, texts: list[str], kind: str) -> list[list[float]]:
    # e5 models are trained with "query: " / "passage: " prefixes
    name = cfg["index"]["embedding_model"]
    if "e5" in name:
        texts = [f"{kind}: {t}" for t in texts]
    return embedder(name).encode(texts, normalize_embeddings=True, batch_size=32).tolist()


def chunk(text_: str, size: int, overlap: int) -> list[str]:
    """Fixed-size character windows, each end moved back to the last whitespace so words stay whole."""
    chunks, start = [], 0
    while start < len(text_):
        end = min(start + size, len(text_))
        if end < len(text_):
            cut = text_.rfind(" ", start + size // 2, end)
            end = cut if cut > 0 else end
        chunks.append(text_[start:end].strip())
        if end == len(text_):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def build_index(cfg: dict) -> None:
    docs = [json.loads(line) for line in Path(cfg["corpus"]).read_text(encoding="utf-8").splitlines()]
    ix, h = cfg["index"], index_hash(cfg)
    rows = [
        {"chunk_id": f"{d['doc_id']}#{i}", "doc_id": d["doc_id"], "text": c}
        for d in docs
        for i, c in enumerate(chunk(d["text"], ix["chunk_size"], ix["chunk_overlap"]))
    ]
    t0 = time.perf_counter()
    # the title travels with every chunk: a chunk from the middle of a page still says which policy it is
    vectors = embed(cfg, [f"{r['doc_id']}: {r['text']}" for r in rows], "passage")
    dim = len(vectors[0])
    with engine().begin() as con:
        con.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        con.execute(text(
            f"CREATE TABLE IF NOT EXISTS policy_chunks_{dim} (index_hash text, chunk_id text, doc_id text,"
            f" text text, embedding vector({dim}), PRIMARY KEY (index_hash, chunk_id))"
        ))
        con.execute(text(f"DELETE FROM policy_chunks_{dim} WHERE index_hash = :h"), {"h": h})
        con.execute(
            text(f"INSERT INTO policy_chunks_{dim} VALUES (:h, :chunk_id, :doc_id, :text, CAST(:v AS vector))"),
            [{**r, "h": h, "v": str(v)} for r, v in zip(rows, vectors)],
        )
        n_docs, n_chunks = con.execute(text(
            f"SELECT count(DISTINCT doc_id), count(*) FROM policy_chunks_{dim} WHERE index_hash = :h"
        ), {"h": h}).one()
    print(f"index {h}: {n_docs} documents, {n_chunks} chunks, {ix} ({time.perf_counter() - t0:.0f}s)")


def retrieve(cfg: dict, question: str, k: int | None = None) -> list[dict]:
    k = k or cfg["retrieval"]["top_k"]
    v = embed(cfg, [question], "query")[0]
    # exact cosine scan: a few thousand rows need no HNSW index
    with engine().connect() as con:
        rows = con.execute(text(
            f"SELECT chunk_id, doc_id, text FROM policy_chunks_{len(v)} WHERE index_hash = :h"
            " ORDER BY embedding <=> CAST(:v AS vector) LIMIT :k"
        ), {"h": index_hash(cfg), "v": str(v), "k": k}).mappings().all()
    if not rows:
        raise SystemExit(f"index {index_hash(cfg)} is empty for this config: run `make index`")
    return [dict(r) for r in rows]


def chat(llm: dict, prompt: str) -> dict:
    """One call to an OpenAI-compatible chat endpoint. Returns text, provider-reported tokens, latency."""
    for attempt in range(8):
        t0 = time.perf_counter()
        resp = requests.post(
            f"{llm['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {os.environ[llm['api_key_env']]}"},
            json={
                "model": llm["model"],
                "messages": [{"role": "user", "content": prompt}],
                "temperature": llm["temperature"],
                "max_tokens": llm["max_tokens"],
            },
            timeout=120,
        )
        latency = time.perf_counter() - t0
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = min(60, 2**attempt * 2)
            print(f"  HTTP {resp.status_code}; retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        body = resp.json()
        usage = body.get("usage", {})
        tokens_in, tokens_out = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        # billed output includes hidden reasoning tokens: take what the provider reports, not what we see
        tokens_out = max(tokens_out, usage.get("total_tokens", 0) - tokens_in)
        return {
            "text": body["choices"][0]["message"].get("content") or "",
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_s": latency,
            "cost_usd": (tokens_in * llm["usd_per_mtok_in"] + tokens_out * llm["usd_per_mtok_out"]) / 1e6,
        }
    raise RuntimeError("LLM endpoint kept failing")


def answer(cfg: dict, question: str, closed_book: bool = False, chunks: list[dict] | None = None) -> dict:
    if closed_book:
        chunks, prompt = [], Path(cfg["prompts"]["closed_book"]).read_text().format(question=question)
    else:
        chunks = chunks if chunks is not None else retrieve(cfg, question)
        context = "\n\n".join(f"[{c['doc_id']}]\n{c['text']}" for c in chunks)
        prompt = Path(cfg["prompts"]["answer"]).read_text().format(question=question, context=context)
    return {**chat(cfg["llm"], prompt), "chunks": chunks}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["index", "ask"])
    p.add_argument("question", nargs="?")
    p.add_argument("--set", action="append", default=[], help="config override, e.g. index.chunk_size=400")
    args = p.parse_args()
    cfg = load_config(args.set)
    if args.command == "index":
        build_index(cfg)
    else:
        r = answer(cfg, args.question)
        print(r["text"], "\n\nsources:", [c["chunk_id"] for c in r["chunks"]])


if __name__ == "__main__":
    main()
