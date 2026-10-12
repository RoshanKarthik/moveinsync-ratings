"""Turn stored reviews + tags into every number the report shows.

Conventions (kept identical everywhere so numbers can be compared):
- "Negative" = 1–2★. Shares of negative reviews are the main measure of what drives bad ratings.
- Every share comes with n and a 95% Wilson interval; changes are tested with a two-proportion
  z-test. Release comparisons are Bonferroni-corrected because many releases are tested at once.
- Months and dates are in India time (IST), where the users are.
- An issue's "rating cost" splits each negative review equally between the specific issues it
  names, so the costs of all issues add up instead of double counting.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .config import Settings
from .evaluate import wilson
from .store import Store
from .taxonomy import NON_SPECIFIC, ROOT_CAUSE_LABELS, Taxonomy

IST = "Asia/Kolkata"
NEG_MAX = 2


# ── small statistics helpers ──────────────────────────────────────────────────────────────────
def _phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def two_prop_p(k1: int, n1: int, k2: int, n2: int) -> float:
    """Two-sided p-value that proportions k1/n1 and k2/n2 differ (pooled z-test)."""
    if min(n1, n2) == 0:
        return 1.0
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = (k1 / n1 - k2 / n2) / se
    return 2 * (1 - _phi(abs(z)))


def share(k: int, n: int) -> dict[str, Any]:
    lo, hi = wilson(k, n)
    return {"k": int(k), "n": int(n), "p": (k / n) if n else 0.0, "lo": lo, "hi": hi}


def _mean_ci(scores: pd.Series) -> dict[str, Any]:
    n = int(scores.size)
    if n == 0:
        return {"mean": None, "n": 0, "lo": None, "hi": None}
    m = float(scores.mean())
    sd = float(scores.std(ddof=1)) if n > 1 else 0.0
    half = 1.96 * sd / math.sqrt(n) if n > 1 else 0.0
    return {"mean": m, "n": n, "lo": m - half, "hi": m + half}


# ── loading ───────────────────────────────────────────────────────────────────────────────────
def load_frame(store: Store, app_id: str) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT r.review_id, r.score, r.content, r.thumbs_up, r.app_version, r.at, r.reply_content,
               r.replied_at, t.issues, t.target, t.mentions_update, t.safety, t.org, t.detail,
               t.agreement, t.method
        FROM reviews r LEFT JOIN tags t ON t.review_id = r.review_id
        WHERE r.app_id = ?
        """,
        store.conn,
        params=(app_id,),
    )
    df["ts"] = pd.to_datetime(df["at"], utc=True).dt.tz_convert(IST)
    df["replied_ts"] = pd.to_datetime(df["replied_at"], utc=True, errors="coerce").dt.tz_convert(IST)
    df["issues"] = df["issues"].map(lambda s: json.loads(s) if isinstance(s, str) else None)
    df["tagged"] = df["issues"].notna()
    df["neg"] = df["score"] <= NEG_MAX
    df["replied"] = df["reply_content"].notna()
    df["month"] = df["ts"].dt.tz_localize(None).dt.to_period("M")
    df["quarter"] = df["ts"].dt.tz_localize(None).dt.to_period("Q")
    return df


def _specific(issues: list[str] | None) -> list[str]:
    return [c for c in (issues or []) if c not in NON_SPECIFIC]


def _has(frame: pd.DataFrame, code: str) -> pd.Series:
    """Boolean mask: rows tagged with `code` (always bool dtype, also for empty frames)."""
    return frame["issues"].map(lambda x: code in (x or [])).astype(bool)


def _any_specific(frame: pd.DataFrame) -> pd.Series:
    return frame["issues"].map(lambda x: bool(_specific(x))).astype(bool)


