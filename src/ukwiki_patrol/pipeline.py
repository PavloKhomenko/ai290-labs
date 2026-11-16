from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

from ukwiki_patrol.features import FEATURE_VERSION
from ukwiki_patrol.ingest import recentchanges

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
LABEL_MATURITY = pd.Timedelta(days=7)
RAW_COLUMNS = {
    "revid", "pageid", "timestamp", "user_key", "is_anon", "is_temp_account", "is_new_page",
    "is_minor", "size_delta", "comment", "tags", "reverted", "revert_delay_h",
    "user_registration", "collected_at",
}

DDL = """
CREATE TABLE IF NOT EXISTS clean_edits (
    revid BIGINT, pageid BIGINT, ts TIMESTAMPTZ, date DATE, user_key TEXT,
    is_unregistered BOOLEAN, account_age_days DOUBLE PRECISION, is_new_page BOOLEAN,
    is_minor BOOLEAN, size_delta BIGINT, comment_len INT, comment_is_auto BOOLEAN,
    tag_mw_blank BOOLEAN, tag_mw_replace BOOLEAN, tag_mobile_edit BOOLEAN,
    tag_visualeditor BOOLEAN, reverted BOOLEAN, revert_delay_h DOUBLE PRECISION,
    label_mature BOOLEAN, collected_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS clean_edits_date ON clean_edits (date);
CREATE TABLE IF NOT EXISTS features (
    revid BIGINT, ts TIMESTAMPTZ, date DATE, feature_version TEXT,
    is_unregistered BOOLEAN, account_age_days DOUBLE PRECISION, user_prior_edits INT,
    page_edits_24h INT, size_delta BIGINT, is_new_page BOOLEAN, is_minor BOOLEAN,
    hour_utc INT, weekday INT, comment_len INT, comment_is_auto BOOLEAN,
    tag_mw_blank BOOLEAN, tag_mw_replace BOOLEAN, tag_mobile_edit BOOLEAN,
    tag_visualeditor BOOLEAN, reverted BOOLEAN, label_mature BOOLEAN
);
CREATE INDEX IF NOT EXISTS features_date ON features (date);
"""


def engine():
    return create_engine(os.environ["WAREHOUSE_URL"])


def _part(layer: str, date: str, name: str) -> Path:
    path = DATA_DIR / layer / f"date={date}" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def extract(date: str) -> str:
    start = dt.datetime.fromisoformat(date).replace(tzinfo=dt.UTC)
    df = recentchanges.collect(
        start, start + dt.timedelta(days=1), os.environ["WIKI_CONTACT"], os.environ["USER_HASH_SALT"]
    )
    path = _part("raw", date, "edits.parquet")
    df.to_parquet(path, index=False)
    return str(path)


def validate(raw_path: str) -> None:
    df = pd.read_parquet(raw_path)
    missing = RAW_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"raw schema changed, missing columns: {sorted(missing)}")
    if df.empty:
        raise ValueError(f"{raw_path} has no rows (recentchanges keeps only ~30 days)")


def transform(raw_path: str, date: str, corrupt: bool = False) -> str:
    raw = pd.read_parquet(raw_path)
    tags = raw["tags"].map(set)
    unregistered = raw["is_anon"] | raw["is_temp_account"]
    df = pd.DataFrame(
        {
            "revid": raw["revid"],
            "pageid": raw["pageid"],
            "ts": raw["timestamp"],
            "date": pd.to_datetime(date).date(),
            "user_key": raw["user_key"],
            "is_unregistered": unregistered,
            "account_age_days": ((raw["timestamp"] - raw["user_registration"]).dt.total_seconds() / 86400)
            .where(~unregistered),
            "is_new_page": raw["is_new_page"],
            "is_minor": raw["is_minor"],
            "size_delta": raw["size_delta"].astype("int64"),
            "comment_len": raw["comment"].fillna("").str.len(),
            "comment_is_auto": raw["comment"].fillna("").str.fullmatch(r"/\*.*\*/\s*"),
            "tag_mw_blank": tags.map(lambda t: "mw-blank" in t),
            "tag_mw_replace": tags.map(lambda t: "mw-replace" in t),
            "tag_mobile_edit": tags.map(lambda t: "mobile edit" in t),
            "tag_visualeditor": tags.map(lambda t: "visualeditor" in t),
            "reverted": raw["reverted"],
            "revert_delay_h": raw["revert_delay_h"],
            "label_mature": raw["collected_at"] - raw["timestamp"] >= LABEL_MATURITY,
            "collected_at": raw["collected_at"],
        }
    )
    if corrupt:  # demo of the quality gate: an upstream pagination bug repeats 20% of the rows
        df = pd.concat([df, df.sample(frac=0.2, random_state=0)])
    path = _part("clean", date, "edits.parquet")
    df.to_parquet(path, index=False)
    return str(path)


