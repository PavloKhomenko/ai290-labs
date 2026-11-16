from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import lightgbm as lgb
import mlflow
import numpy as np
import pandas as pd
import yaml
from sklearn.metrics import average_precision_score, precision_recall_curve

from ukwiki_patrol.features import FEATURE_VERSION, FEATURES, rule_score

MODEL_NAME = "ukwiki-patrol"
FEATURES_DVC = Path("data/features.dvc")
LGB_PARAMS = {
    "n_estimators": 300,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 50,
    "random_state": 42,
    "n_jobs": 1,
    "deterministic": True,
    "force_row_wise": True,
    "verbose": -1,
}


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def lineage(allow_dirty: bool) -> dict[str, str]:
    """The data and code this run reads, refusing if either differs from what is committed."""
    if not allow_dirty:
        if sh("git", "status", "--porcelain", "--untracked-files=no"):
            raise SystemExit("uncommitted changes: commit first so git_commit identifies the code")
        if json.loads(sh("dvc", "status", "--json", str(FEATURES_DVC))):
            raise SystemExit("data/features differs from data/features.dvc: run `make data-version`")
    return {
        "data_hash": yaml.safe_load(FEATURES_DVC.read_text())["outs"][0]["md5"],
        "git_commit": sh("git", "rev-parse", "HEAD"),
        "feature_version": FEATURE_VERSION,
    }


def time_split(df: pd.DataFrame, test_days: int, gap_days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = sorted(df["date"].unique())
    train_dates = dates[: -(test_days + gap_days)]
    if len(train_dates) < 7:
        raise SystemExit(f"only {len(dates)} matured days; need at least {7 + test_days + gap_days}")
    return df[df["date"].isin(train_dates)], df[df["date"].isin(dates[-test_days:])]


def capture_at_10(test: pd.DataFrame, score: np.ndarray) -> float:
    """Share of a day's reverted edits ranked in that day's top 10%, averaged over days.
    Ties are broken by recency, as in the patrol queue."""
    d = test.assign(score=score).sort_values(["score", "ts"], ascending=False)
    shares = []
    for _, g in d.groupby("date"):
        top = g.head(int(np.ceil(0.1 * len(g))))
        if g["reverted"].sum():
            shares.append(top["reverted"].sum() / g["reverted"].sum())
    return float(np.mean(shares))


def evaluate(test: pd.DataFrame, score: np.ndarray) -> dict[str, float]:
    y = test["reverted"].astype(int)
    precision, recall, _ = precision_recall_curve(y, score)
    return {
        "pr_auc": float(average_precision_score(y, score)),
        "capture_at_10": capture_at_10(test, score),
        "recall_at_p30": float(recall[precision >= 0.30].max(initial=0.0)),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--test-days", type=int, default=5)
    p.add_argument("--gap-days", type=int, default=7, help="label maturity window, design 5.4")
    p.add_argument("--reproduce-of", help="run id being reproduced; the new run is not registered")
    p.add_argument("--allow-dirty", action="store_true", help="skip lineage checks (never for real runs)")
    args = p.parse_args()

    tags = lineage(args.allow_dirty)
    if args.reproduce_of:
        tags["reproduced_from"] = args.reproduce_of

    df = pd.read_parquet("data/features", partitioning=None)
    # whole days only: a partly matured day would bias that day's capture@10%
    df = df[df.groupby("date")["label_mature"].transform("all")].reset_index(drop=True)
    train, test = time_split(df, args.test_days, args.gap_days)
    split = {
        "train_from": str(train["date"].min()), "train_to": str(train["date"].max()),
        "test_from": str(test["date"].min()), "test_to": str(test["date"].max()),
        "gap_days": args.gap_days, "train_rows": len(train), "test_rows": len(test),
        "train_positives": int(train["reverted"].sum()), "test_positives": int(test["reverted"].sum()),
    }

    mlflow.set_experiment(MODEL_NAME)
    with mlflow.start_run(run_name="rule-baseline", tags=tags) as run:
        mlflow.log_params(split)
        rule = evaluate(test, rule_score(test).to_numpy())
        mlflow.log_metrics(rule)
        print(f"rule-baseline run {run.info.run_id}")

    x_train, x_test = train[FEATURES].astype(float), test[FEATURES].astype(float)
    model = lgb.LGBMClassifier(**LGB_PARAMS).fit(x_train, train["reverted"].astype(int))
    with mlflow.start_run(run_name="lightgbm", tags=tags) as run:
        mlflow.log_params({**split, **LGB_PARAMS})
        model_metrics = evaluate(test, model.predict_proba(x_test)[:, 1])
        mlflow.log_metrics(model_metrics)
        info = mlflow.lightgbm.log_model(
            model,
            name="model",
            input_example=x_test.head(5),
            registered_model_name=None if args.reproduce_of else MODEL_NAME,
        )
        if not args.reproduce_of:
            client = mlflow.MlflowClient()
            for key in ("data_hash", "git_commit"):
                client.set_model_version_tag(MODEL_NAME, info.registered_model_version, key, tags[key])
            print(f"registered {MODEL_NAME} version {info.registered_model_version}")
        print(f"lightgbm run {run.info.run_id}")

    print(pd.DataFrame({"rule-baseline": rule, "lightgbm": model_metrics}).round(4).to_string())


if __name__ == "__main__":
    main()