# ── sections ──────────────────────────────────────────────────────────────────────────────────
def listing_section(store: Store, settings: Settings) -> dict[str, Any]:
    rows = store.conn.execute("SELECT * FROM listings ORDER BY captured_at").fetchall()
    by_app: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_app.setdefault(r["app_id"], []).append(dict(r))

    def summary(app_id: str, name: str) -> dict[str, Any] | None:
        hist = by_app.get(app_id)
        if not hist:
            return None
        cur = hist[-1]
        h = [cur[f"h{i}"] or 0 for i in range(1, 6)]
        total = sum(h)
        return {
            "id": app_id,
            "name": name,
            "score": cur["score"],
            "ratings": cur["ratings"],
            "reviews": cur["reviews"],
            "installs": cur["installs"],
            "hist": h,
            "one_star": share(h[0], total),
            "neg": share(h[0] + h[1], total),
            "version": cur["version"],
            "captured_at": cur["captured_at"],
            "history": [
                {"at": x["captured_at"][:10], "at_full": x["captured_at"], "score": x["score"], "ratings": x["ratings"]}
                for x in hist
            ],
        }

    main = summary(settings.app.id, settings.app.name)
    benches = [b for b in (summary(a.id, a.name) for a in settings.benchmarks) if b]
    siblings = [b for b in (summary(a.id, a.name) for a in settings.sibling_apps) if b]
    return {"main": main, "benchmarks": benches, "siblings": siblings}


def rating_gap(df: pd.DataFrame, listing: dict[str, Any]) -> dict[str, Any]:
    """Store rating vs what written reviews say, and who the silent raters are.

    Google's histogram counts every rating; our reviews are the written subset. Subtracting the
    written reviews per star estimates the star-only raters (approximate: Play's counts are for
    all languages and drop some deleted reviews, ours are English)."""
    hist = listing["hist"]
    written = [int((df["score"] == s).sum()) for s in range(1, 6)]
    star_only = [max(h - w, 0) for h, w in zip(hist, written, strict=True)]
    so_total = sum(star_only)
    w_total = sum(written)
    return {
        "written": written,
        "star_only": star_only,
        "written_share_of_ratings": w_total / max(sum(hist), 1),
        "written_mean": sum((i + 1) * c for i, c in enumerate(written)) / max(w_total, 1),
        "star_only_mean": sum((i + 1) * c for i, c in enumerate(star_only)) / max(so_total, 1),
        "written_one_star": share(written[0], w_total),
        "star_only_one_star": share(star_only[0], so_total),
    }


def monthly_section(df: pd.DataFrame, months: int = 36) -> list[dict[str, Any]]:
    last = df["month"].max()
    first = last - (months - 1)
    out = []
    for m, g in df[df["month"] >= first].groupby("month"):
        n = len(g)
        out.append(
            {
                "month": str(m),
                "n": n,
                "mean": float(g["score"].mean()),
                "neg": share(int(g["neg"].sum()), n),
                "replied": share(int(g["replied"].sum()), n),
                "partial": m == last,
            }
        )
    return out


def reply_section(df: pd.DataFrame) -> dict[str, Any]:
    q = []
    for quarter, g in df[df["ts"] >= df["ts"].max() - pd.Timedelta(days=365 * 5)].groupby("quarter"):
        lag = (g["replied_ts"] - g["ts"]).dt.total_seconds() / 86400
        lag = lag[(lag >= 0) & g["replied"]]
        q.append(
            {
                "quarter": str(quarter),
                "n": len(g),
                "replied": share(int(g["replied"].sum()), len(g)),
                "replied_neg": share(int((g["replied"] & g["neg"]).sum()), int(g["neg"].sum())),
                "median_lag_days": float(lag.median()) if len(lag) else None,
            }
        )
    replied = df[df["replied"]]
    last_reply = replied["replied_ts"].max()
    # Most common reply texts — shows whether replies were templates or real answers.
    templates = Counter(
        re.sub(r"\s+", " ", t).strip()[:140]
        for t in replied[replied["ts"] >= replied["ts"].max() - pd.Timedelta(days=730)]["reply_content"]
    ).most_common(4)
    # Reviews whose text or stars changed after the developer replied (Play keeps only the latest
    # version, so the stars before the edit are unknown; `review_changes` records them from now on).
    edited = replied[replied["ts"] > replied["replied_ts"]]
    return {
        "quarters": q,
        "edited_after_reply": share(len(edited), len(replied)),
        "last_reply": last_reply.strftime("%d %b %Y") if pd.notna(last_reply) else None,
        "top_templates": [{"text": t, "n": n} for t, n in templates],
    }


