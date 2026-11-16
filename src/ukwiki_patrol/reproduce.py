from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import mlflow
import yaml

from ukwiki_patrol.train import MODEL_NAME

TOLERANCE = 0.001  # LightGBM runs single-threaded and deterministic; the gap only absorbs float noise


def main() -> None:
    client = mlflow.MlflowClient()
    run_id = sys.argv[1] if len(sys.argv) > 1 else client.get_model_version_by_alias(MODEL_NAME, "champion").run_id
    run = client.get_run(run_id)
    commit, data_hash = run.data.tags["git_commit"], run.data.tags["data_hash"]
    print(f"reproducing run {run_id}: commit {commit[:10]}, data {data_hash}")

    worktree = Path(tempfile.mkdtemp()) / "repo"
    subprocess.run(["git", "worktree", "add", "--detach", str(worktree), commit], check=True)
    try:
        dvc_md5 = yaml.safe_load((worktree / "data/features.dvc").read_text())["outs"][0]["md5"]
        if dvc_md5 != data_hash:
            raise SystemExit(f"commit {commit} points at data {dvc_md5}, run says {data_hash}")
        subprocess.run(["uv", "run", "--frozen", "dvc", "pull", "data/features.dvc"], cwd=worktree, check=True)
        subprocess.run(
            ["uv", "run", "--frozen", "python", "-m", "ukwiki_patrol.train", "--reproduce-of", run_id],
            cwd=worktree,
            check=True,
        )
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], check=True)

    new = client.search_runs(
        [run.info.experiment_id],
        filter_string=f"tags.reproduced_from = '{run_id}' and tags.mlflow.runName = 'lightgbm'",
        order_by=["attributes.start_time DESC"],
        max_results=1,
    )[0]
    old_auc, new_auc = run.data.metrics["pr_auc"], new.data.metrics["pr_auc"]
    ok = abs(old_auc - new_auc) <= TOLERANCE
    print(f"pr_auc original {old_auc:.6f}, reproduced {new_auc:.6f} (run {new.info.run_id}): "
          f"{'OK' if ok else 'MISMATCH'} within {TOLERANCE}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
