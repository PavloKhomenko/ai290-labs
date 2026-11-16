from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

import mlflow
import numpy as np

from ukwiki_patrol.llm.rag import answer, chat, file_hash, index_hash, load_config, retrieve

EXPERIMENT = "ukwiki-patrol-llm"


def sh(*cmd: str) -> str:
    # not imported from train.py: lightgbm and torch load two OpenMP runtimes and segfault on macOS
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def first_relevant_rank(chunks: list[dict], relevant: list[str]) -> int | None:
    return next((i for i, c in enumerate(chunks, 1) if c["doc_id"] in relevant), None)


def citations(text: str) -> set[str]:
    return {c.strip() for c in re.findall(r"\[([^\[\]]+)\]", text)}


def judge(cfg: dict, item: dict, answer_text: str) -> dict:
    prompt = Path(cfg["prompts"]["judge"]).read_text().format(
        question=item["question"], reference=item["reference"], answer=answer_text
    )
    r = chat({**cfg["llm"], **cfg["judge"]}, prompt)
    digit = re.search(r"[1-5]", r["text"])
    return {**r, "score": int(digit.group()) if digit else None}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--closed-book", action="store_true", help="LLM baseline without retrieval")
    p.add_argument("--set", action="append", default=[], help="config override, e.g. retrieval.top_k=10")
    p.add_argument("--limit", type=int, help="first N questions only (smoke test, not a real run)")
    p.add_argument("--allow-dirty", action="store_true", help="skip the clean-tree check (never for real runs)")
    args = p.parse_args()

    if not args.allow_dirty and sh("git", "status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("uncommitted changes: commit first so git_commit identifies the prompts and code")
    cfg = load_config(args.set)
    items = [json.loads(line) for line in Path(cfg["eval_set"]).read_text().splitlines()][: args.limit]
    prompt_file = cfg["prompts"]["closed_book" if args.closed_book else "answer"]

    rows = []
    for item in items:
        chunks = [] if args.closed_book else retrieve(cfg, item["question"])
        a = answer(cfg, item["question"], closed_book=args.closed_book, chunks=chunks)
        j = judge(cfg, item, a["text"])
        cited = citations(a["text"])
        rank = first_relevant_rank(chunks, item["relevant_docs"])
        rows.append({
            "id": item["id"], "question": item["question"], "relevant_docs": item["relevant_docs"],
            "retrieved": [c["chunk_id"] for c in chunks], "rank": rank,
            "answer": a["text"], "cited": sorted(cited), "judge_score": j["score"], "judge_raw": j["text"],
            "tokens_in": a["tokens_in"], "tokens_out": a["tokens_out"], "latency_s": a["latency_s"],
            "cost_usd": a["cost_usd"], "judge_cost_usd": j["cost_usd"],
        })
        print(f"{item['id']} rank={rank} judge={j['score']} {a['latency_s']:.2f}s cited={sorted(cited)}")

    lat = np.array([r["latency_s"] for r in rows])
    scores = [r["judge_score"] for r in rows if r["judge_score"] is not None]
    metrics = {
        "answer_score": float(np.mean(scores)),  # judge, 1-5
        "judge_failures": len(rows) - len(scores),
        "citation_accuracy": float(np.mean([bool(set(r["cited"]) & set(r["relevant_docs"])) for r in rows])),
        "latency_p50_s": float(np.percentile(lat, 50)),
        "latency_p95_s": float(np.percentile(lat, 95)),
        "tokens_in_total": sum(r["tokens_in"] for r in rows),
        "tokens_out_total": sum(r["tokens_out"] for r in rows),
        "tokens_in_per_request": float(np.mean([r["tokens_in"] for r in rows])),
        "tokens_out_per_request": float(np.mean([r["tokens_out"] for r in rows])),
        "cost_usd_total": sum(r["cost_usd"] for r in rows),
        "cost_usd_per_request": float(np.mean([r["cost_usd"] for r in rows])),
        "judge_cost_usd_total": sum(r["judge_cost_usd"] for r in rows),
        "n_questions": len(rows),
    }
    if not args.closed_book:
        k = cfg["retrieval"]["top_k"]
        metrics[f"hit_rate_at_{k}"] = float(np.mean([r["rank"] is not None for r in rows]))
        metrics[f"mrr_at_{k}"] = float(np.mean([1 / r["rank"] if r["rank"] else 0.0 for r in rows]))
        metrics["hit_rate_at_1"] = float(np.mean([r["rank"] == 1 for r in rows]))
        # cites a policy the retriever never returned: the answer came from the model, not the context
        metrics["ungrounded_citation_rate"] = float(np.mean([
            bool(set(r["cited"]) - {c.split("#")[0] for c in r["retrieved"]}) for r in rows
        ]))

    mode = "closed-book" if args.closed_book else "rag"
    tags = {
        "mode": mode,
        "git_commit": sh("git", "rev-parse", "HEAD"),
        "prompt_file": prompt_file,
        "prompt_hash": file_hash(prompt_file),
        "judge_prompt_hash": file_hash(cfg["prompts"]["judge"]),
        "eval_set_hash": file_hash(cfg["eval_set"]),
        "corpus_hash": file_hash(cfg["corpus"]),
        "overrides": " ".join(args.set) or "none",
    }
    params = {
        "mode": mode, "llm_model": cfg["llm"]["model"], "temperature": cfg["llm"]["temperature"],
        "max_tokens": cfg["llm"]["max_tokens"], "judge_model": cfg["judge"]["model"],
    }
    if not args.closed_book:
        tags["index_hash"] = index_hash(cfg)
        params |= {**cfg["index"], "top_k": cfg["retrieval"]["top_k"]}
    if args.limit:
        tags["smoke"] = "true"

    mlflow.set_experiment(EXPERIMENT)
    name = mode if not args.set else f"{mode} {' '.join(args.set)}"
    with mlflow.start_run(run_name=name, tags=tags) as run:
        mlflow.log_params(params)
        mlflow.log_metrics(metrics)
        mlflow.log_dict(cfg, "config.json")
        mlflow.log_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), "results.jsonl")
        for f in {prompt_file, cfg["prompts"]["judge"]}:
            mlflow.log_artifact(f, "prompts")
    print(json.dumps({k: round(v, 4) for k, v in metrics.items()}, indent=1))
    print(f"run {run.info.run_id} ({name})")


if __name__ == "__main__":
    main()