def window_stats(df: pd.DataFrame, end: pd.Timestamp, days: int) -> dict[str, Any]:
    w = df[(df["ts"] > end - pd.Timedelta(days=days)) & (df["ts"] <= end)]
    return {
        "days": days,
        "n": len(w),
        "mean": _mean_ci(w["score"]),
        "neg": share(int(w["neg"].sum()), len(w)),
        "one_star": share(int((w["score"] == 1).sum()), len(w)),
        "five_star": share(int((w["score"] == 5).sum()), len(w)),
        "replied": share(int(w["replied"].sum()), len(w)),
        "replied_neg": share(int((w["replied"] & w["neg"]).sum()), int(w["neg"].sum())),
    }


def version_section(df: pd.DataFrame, end: pd.Timestamp, days: int, min_n: int) -> list[dict[str, Any]]:
    w = df[(df["ts"] > end - pd.Timedelta(days=days)) & df["app_version"].notna()]
    groups = []
    for v, g in w.groupby("app_version"):
        if len(g) >= min_n:
            groups.append((v, g))
    tests = len(groups) or 1
    out = []
    for v, g in groups:
        rest = w[w["app_version"] != v]
        k, n = int(g["neg"].sum()), len(g)
        p = two_prop_p(k, n, int(rest["neg"].sum()), len(rest))
        worse = k / n > rest["neg"].mean()
        out.append(
            {
                "version": v,
                "first_seen": g["ts"].min().strftime("%d %b %Y"),
                "first_seen_sort": g["ts"].min().isoformat(),
                "n": n,
                "mean": float(g["score"].mean()),
                "neg": share(k, n),
                "baseline_neg": float(rest["neg"].mean()),
                "p": p,
                # "worse"/"better": significant after Bonferroni correction for all releases tested.
                # "watch": p < 0.05 on its own, but could be chance given how many releases were tested.
                "flag": ("worse" if worse else "better")
                if p < 0.05 / tests
                else ("watch" if worse and p < 0.05 else ""),
                "tests": tests,
                "top_issues": _top_codes(g[g["neg"]], 3),
            }
        )
    out.sort(key=lambda x: x["first_seen_sort"])
    return out


def _top_codes(g: pd.DataFrame, k: int) -> list[tuple[str, int]]:
    c: Counter[str] = Counter()
    for issues in g["issues"].dropna():
        c.update(set(_specific(issues)))
    return c.most_common(k)


