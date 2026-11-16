import pandas as pd

FEATURE_VERSION = "v1"

FEATURES = [
    "is_unregistered",
    "account_age_days",
    "user_prior_edits",
    "page_edits_24h",
    "size_delta",
    "is_new_page",
    "is_minor",
    "hour_utc",
    "weekday",
    "comment_len",
    "comment_is_auto",
    "tag_mw_blank",
    "tag_mw_replace",
    "tag_mobile_edit",
    "tag_visualeditor",
]


def rule_score(df: pd.DataFrame) -> pd.Series:
    """Number of triggered rules (design 5.3). The text rule needs diffs and is deferred."""
    unregistered = df["is_unregistered"].astype(bool)
    removal = (df["size_delta"] < -500) | df["tag_mw_blank"].astype(bool)
    no_summary = (df["comment_len"] == 0) & ~df["is_minor"].astype(bool)
    return unregistered.astype(int) + removal.astype(int) + no_summary.astype(int)