def load(clean_path: str, date: str) -> int:
    df = pd.read_parquet(clean_path)
    with engine().begin() as conn:
        conn.execute(text(DDL))
        conn.execute(text("DELETE FROM clean_edits WHERE date = :d"), {"d": date})
        df.to_sql("clean_edits", conn, if_exists="append", index=False)
    return len(df)


FEATURES_SQL = """
WITH w AS (
    SELECT c.*,
        count(*) OVER (PARTITION BY user_key ORDER BY ts, revid
                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS user_prior_edits,
        count(*) OVER (PARTITION BY pageid ORDER BY ts
                       RANGE BETWEEN INTERVAL '24 hours' PRECEDING AND CURRENT ROW) - 1 AS page_edits_24h
    FROM clean_edits c
    WHERE c.date <= :d
)
INSERT INTO features
SELECT revid, ts, date, :fv, is_unregistered, account_age_days, user_prior_edits, page_edits_24h,
    size_delta, is_new_page, is_minor,
    extract(hour FROM ts AT TIME ZONE 'UTC')::int, extract(isodow FROM ts AT TIME ZONE 'UTC')::int,
    comment_len, comment_is_auto, tag_mw_blank, tag_mw_replace, tag_mobile_edit, tag_visualeditor,
    reverted, label_mature
FROM w WHERE date = :d
"""


def build_features(date: str) -> str:
    """Fill the feature table for one date and export it as the offline (training) copy."""
    with engine().begin() as conn:
        conn.execute(text("DELETE FROM features WHERE date = :d"), {"d": date})
        conn.execute(text(FEATURES_SQL), {"d": date, "fv": FEATURE_VERSION})
        df = pd.read_sql(
            text("SELECT * FROM features WHERE date = :d ORDER BY ts, revid"), conn, params={"d": date}
        )
    path = _part("features", date, "features.parquet")
    df.to_parquet(path, index=False)
    return str(path)


def quality_check(date: str) -> dict:
    q = """
    SELECT
        (SELECT count(*) FROM features WHERE date = :d) AS n_features,
        (SELECT count(*) FROM clean_edits WHERE date = :d) AS n_clean,
        (SELECT count(*) - count(DISTINCT revid) FROM features WHERE date = :d) AS duplicate_revids,
        (SELECT count(*) FROM features WHERE date = :d
            AND (revid IS NULL OR ts IS NULL OR size_delta IS NULL)) AS null_keys,
        (SELECT count(*) FROM features WHERE date = :d
            AND (ts < CAST(:d AS date) OR ts >= CAST(:d AS date) + 1)) AS outside_interval,
        (SELECT avg(reverted::int) FROM features WHERE date = :d) AS label_rate,
        (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY n) FROM (
            SELECT count(*) AS n FROM features
            WHERE date >= CAST(:d AS date) - 7 AND date < CAST(:d AS date) GROUP BY date) h) AS median_7d
    """
    with engine().connect() as conn:
        m = dict(conn.execute(text(q), {"d": date}).mappings().one())

    failures = []
    if m["n_features"] == 0:
        failures.append("no rows")
    if m["n_features"] != m["n_clean"]:
        failures.append(f"features rows {m['n_features']} != clean rows {m['n_clean']}")
    if m["duplicate_revids"]:
        failures.append(f"{m['duplicate_revids']} duplicate revids")
    if m["null_keys"]:
        failures.append(f"{m['null_keys']} rows with null revid/ts/size_delta")
    if m["outside_interval"]:
        failures.append(f"{m['outside_interval']} rows outside {date}")
    if m["label_rate"] is not None and not 0.002 <= m["label_rate"] <= 0.10:
        failures.append(f"label rate {m['label_rate']:.4f} outside [0.002, 0.10]")
    if m["median_7d"] and m["n_features"] < 0.5 * m["median_7d"]:
        failures.append(f"{m['n_features']} rows < 50% of the 7-day median {m['median_7d']:.0f}")
    if failures:
        raise ValueError(f"quality check failed for {date}: " + "; ".join(failures))
    return {k: float(v) if v is not None else None for k, v in m.items()}