def issue_section(
    df: pd.DataFrame, tax: Taxonomy, end: pd.Timestamp, recent_days: int, year_days: int, eval_codes: dict[str, Any]
) -> dict[str, Any]:
    tagged = df[df["tagged"]]
    recent = tagged[(tagged["ts"] > end - pd.Timedelta(days=recent_days))]
    prior = tagged[
        (tagged["ts"] > end - pd.Timedelta(days=2 * recent_days))
        & (tagged["ts"] <= end - pd.Timedelta(days=recent_days))
    ]
    year = tagged[tagged["ts"] > end - pd.Timedelta(days=year_days)]

    def neg_counts(frame: pd.DataFrame) -> tuple[Counter[str], int]:
        c: Counter[str] = Counter()
        neg = frame[frame["neg"]]
        for issues in neg["issues"]:
            c.update(set(issues or []))
        return c, len(neg)

    rc, rn = neg_counts(recent)
    pc, pn = neg_counts(prior)
    yc, yn = neg_counts(year)

    # Rating cost over the year window: split each negative review across its specific issues.
    no_issue = year[~_any_specific(year) & ~year["neg"]]
    alt_score = float(no_issue["score"].mean()) if len(no_issue) else 4.5
    cost: Counter[str] = Counter()
    attributed: dict[str, float] = {}  # negative reviews split evenly between the issues they name
    for _, row in year[year["neg"]].iterrows():
        spec = _specific(row["issues"])
        for c in spec:
            cost[c] += (alt_score - row["score"]) / len(spec)
            attributed[c] = attributed.get(c, 0.0) + 1 / len(spec)
    n_year = len(year)

    issues: list[dict[str, Any]] = []
    for code in tax.specific_codes:
        meta = tax.issues[code]
        mentions = year[_has(year, code)]
        recent_m = recent[_has(recent, code)]
        p = two_prop_p(rc[code], rn, pc[code], pn)
        r_share = rc[code] / rn if rn else 0
        p_share = pc[code] / pn if pn else 0
        trend = "steady"
        if p < 0.05:
            trend = "rising" if r_share > p_share else "falling"
        issues.append(
            {
                "code": code,
                "label": meta.label,
                "stage": tax.stages[meta.stage],
                "owner": tax.owners[meta.owner],
                "root": meta.root,
                "root_label": ROOT_CAUSE_LABELS[meta.root],
                "recent": share(rc[code], rn),
                "prior": share(pc[code], pn),
                "year": share(yc[code], yn),
                "trend": trend,
                "trend_p": p,
                "mentions_year": len(mentions),
                "mean_stars_when_mentioned": float(mentions["score"].mean()) if len(mentions) else None,
                "upvotes_year": int(mentions["thumbs_up"].sum()),
                "blames_update": share(int(mentions["mentions_update"].fillna(0).astype(int).sum()), len(mentions)),
                "rating_cost": cost[code] / n_year if n_year else 0.0,
                "attributed_year": attributed.get(code, 0.0),
                "recent_unanswered": int((recent_m["neg"] & ~recent_m["replied"]).sum()),
                "examples": _examples(pd.concat([recent_m, mentions]).drop_duplicates("review_id"), 3),
                "details": _details(mentions),
                "tag_quality": eval_codes.get(code),
                "monthly": _monthly_share(tagged, code, end),
            }
        )
    issues.sort(key=lambda x: -float(x["rating_cost"]))
    for i, x in enumerate(issues, 1):
        x["rank"] = i

    # Why are reviews negative? Root-cause mix of the primary specific issue, last year.
    roots: Counter[str] = Counter()
    targets: Counter[str] = Counter()
    neg_year = year[year["neg"]]
    for _, row in neg_year.iterrows():
        spec = _specific(row["issues"])
        roots[tax.issues[spec[0]].root if spec else "unspecified"] += 1
        targets[row["target"] or "none"] += 1
    stages: Counter[str] = Counter()
    for issues_ in neg_year["issues"]:
        for st in {tax.issues[c].stage for c in _specific(issues_)}:
            stages[st] += 1

    return {
        "recent_neg_n": rn,
        "prior_neg_n": pn,
        "year_neg_n": yn,
        "year_n": n_year,
        "alt_score": alt_score,
        "issues": issues,
        "roots": {k: share(v, len(neg_year)) for k, v in roots.most_common()},
        "targets": {k: share(v, len(neg_year)) for k, v in targets.most_common()},
        "stages": [{"stage": tax.stages[k], "code": k, **share(v, len(neg_year))} for k, v in stages.most_common()],
        "specific_share": share(int(_any_specific(neg_year).sum()), len(neg_year)),
    }


def _monthly_share(tagged: pd.DataFrame, code: str, end: pd.Timestamp, months: int = 12) -> list[dict[str, Any]]:
    last = (
        end.tz_localize(None).to_period("M")
        if end.tzinfo is None
        else end.tz_convert(IST).tz_localize(None).to_period("M")
    )
    out = []
    neg = tagged[tagged["neg"]]
    for m in pd.period_range(last - (months - 1), last, freq="M"):
        g = neg[neg["month"] == m]
        k = int(_has(g, code).sum())
        out.append({"month": str(m), **share(k, len(g))})
    return out


def _examples(frame: pd.DataFrame, k: int) -> list[dict[str, Any]]:
    """Representative quotes: negative, recent, readable length, most upvoted first. No names."""
    f = frame[frame["neg"] & frame["content"].str.len().between(40, 420)]
    if f.empty:
        f = frame[frame["content"].str.len().between(15, 420)]
    f = f.assign(_len_ok=-(f["content"].str.len() - 160).abs()).sort_values(
        ["thumbs_up", "ts", "_len_ok"], ascending=[False, False, False]
    )
    return _quote_rows(f.head(k))


