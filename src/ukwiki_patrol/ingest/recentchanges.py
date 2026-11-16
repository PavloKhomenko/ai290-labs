"""Collect edits from Ukrainian Wikipedia (main namespace, non-bot) with revert labels.

Output: one Parquet file per UTC day, data/raw/date=YYYY-MM-DD/edits.parquet.
Re-running the same interval overwrites the same partitions (idempotent).

Label:
  reverted      edit carries the `mw-reverted` tag at collection time
  reverted_at   timestamp of the first later revert edit on the same page seen in the data
  revert_delay_h  hours between the edit and that revert (label arrival delay)
Labels are a snapshot: an edit collected 1 hour after it was made may still be reverted later.
`collected_at` records when the snapshot was taken; re-collect old days to refresh labels.

Privacy: user names are replaced by a salted hash (`user_key`) before anything is written,
so no raw identifier reaches DVC, the warehouse or MLflow (design 6.5).

recentchanges keeps ~30 days of history; DVC keeps what was collected after that.

Usage:
  uv run python -m ukwiki_patrol.ingest.recentchanges --start 2026-09-20 --end 2026-10-19
  (needs WIKI_CONTACT and USER_HASH_SALT in the environment, see .env.example)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

API = "https://uk.wikipedia.org/w/api.php"
REVERT_TAGS = {"mw-rollback", "mw-undo", "mw-manual-revert"}
REVERTED_TAG = "mw-reverted"
MAX_TEXT = 4000


class Api:
    def __init__(self, contact: str, sleep: float = 0.1):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            f"kse-mlops-ukwiki-vandalism/0.1 (student course project; {contact})"
        )
        self.sleep = sleep

    def get(self, **params) -> dict:
        params = {**params, "format": "json", "formatversion": "2", "maxlag": "5"}
        for attempt in range(8):
            try:
                resp = self.session.get(API, params=params, timeout=60)
            except requests.RequestException as exc:
                wait = 2**attempt
                print(f"  network error ({exc}); retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = int(resp.headers.get("Retry-After", 2**attempt))
                print(f"  HTTP {resp.status_code}; retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                if data["error"].get("code") == "maxlag":
                    time.sleep(int(resp.headers.get("Retry-After", 5)))
                    continue
                raise RuntimeError(data["error"])
            time.sleep(self.sleep)
            return data
        raise RuntimeError(f"too many retries for {params}")


def iso(ts: dt.datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_recentchanges(api: Api, start: dt.datetime, end: dt.datetime) -> list[dict]:
    params = {
        "action": "query",
        "list": "recentchanges",
        "rcstart": iso(start),
        "rcend": iso(end),
        "rcdir": "newer",
        "rcnamespace": "0",
        "rctype": "edit|new",
        "rcshow": "!bot",
        "rcprop": "title|ids|sizes|flags|user|userid|timestamp|comment|tags",
        "rclimit": "max",
    }
    rows: list[dict] = []
    while True:
        data = api.get(**params)
        rows.extend(data.get("query", {}).get("recentchanges", []))
        if "continue" not in data:
            return rows
        params.update(data["continue"])


def fetch_registrations(api: Api, names: list[str]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for i in range(0, len(names), 50):
        data = api.get(
            action="query", list="users", ususers="|".join(names[i : i + 50]), usprop="registration"
        )
        for u in data.get("query", {}).get("users", []):
            if "missing" not in u and "invalid" not in u:
                out[u["name"]] = u.get("registration")
    return out


# Diffs are fetched on demand (patrol-api, Lab 3 explanations, Lab 5 agent), not in the daily batch.
_ADDED_CELL = re.compile(r'<td class="diff-addedline[^"]*">(.*?)</td>', re.DOTALL)
_DELETED_CELL = re.compile(r'<td class="diff-deletedline[^"]*">(.*?)</td>', re.DOTALL)
_INS = re.compile(r"<ins[^>]*>(.*?)</ins>", re.DOTALL)
_DEL = re.compile(r"<del[^>]*>(.*?)</del>", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


def _clean(fragment: str) -> str:
    return html.unescape(_TAG.sub("", fragment)).strip()


def _side_text(body: str, cell_re: re.Pattern, inline_re: re.Pattern) -> str:
    parts = []
    for cell in cell_re.findall(body):
        inline = inline_re.findall(cell)
        if inline:
            parts.extend(_clean(x) for x in inline)
        else:
            parts.append(_clean(cell))
    return "\n".join(p for p in parts if p)


def fetch_diff(api: Api, old_revid: int, revid: int) -> tuple[str | None, str | None]:
    if not old_revid:
        return None, None
    try:
        data = api.get(action="compare", fromrev=str(old_revid), torev=str(revid), prop="diff")
    except RuntimeError as exc:  # deleted/suppressed revisions
        print(f"  diff {old_revid}->{revid} unavailable: {exc}", file=sys.stderr)
        return None, None
    body = data.get("compare", {}).get("body", "") or ""
    added = _side_text(body, _ADDED_CELL, _INS)[:MAX_TEXT]
    removed = _side_text(body, _DELETED_CELL, _DEL)[:MAX_TEXT]
    return added, removed


def user_key(name: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{name}".encode()).hexdigest()[:16]


def build_frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "rcid": [r.get("rcid") for r in rows],
            "revid": [r.get("revid") for r in rows],
            "old_revid": [r.get("old_revid") for r in rows],
            "pageid": [r.get("pageid") for r in rows],
            "title": [r.get("title") for r in rows],
            "timestamp": pd.to_datetime([r.get("timestamp") for r in rows], utc=True),
            "user": [r.get("user") or "" for r in rows],
            "is_anon": [bool(r.get("anon", False)) for r in rows],
            "is_new_page": [r.get("type") == "new" for r in rows],
            "is_minor": [bool(r.get("minor", False)) for r in rows],
            "oldlen": [r.get("oldlen") for r in rows],
            "newlen": [r.get("newlen") for r in rows],
            "comment": [r.get("comment", "") for r in rows],
            "tags": [list(r.get("tags", [])) for r in rows],
        }
    )
    df = df.drop_duplicates("rcid").sort_values("timestamp").reset_index(drop=True)
    df["is_temp_account"] = df["user"].str.startswith("~")
    df["size_delta"] = df["newlen"].fillna(0) - df["oldlen"].fillna(0)
    df["is_revert"] = df["tags"].map(lambda t: bool(REVERT_TAGS & set(t)))
    df["reverted"] = df["tags"].map(lambda t: REVERTED_TAG in t)
    return df


def attach_revert_times(df: pd.DataFrame) -> pd.DataFrame:
    reverted_at = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]")
    for _, g in df.groupby("pageid", sort=False):
        revert_ts = g.loc[g["is_revert"], "timestamp"]
        if revert_ts.empty:
            continue
        for idx, ts in g.loc[g["reverted"], "timestamp"].items():
            later = revert_ts[revert_ts > ts]
            if not later.empty:
                reverted_at[idx] = later.min()
    df["reverted_at"] = reverted_at
    df["revert_delay_h"] = (df["reverted_at"] - df["timestamp"]).dt.total_seconds() / 3600
    return df


def collect(start: dt.datetime, end: dt.datetime, contact: str, salt: str) -> pd.DataFrame:
    """Edits saved in [start, end), user names already replaced by `user_key`."""
    api = Api(contact)
    rows = fetch_recentchanges(api, start, end)
    df = attach_revert_times(build_frame(rows))
    df = df[(df["timestamp"] >= start) & (df["timestamp"] < end)].copy()

    names = sorted(set(df.loc[~df["is_anon"] & ~df["is_temp_account"] & (df["user"] != ""), "user"]))
    regs = fetch_registrations(api, names)
    df["user_registration"] = pd.to_datetime(df["user"].map(regs), utc=True)

    df["user_key"] = df["user"].map(lambda u: user_key(u, salt))
    df["collected_at"] = pd.Timestamp.now(tz="UTC")
    return df.drop(columns="user").reset_index(drop=True)


def write_partitions(df: pd.DataFrame, out: Path) -> list[Path]:
    paths = []
    for date, g in df.groupby(df["timestamp"].dt.strftime("%Y-%m-%d")):
        part = out / f"date={date}"
        part.mkdir(parents=True, exist_ok=True)
        g.to_parquet(part / "edits.parquet", index=False)
        paths.append(part / "edits.parquet")
    return paths


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", required=True, help="UTC date, inclusive (YYYY-MM-DD)")
    p.add_argument("--end", required=True, help="UTC date, exclusive (YYYY-MM-DD)")
    p.add_argument("--out", default="data/raw")
    args = p.parse_args()

    start = dt.datetime.fromisoformat(args.start).replace(tzinfo=dt.UTC)
    end = dt.datetime.fromisoformat(args.end).replace(tzinfo=dt.UTC)
    df = collect(start, end, os.environ["WIKI_CONTACT"], os.environ["USER_HASH_SALT"])
    for path in write_partitions(df, Path(args.out)):
        print(path, file=sys.stderr)


if __name__ == "__main__":
    main()
