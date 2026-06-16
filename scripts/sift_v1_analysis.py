#!/usr/bin/env python
"""Quantitative analysis of sift v1 labels against the production score.

Reads /tmp/sift_v1_active.db (or $SIFT_DB) and produces a markdown report with:
    - per-tier mean of every score_breakdown component
    - per-tier feature distributions (carpet, EPC, photos, basement, floor)
    - Spearman rank correlation between user tier ordering and the production score
    - top-K disagreement cases (high score but tiered low; low score but tiered high)

Output is written to docs/sift_v1/2026-05-10-sift-v1-component-analysis.md.

No model. No third-party calls. Pure SQL + pandas.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

import pandas as pd
from scipy.stats import spearmanr

DB_PATH = os.environ.get("SIFT_DB", "/tmp/sift_v1_active.db")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = PROJECT_ROOT / "docs" / "sift_v1" / "2026-05-10-sift-v1-component-analysis.md"

TIER_RANK = {"best": 4, "good": 3, "bad": 2, "worse": 1}
TIER_ORDER = ["best", "good", "bad", "worse"]


def load_dataframe() -> pd.DataFrame:
    con = sqlite3.connect(DB_PATH)
    sql = """
        SELECT
            l.tier            AS tier,
            l.uid             AS uid,
            l.reasoning       AS reasoning,
            l.position        AS position,
            li.score          AS score,
            li.score_breakdown AS score_breakdown,
            li.price_numeric  AS price,
            li.bedrooms       AS bedrooms,
            li.size_sqft      AS size_sqft,
            li.carpet_in_bedroom AS carpet_bedroom_flag,
            li.carpet_other_areas AS carpet_other_flag,
            li.images         AS images,
            li.floor_level    AS floor_level,
            li.tube_distance  AS tube_distance,
            li.area           AS area,
            li.address        AS address,
            li.title          AS title,
            li.url            AS url,
            li.floorplan_data AS floorplan_data
        FROM sifting_label_v2 l
        JOIN listing li ON l.uid = li.uid
        WHERE l.session_id = (SELECT MAX(id) FROM sifting_session_v2)
    """
    df = pd.read_sql_query(sql, con)
    con.close()

    parsed = df["score_breakdown"].apply(
        lambda s: json.loads(s) if isinstance(s, str) and s else {}
    )
    component_df = pd.json_normalize(parsed).add_prefix("comp_")
    df = pd.concat([df, component_df], axis=1)

    df["tier_rank"] = df["tier"].map(TIER_RANK)
    df["photo_count"] = df["images"].apply(_count_images)
    return df


def _count_images(raw: object) -> int:
    if not isinstance(raw, str) or not raw:
        return 0
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if isinstance(parsed, list):
        return len(parsed)
    if isinstance(parsed, dict) and "urls" in parsed:
        return len(parsed["urls"])
    return 0


def per_tier_component_means(df: pd.DataFrame) -> pd.DataFrame:
    component_cols = [
        "comp_price", "comp_size", "comp_bedrooms", "comp_epc", "comp_transport", "comp_outside",
        "comp_carpet_bedroom", "comp_carpet_other_areas", "comp_region_pref",
    ]
    component_cols = [c for c in component_cols if c in df.columns]
    grouped = df.groupby("tier")[component_cols].mean().round(1)
    grouped = grouped.reindex(TIER_ORDER)
    grouped["count"] = df.groupby("tier").size().reindex(TIER_ORDER)
    grouped["mean_score"] = df.groupby("tier")["score"].mean().reindex(TIER_ORDER).round(2)
    return grouped


def per_tier_feature_distributions(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}

    out["carpet_bedroom"] = (
        df.groupby(["tier", "carpet_bedroom_flag"]).size().unstack(fill_value=0).reindex(TIER_ORDER)
    )
    out["carpet_other_areas"] = (
        df.groupby(["tier", "carpet_other_flag"]).size().unstack(fill_value=0).reindex(TIER_ORDER)
    )
    out["floor_level"] = (
        df.groupby(["tier", "floor_level"]).size().unstack(fill_value=0).reindex(TIER_ORDER)
    )
    out["bedrooms"] = (
        df.groupby(["tier", "bedrooms"]).size().unstack(fill_value=0).reindex(TIER_ORDER)
    )

    photo_summary = df.groupby("tier")["photo_count"].agg(["mean", "min", "max"]).round(1)
    out["photo_count"] = photo_summary.reindex(TIER_ORDER)

    size_summary = df.groupby("tier")["size_sqft"].agg(["mean", "min", "max", "count"]).round(0)
    out["size_sqft"] = size_summary.reindex(TIER_ORDER)

    return out


def rank_correlation(df: pd.DataFrame) -> dict[str, float]:
    rho, p = spearmanr(df["tier_rank"], df["score"])
    return {"spearman_rho": round(float(rho), 3), "p_value": round(float(p), 4)}


def disagreement_cases(df: pd.DataFrame, k: int = 6) -> dict[str, pd.DataFrame]:
    cols = ["uid", "tier", "score", "area", "reasoning"]
    overrated = df[df["tier"].isin(["bad", "worse"])].nlargest(k, "score")[cols]
    underrated = df[df["tier"].isin(["best", "good"])].nsmallest(k, "score")[cols]
    return {"score_overrated": overrated, "score_underrated": underrated}


def reasoning_token_frequency(df: pd.DataFrame) -> dict[str, list[tuple[str, int]]]:
    out = {}
    for tier in TIER_ORDER:
        tokens: list[str] = []
        for r in df[df["tier"] == tier]["reasoning"].dropna():
            tokens.extend(_tokenize(r))
        counter = Counter(tokens)
        out[tier] = counter.most_common(15)
    return out


def _tokenize(text: str) -> list[str]:
    cleaned = text.lower()
    for ch in [",", ".", ";", ":", "(", ")", "!", "?", "/", "\\"]:
        cleaned = cleaned.replace(ch, " ")
    stop = {
        "the", "a", "an", "and", "or", "but", "is", "are", "in", "on", "of", "to",
        "for", "with", "without", "as", "at", "by", "this", "that", "it", "be",
        "has", "have", "had", "not", "no", "yes", "i", "its", "it's", "very",
        "too", "much", "lot", "lots", "some", "any", "more", "less", "most",
        "least", "than", "then", "so", "if", "do", "does", "did", "be", "being",
        "been", "from", "into", "out", "off", "up", "down", "all", "each", "every",
        "few", "many", "good", "bad", "ok", "okay", "alright",
    }
    return [t for t in cleaned.split() if len(t) > 2 and t not in stop]


def _df_to_md(df: pd.DataFrame, index: bool = True) -> str:
    if index:
        df = df.reset_index()
    cols = list(df.columns)
    head = "| " + " | ".join(str(c) for c in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body_rows: list[str] = []
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            v = row[c]
            if pd.isna(v):
                cells.append("")
            elif isinstance(v, float):
                cells.append(f"{v:.2f}".rstrip("0").rstrip("."))
            else:
                cells.append(str(v))
        body_rows.append("| " + " | ".join(cells) + " |")
    return "\n".join([head, sep, *body_rows])


def render_report(df: pd.DataFrame) -> str:
    means = per_tier_component_means(df)
    dists = per_tier_feature_distributions(df)
    corr = rank_correlation(df)
    disagree = disagreement_cases(df)
    tokens = reasoning_token_frequency(df)

    lines: list[str] = []
    lines.append("# Sift v1 component analysis")
    lines.append("")
    lines.append("**Date:** 2026-05-10. Generated by `scripts/sift_v1_analysis.py`.")
    lines.append("")
    lines.append("## Headline numbers")
    lines.append("")
    lines.append(f"- Total labelled listings: {len(df)}")
    lines.append(f"- Sift session id: {int(df['uid'].count())} entries from session 1 (started 2026-05-09)")
    lines.append(f"- Spearman rank correlation (user tier vs production score): **{corr['spearman_rho']}** (p={corr['p_value']})")
    lines.append("")
    lines.append("Spearman target for calibration is 0.7. Anything below 0.3 means the score has near-zero relationship to user preference.")
    lines.append("")

    lines.append("## Per-tier component means")
    lines.append("")
    lines.append("Means of each `score_breakdown` component, per user-assigned tier. Rows ordered best -> worse. A well-calibrated component shows monotonic decline from best to worse.")
    lines.append("")
    lines.append(_df_to_md(means))
    lines.append("")

    lines.append("## Feature distributions")
    lines.append("")

    for label, frame in dists.items():
        lines.append(f"### {label}")
        lines.append("")
        lines.append(_df_to_md(frame))
        lines.append("")

    lines.append("## Disagreement cases")
    lines.append("")
    lines.append("### Highest-scored listings the user labelled bad or worse")
    lines.append("")
    lines.append(_df_to_md(disagree["score_overrated"], index=False))
    lines.append("")
    lines.append("### Lowest-scored listings the user labelled best or good")
    lines.append("")
    lines.append(_df_to_md(disagree["score_underrated"], index=False))
    lines.append("")

    lines.append("## Reasoning token frequency per tier")
    lines.append("")
    lines.append("Top 15 content words per tier from the reasoning text. Stopwords and obvious tier-words are excluded. This is a crude lexical lens; the qwen3:4b theme pass produces the proper clustering.")
    lines.append("")
    for tier in TIER_ORDER:
        lines.append(f"### {tier}")
        lines.append("")
        items = tokens[tier]
        if not items:
            lines.append("(no reasoning text)")
            lines.append("")
            continue
        for word, count in items:
            lines.append(f"- `{word}` ({count})")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    df = load_dataframe()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report = render_report(df)
    REPORT_PATH.write_text(report)
    print(f"Report written to {REPORT_PATH}")
    print(f"N = {len(df)} labelled listings")
    print(f"Spearman rho = {rank_correlation(df)['spearman_rho']}")


if __name__ == "__main__":
    main()