def unanswered_negatives(df: pd.DataFrame, codes: list[str], days: int) -> int:
    """Distinct 1–2★ reviews in the last `days` naming any of `codes` that have no reply."""
    w = df[(df["ts"] > df["ts"].max() - pd.Timedelta(days=days)) & df["neg"] & ~df["replied"]]
    return int(w["issues"].map(lambda x: bool(set(x or []) & set(codes))).astype(bool).sum())


def quotes_matching(
    df: pd.DataFrame,
    terms: list[str],
    *,
    codes: list[str] | None = None,
    version: str | None = None,
    k: int = 2,
    max_stars: int = 3,
) -> list[dict[str, Any]]:
    """Most recent ≤3★ reviews that contain any of `terms` (case-insensitive), name any of `codes`
    and/or come from app `version`. At least one filter is required. No names."""
    if not (terms or codes or version):
        return []
    keep = (df["score"] <= max_stars) & df["content"].fillna("").str.len().between(40, 420)
    if terms:
        text = df["content"].fillna("").str.lower()
        keep &= text.map(lambda t: any(term.lower() in t for term in terms)).astype(bool)
    if codes:
        keep &= df["issues"].map(lambda x: bool(set(x or []) & set(codes))).astype(bool)
    if version:
        keep &= df["app_version"] == version
    return _quote_rows(df[keep].sort_values("ts", ascending=False).head(k))


def _quote_rows(f: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {
            "text": r["content"].strip(),
            "stars": int(r["score"]),
            "date": r["ts"].strftime("%b %Y"),
            "version": r["app_version"],
            "upvotes": int(r["thumbs_up"]),
        }
        for _, r in f.iterrows()
    ]


def _details(frame: pd.DataFrame, k: int = 6) -> list[tuple[str, int]]:
    c: Counter[str] = Counter()
    for d in frame["detail"].dropna():
        d = re.sub(r"[^\w\s/'-]", "", d.lower()).strip()
        if d:
            c[d] += 1
    return c.most_common(k)


def emerging_section(df: pd.DataFrame, tax: Taxonomy, end: pd.Timestamp) -> list[dict[str, Any]]:
    """Issues whose share of negative reviews in the last 30 days is significantly above the
    previous 180 days — the early warning a weekly run exists for."""
    tagged = df[df["tagged"] & df["neg"]]
    last = tagged[tagged["ts"] > end - pd.Timedelta(days=30)]
    base = tagged[(tagged["ts"] <= end - pd.Timedelta(days=30)) & (tagged["ts"] > end - pd.Timedelta(days=210))]
    out = []
    for code in tax.specific_codes:
        k1 = int(_has(last, code).sum())
        k2 = int(_has(base, code).sum())
        if k1 < 3:
            continue
        p = two_prop_p(k1, len(last), k2, len(base))
        if p < 0.05 and k1 / max(len(last), 1) > k2 / max(len(base), 1):
            out.append(
                {
                    "code": code,
                    "label": tax.issues[code].label,
                    "last30": share(k1, len(last)),
                    "base": share(k2, len(base)),
                    "p": p,
                }
            )
    return sorted(out, key=lambda x: x["p"])


def org_section(df: pd.DataFrame, since: pd.Timestamp) -> list[dict[str, Any]]:
    w = df[(df["ts"] >= since) & df["org"].notna()]
    norm = w["org"].map(_norm_org)
    w = w[norm != "Moveinsync"]
    norm = norm[norm != "Moveinsync"]
    out = []
    for org, g in w.groupby(norm):
        if len(g) >= 3 and org:
            out.append(
                {
                    "org": org,
                    "n": len(g),
                    "neg": share(int(g["neg"].sum()), len(g)),
                    "top_issues": _top_codes(g[g["neg"]], 3),
                }
            )
    return sorted(out, key=lambda x: -x["n"])[:12]


_ORG_SUFFIX = re.compile(
    r"\b(ltd|limited|pvt|private|technologies|technology|tech|inc|india|corp|corporation|company|co)\b\.?", re.I
)


_ORG_ALIASES = {
    "jpmc": "JPMorgan Chase",
    "jp morgan": "JPMorgan Chase",
    "jpmorgan": "JPMorgan Chase",
    "jpmorgan chase": "JPMorgan Chase",
    "tcs": "TCS",
    "tata consultancy services": "TCS",
    "hcltech": "HCL",
    "hcl": "HCL",
    "techm": "Tech Mahindra",
    "tech mahindra": "Tech Mahindra",
    "cnx": "Concentrix",
    "concentrix": "Concentrix",
    "wf": "Wells Fargo",
    "wells fargo": "Wells Fargo",
}


def _norm_org(s: str) -> str:
    raw = re.sub(r"\s+", " ", s).strip(" .,-").lower()
    for key in (raw, raw.split(" ")[0], " ".join(raw.split(" ")[:2])):
        if key in _ORG_ALIASES:
            return _ORG_ALIASES[key]
    s = _ORG_SUFFIX.sub("", s).strip(" .,-")
    return s.upper() if len(s) <= 4 else s.title()


def safety_concerns(frame: pd.DataFrame) -> pd.Series:
    """Reviews raising a safety *concern*: flagged, and a complaint (≤3★ or another negative
    issue named). Excludes praise such as "safe ride ⭐⭐⭐⭐⭐", which models also flag."""
    flagged = frame["safety"].fillna(0).astype(bool) | _has(frame, "safety_concern")
    other_negative = (
        frame["issues"]
        .map(lambda x: any(c not in NON_SPECIFIC and c != "safety_concern" for c in (x or [])))
        .astype(bool)
    )
    return flagged & ((frame["score"] <= 3) | other_negative)


def safety_section(df: pd.DataFrame, end: pd.Timestamp, days: int) -> dict[str, Any]:
    w = df[(df["ts"] > end - pd.Timedelta(days=days))]
    s = w[safety_concerns(w)]
    praise = w[w["safety"].fillna(0).astype(bool) & ~safety_concerns(w)]
    return {
        "n": len(s),
        "of": len(w),
        "last30": int((s["ts"] > end - pd.Timedelta(days=30)).sum()),
        "praise": len(praise),
        "reviews": [
            {
                "text": r["content"].strip(),
                "stars": int(r["score"]),
                "date": r["ts"].strftime("%d %b %Y"),
                "replied": bool(r["replied"]),
            }
            for _, r in s.sort_values("ts", ascending=False).iterrows()
        ],
    }


def visible_section(df: pd.DataFrame, end: pd.Timestamp, days: int, k: int = 8) -> list[dict[str, Any]]:
    """Most-upvoted negative reviews: the ones Play shows first to every new employee."""
    w = df[(df["ts"] > end - pd.Timedelta(days=days)) & df["neg"]]
    w = w.sort_values(["thumbs_up", "ts"], ascending=False).head(k)
    return [
        {
            "text": r["content"].strip(),
            "stars": int(r["score"]),
            "upvotes": int(r["thumbs_up"]),
            "date": r["ts"].strftime("%d %b %Y"),
            "replied": bool(r["replied"]),
            "issues": _specific(r["issues"]),
        }
        for _, r in w.iterrows()
    ]


def benchmark_reviews(store: Store, settings: Settings, end: pd.Timestamp, days: int) -> list[dict[str, Any]]:
    out = []
    for b in settings.benchmarks:
        bdf = load_frame(store, b.id)
        if bdf.empty:
            continue
        w = bdf[bdf["ts"] > end - pd.Timedelta(days=days)]
        out.append(
            {
                "id": b.id,
                "name": b.name,
                "n": len(w),
                "mean": float(w["score"].mean()) if len(w) else None,
                "neg": share(int(w["neg"].sum()), len(w)),
                "replied": share(int(w["replied"].sum()), len(w)),
                "replied_neg": share(int((w["replied"] & w["neg"]).sum()), int(w["neg"].sum())),
            }
        )
    return out


def changes_section(store: Store, app_id: str) -> dict[str, Any]:
    """Reviews whose stars changed between refreshes, split by whether a reply came first."""
    rows = store.conn.execute(
        "SELECT old_score, new_score, replied_before, seen_at FROM review_changes WHERE app_id=?", (app_id,)
    ).fetchall()

    def tally(sel: list[Any]) -> dict[str, Any]:
        up = sum(1 for r in sel if r["new_score"] > r["old_score"])
        return {
            "n": len(sel),
            "up": up,
            "down": len(sel) - up,
            "mean_change": (sum(r["new_score"] - r["old_score"] for r in sel) / len(sel)) if sel else None,
        }

    return {
        "since": min((r["seen_at"] for r in rows), default=None),
        "all": tally(rows),
        "after_reply": tally([r for r in rows if r["replied_before"]]),
        "no_reply": tally([r for r in rows if not r["replied_before"]]),
    }


def latest_eval(settings: Settings) -> dict[str, Any] | None:
    folder = settings.root / "data" / "eval"
    files = sorted(folder.glob("eval_*_test.json")) if folder.exists() else []
    if not files:
        return None
    data: dict[str, Any] = json.loads(Path(files[-1]).read_text(encoding="utf-8"))
    data.setdefault("date", files[-1].stem.split("_")[1])  # eval_<YYYY-MM-DD>_test.json
    return data


def analyse(settings: Settings, tax: Taxonomy, store: Store) -> dict[str, Any]:
    df = load_frame(store, settings.app.id)
    if df.empty:
        raise RuntimeError("No reviews stored yet — run `ratings refresh` first.")
    end = df["ts"].max()
    a = settings.analysis
    listings = listing_section(store, settings)
    ev = latest_eval(settings)
    best = None
    if ev:
        # Quote the configuration actually in use: the vote row for "vote", the primary model otherwise.
        rows = ev["results"]
        if settings.llm.strategy == "vote" and len(settings.llm.models) > 1:
            best = next((r for r in rows if r["name"].startswith("vote")), rows[0])
        else:
            best = next((r for r in rows if r["name"] == settings.llm.model), rows[0])
    tag_window = df[df["ts"] >= pd.Timestamp(settings.tag_since, tz=IST)]
    return {
        "generated_at": datetime.now(UTC).astimezone().strftime("%d %b %Y, %H:%M"),
        "generated_iso": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "data_through": end.strftime("%d %b %Y"),
        "app": settings.app.model_dump(),
        "models": settings.llm.models,
        "taxonomy_version": tax.version,
        "counts": {
            "reviews": len(df),
            "tag_window": len(tag_window),
            "tagged": int(tag_window["tagged"].sum()),
            "first_review": df["ts"].min().strftime("%b %Y"),
            "agreement_mean": float(tag_window["agreement"].dropna().mean())
            if tag_window["agreement"].notna().any()
            else None,
        },
        "listing": listings,
        "gap": rating_gap(df, listings["main"]) if listings["main"] else None,
        "monthly": monthly_section(df),
        "replies": reply_section(df),
        "windows": {
            "recent": window_stats(df, end, a.recent_days),
            "prior": window_stats(df, end - pd.Timedelta(days=a.recent_days), a.recent_days),
            "year": window_stats(df, end, a.comparison_days),
            "year_before": window_stats(df, end - pd.Timedelta(days=a.comparison_days), a.comparison_days),
        },
        "versions": version_section(df, end, a.comparison_days, a.min_reviews_per_version),
        "issues": issue_section(df, tax, end, a.recent_days, a.comparison_days, (best or {}).get("per_code", {})),
        "emerging": emerging_section(df, tax, end),
        "orgs": org_section(df, pd.Timestamp(settings.tag_since, tz=IST)),
        "safety": safety_section(df, end, a.comparison_days),
        "visible": visible_section(df, end, a.comparison_days),
        "benchmark_reviews": benchmark_reviews(store, settings, end, a.comparison_days),
        "rating_changes": changes_section(store, settings.app.id),
        "eval": {"set": ev["set"], "date": ev["date"], "result": best, "results": ev["results"]}
        if ev and best
        else None,
        "end_iso": (end - timedelta(0)).isoformat(),
    }
