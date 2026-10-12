"""Render the analysis into a single self-contained HTML page (plus a dated copy)."""

from __future__ import annotations

import math
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from jinja2 import Environment, PackageLoader, select_autoescape

from . import charts
from .charts import Point, Row, pct
from .config import Settings
from .taxonomy import Taxonomy

IST_TZ = ZoneInfo("Asia/Kolkata")

ROOT_VARS = {
    "defect": "--series-1",
    "design": "--series-2",
    "policy": "--series-3",
    "service": "--series-4",
    "gap": "--series-5",
    "unspecified": "--muted-fill",
}


def _env() -> Environment:
    env = Environment(
        loader=PackageLoader("moveinsync_ratings", "templates"),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pct"] = lambda v, d=0: "—" if v is None else pct(v, d)
    env.filters["pp"] = lambda v: "—" if v is None else f"{v * 100:+.1f} pp"
    env.filters["num"] = lambda v: "—" if v is None else f"{v:,.0f}"
    env.filters["stars"] = lambda v: "—" if v is None else f"{v:.2f}★"
    env.filters["f2"] = lambda v: "—" if v is None else f"{v:.2f}"
    env.filters["f3"] = lambda v: "—" if v is None else f"{v:.3f}"
    return env


def load_backlog(settings: Settings) -> dict[str, Any] | None:
    path = settings.root / "backlog.yaml"
    if not path.exists():
        return None
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def build_charts(a: dict[str, Any], tax: Taxonomy) -> dict[str, str]:
    c: dict[str, str] = {}
    months = a["monthly"]
    c["neg_trend"] = charts.line_chart(
        [
            Point(
                m["month"][2:].replace("-", "/"),
                m["neg"]["p"],
                m["neg"]["lo"],
                m["neg"]["hi"],
                tip=f"{m['month']}: {m['neg']['p']:.0%} of {m['n']} reviews were 1–2★ (95% CI {m['neg']['lo']:.0%}–{m['neg']['hi']:.0%})"
                + (" · month in progress" if m["partial"] else ""),
                muted=m["partial"],
            )
            for m in months
        ],
        title="Share of written reviews that are 1–2 stars, by month",
        y_max=0.6,
    )
    c["mean_trend"] = charts.line_chart(
        [
            Point(
                m["month"][2:].replace("-", "/"),
                m["mean"],
                tip=f"{m['month']}: average {m['mean']:.2f}★ over {m['n']} reviews",
                muted=m["partial"],
            )
            for m in months
        ],
        fmt=lambda v: f"{v:.1f}★",
        y_min=3.0,
        y_max=4.5,
        ticks=3,
        series_var="--series-1",
        title="Average stars of written reviews, by month",
    )
    q = a["replies"]["quarters"]
    c["replies"] = charts.column_chart(
        [
            Point(
                x["quarter"].replace("Q", " Q")[2:],
                x["replied"]["p"],
                tip=f"{x['quarter']}: replied to {x['replied']['k']} of {x['replied']['n']} reviews ({x['replied']['p']:.0%})",
            )
            for x in q
        ],
        y_max=1.0,
        label_every=2,
        highlight=lambda p: p.value < 0.2,
        title="Share of reviews that got a developer reply, by quarter",
    )
    iss = a["issues"]["issues"]
    top = [i for i in iss if i["year"]["k"] > 0][:12]
    c["issues"] = charts.hbar_chart(
        [
            Row(
                i["label"],
                i["recent"]["p"],
                i["recent"]["lo"],
                i["recent"]["hi"],
                color_var=ROOT_VARS[i["root"]],
                tip=(
                    f"{i['label']}: in {i['recent']['k']} of {i['recent']['n']} negative reviews in the last "
                    f"{a['windows']['recent']['days']} days ({i['recent']['p']:.0%}, 95% CI {i['recent']['lo']:.0%}–{i['recent']['hi']:.0%}). "
                    f"Previous period {i['prior']['p']:.0%}. Owner: {i['owner']}."
                ),
            )
            for i in top
        ],
        title="Share of recent negative reviews naming each issue",
    )
    gap = a.get("gap")
    main = a["listing"]["main"]
    if gap and main:
        groups: list[tuple[str, list[int]]] = [
            ("Star-only ratings", gap["star_only"]),
            ("Written reviews", gap["written"]),
        ]
        for b in a["listing"]["benchmarks"][:1]:
            groups.append((f"{b['name']} (all)", b["hist"]))
        c["star_mix"] = charts.star_mix_chart(groups, title="Star mix: silent raters vs reviewers")
    vers = a["versions"]
    if vers:
        base = sum(v["neg"]["k"] for v in vers) / max(sum(v["neg"]["n"] for v in vers), 1)
        c["releases"] = charts.release_chart(
            [
                {
                    "version": v["version"],
                    "p": v["neg"]["p"],
                    "lo": v["neg"]["lo"],
                    "hi": v["neg"]["hi"],
                    "flag": v["flag"],
                    "tip": (
                        f"v{v['version']} (first review {v['first_seen']}): {v['neg']['k']} of {v['n']} reviews 1–2★ "
                        f"({v['neg']['p']:.0%}, 95% CI {v['neg']['lo']:.0%}–{v['neg']['hi']:.0%}); other releases "
                        f"{v['baseline_neg']:.0%}; p = {v['p']:.3f}"
                        + {
                            "worse": " — significantly worse",
                            "watch": f" — watch: not significant after correcting for {v['tests']} releases",
                            "better": " — significantly better",
                        }.get(v["flag"], "")
                    ),
                }
                for v in vers
            ],
            baseline=base,
            title="Share of 1–2★ reviews per release",
        )
    hist = (main or {}).get("history") or []
    if len(hist) >= 2:
        lo = min(h["score"] for h in hist)
        hi = max(h["score"] for h in hist)
        # One date label per day (refreshes run several times a day); the last point always named.
        days = [_ist(h["at_full"]).strftime("%d %b") for h in hist]
        labels = [d if i == len(days) - 1 or (i == 0 or d != days[i - 1]) else "" for i, d in enumerate(days)]
        c["rating_history"] = charts.line_chart(
            [
                Point(
                    label,
                    h["score"],
                    tip=f"{_ist(h['at_full']).strftime('%d %b %Y, %H:%M')} IST: {h['score']:.3f}★ from {h['ratings']:,} ratings",
                )
                for label, h in zip(labels, hist, strict=True)
            ],
            fmt=lambda v: f"{v:.2f}★",
            y_min=math.floor((lo - 0.05) * 20) / 20,
            y_max=math.ceil((hi + 0.05) * 20) / 20,
            ticks=2,
            width=400,
            height=140,
            label_every=1,
            title="Google Play rating at each refresh",
        )
    for i in iss:
        i["spark"] = charts.sparkline(
            [m["p"] for m in i["monthly"]],
            tips=[f"{m['month']}: {m['k']} of {m['n']} negative reviews ({m['p']:.0%})" for m in i["monthly"]],
        )
    return c


def tracker(a: dict[str, Any], backlog: dict[str, Any] | None) -> list[dict[str, Any]]:
    """For each backlog item: its issue's share of negative reviews at the baseline vs now."""
    if not backlog:
        return []
    by_code = {i["code"]: i for i in a["issues"]["issues"]}
    out = []
    for t in backlog.get("tickets", []):
        codes = [c for c in t.get("issues", []) if c in by_code]
        if not codes or t.get("tracked") is False:
            continue
        main = by_code[codes[0]]
        base = t.get("baseline", {}).get("share")
        status = "no change yet"
        if base is not None and main["recent"]["n"] >= 30:
            lo, hi = main["recent"]["lo"], main["recent"]["hi"]
            if hi < base:
                status = "improved"
            elif lo > base:
                status = "worse"
        out.append(
            {
                "id": t["id"],
                "title": t["title"],
                "issue": main["label"],
                "baseline": base,
                "now": main["recent"],
                "status": status,
            }
        )
    return out


def _ist(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(IST_TZ)


def _p_mean(x: dict[str, Any], y: dict[str, Any]) -> float:
    """Two-sided p-value that two means differ, from their 95% normal intervals."""
    se1 = (x["hi"] - x["mean"]) / 1.96 if x["n"] > 1 else 0.0
    se2 = (y["hi"] - y["mean"]) / 1.96 if y["n"] > 1 else 0.0
    se = math.hypot(se1, se2)
    if se == 0:
        return 1.0
    z = (x["mean"] - y["mean"]) / se
    return 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))


def stat_tiles(a: dict[str, Any]) -> list[dict[str, Any]]:
    """The three headline tiles under the hero: value, a delta vs the previous period (coloured
    good/bad only when statistically significant) and a 12-month sparkline."""
    from .analysis import two_prop_p

    r, p = a["windows"]["recent"], a["windows"]["prior"]
    days = r["days"]
    months = [m for m in a["monthly"] if not m["partial"]][-12:]

    def delta(diff: float, pv: float, up_is_good: bool, unit: str) -> dict[str, str]:
        sig = pv < 0.05
        good = (diff > 0) == up_is_good
        arrow = "▲" if diff > 0 else "▼" if diff < 0 else "■"
        text = f"{arrow} {diff:+.2f}{unit}" if unit == "★" else f"{arrow} {diff * 100:+.1f} pp"
        return {
            "text": text,
            "note": f"vs previous {days} days" + ("" if sig else " · not significant"),
            "title": f"Two-sided p = {pv:.3f}" + (" (significant at 0.05)" if sig else ""),
            "cls": ("delta-good" if good else "delta-bad") if sig else "muted",
        }

    def spark(vals: list[float], fmt: Any, label: str) -> str:
        return charts.sparkline(
            vals,
            width=132,
            height=34,
            cls="spark tile-spark",
            tips=[f"{m['month']}: {fmt(v)} {label}" for m, v in zip(months, vals, strict=True)],
        )

    bench = a["benchmark_reviews"][0] if a["benchmark_reviews"] else None
    return [
        {
            "label": f"Written-review average, last {days} days",
            "value": f"{r['mean']['mean']:.2f}★",
            "delta": delta(r["mean"]["mean"] - p["mean"]["mean"], _p_mean(r["mean"], p["mean"]), True, "★"),
            "ctx": f"{r['n']:,} reviews · ±{r['mean']['hi'] - r['mean']['mean']:.2f} (95% CI)",
            "spark": spark([m["mean"] for m in months], lambda v: f"{v:.2f}★", "average"),
        },
        {
            "label": f"Written reviews that are 1–2★, last {days} days",
            "value": pct(r["neg"]["p"]),
            "delta": delta(
                r["neg"]["p"] - p["neg"]["p"],
                two_prop_p(r["neg"]["k"], r["neg"]["n"], p["neg"]["k"], p["neg"]["n"]),
                False,
                "pp",
            ),
            "ctx": f"{r['neg']['k']} of {r['neg']['n']} · 95% CI {pct(r['neg']['lo'])}–{pct(r['neg']['hi'])}",
            "spark": spark([m["neg"]["p"] for m in months], pct, "were 1–2★"),
        },
        {
            "label": f"Reviews answered, last {days} days",
            "value": pct(r["replied"]["p"]),
            "value_cls": "delta-bad" if r["replied"]["p"] < 0.5 else "",
            "delta": delta(
                r["replied"]["p"] - p["replied"]["p"],
                two_prop_p(r["replied"]["k"], r["replied"]["n"], p["replied"]["k"], p["replied"]["n"]),
                True,
                "pp",
            ),
            "ctx": f"{r['replied']['k']} of {r['replied']['n']}"
            + (f" · {bench['name']}: {pct(bench['replied']['p'])}" if bench else ""),
            "spark": spark([m["replied"]["p"] for m in months], pct, "answered"),
        },
    ]


def summary_points(a: dict[str, Any]) -> list[dict[str, str]]:
    """Plain-language 'what changed' lines, generated from the numbers with significance checks.
    `kind` drives the status icon: bad (needs attention), good, or info."""
    from .analysis import two_prop_p

    pts: list[dict[str, str]] = []
    r, p = a["windows"]["recent"], a["windows"]["prior"]
    days = r["days"]
    pv = two_prop_p(r["neg"]["k"], r["neg"]["n"], p["neg"]["k"], p["neg"]["n"])
    direction = "up" if r["neg"]["p"] > p["neg"]["p"] else "down"
    if pv < 0.05:
        pts.append(
            {
                "kind": "bad" if direction == "up" else "good",
                "text": f"Negative reviews are {direction}: {r['neg']['p']:.0%} of written reviews in the last {days} days were 1–2★, "
                f"vs {p['neg']['p']:.0%} in the {days} days before (statistically significant, p = {pv:.3f}).",
            }
        )
    else:
        pts.append(
            {
                "kind": "info",
                "text": f"No significant change in sentiment: {r['neg']['p']:.0%} of written reviews in the last {days} days were 1–2★, "
                f"vs {p['neg']['p']:.0%} before (p = {pv:.2f}).",
            }
        )
    for e in a["emerging"][:3]:
        pts.append(
            {
                "kind": "bad",
                "text": f"Emerging: “{e['label']}” is in {e['last30']['p']:.0%} of negative reviews in the last 30 days, "
                f"up from {e['base']['p']:.0%} (p = {e['p']:.3f}).",
            }
        )
    for v in a["versions"][-6:]:
        if v["flag"] == "worse":
            pts.append(
                {
                    "kind": "bad",
                    "text": f"Release {v['version']} is significantly worse than other releases ({v['neg']['p']:.0%} negative vs {v['baseline_neg']:.0%}).",
                }
            )
        elif v["flag"] == "watch":
            pts.append(
                {
                    "kind": "bad",
                    "text": f"Watch release {v['version']}: {v['neg']['p']:.0%} negative vs {v['baseline_neg']:.0%} for other releases — "
                    f"not yet conclusive ({v['n']} reviews).",
                }
            )
    rr = a["windows"]["recent"]["replied"]
    pts.append(
        {
            "kind": "bad" if rr["p"] < 0.5 else "good",
            "text": f"Developer replies: {rr['k']} of {rr['n']} reviews in the last {days} days got a reply ({rr['p']:.0%}). Last reply: {a['replies']['last_reply']}.",
        }
    )
    if a["safety"]["last30"]:
        pts.append(
            {
                "kind": "bad",
                "text": f"{a['safety']['last30']} review(s) in the last 30 days raised a personal-safety concern — "
                "each is listed in the Safety section for follow-up.",
            }
        )
    return pts


def next_actions(a: dict[str, Any], tax: Taxonomy, tracked: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Evidence-triggered to-do list for this refresh. Each action cites the number that
    triggered it and the owner from the taxonomy — no free-form model text, so it never drifts.
    `key` names the condition (stable across refreshes); `ratings alerts` uses it to send each once."""
    acts: list[dict[str, str]] = []
    days = a["windows"]["recent"]["days"]
    s30 = a["safety"]["last30"]
    if s30:
        acts.append(
            {
                "key": "safety",
                "priority": "Today",
                "owner": tax.owners["safety_team"],
                "action": f"Follow up the {s30} safety-related review{'s' if s30 > 1 else ''} from the last 30 days.",
                "evidence": "Listed in full in the Safety section below.",
            }
        )
    for v in a["versions"][-3:]:
        if v["flag"] in ("worse", "watch"):
            top = ", ".join(tax.issues[c].label for c, _ in v["top_issues"]) or "no single dominant issue"
            acts.append(
                {
                    "key": f"release:{v['version']}:{v['flag']}",
                    "priority": "This week" if v["flag"] == "worse" else "Watch",
                    "owner": tax.owners["eng_mobile"],
                    "action": f"Check release {v['version']} for regressions — most-named problems: {top}.",
                    "evidence": f"{v['neg']['p']:.0%} of its {v['n']} reviews are 1–2★ vs {v['baseline_neg']:.0%} for other releases "
                    + ("(significant)." if v["flag"] == "worse" else f"(p = {v['p']:.3f}; not yet conclusive)."),
                }
            )
    for e in a["emerging"][:3]:
        meta = tax.issues[e["code"]]
        acts.append(
            {
                "key": f"emerging:{e['code']}",
                "priority": "This week",
                "owner": tax.owners[meta.owner],
                "action": f"Investigate the rise in “{meta.label}”.",
                "evidence": f"{e['last30']['p']:.0%} of negative reviews in the last 30 days vs {e['base']['p']:.0%} before (p = {e['p']:.3f}).",
            }
        )
    unanswered = [v for v in a["visible"] if not v["replied"] and v["upvotes"] >= 2]
    rr = a["windows"]["recent"]["replied"]
    if unanswered:
        acts.append(
            {
                "key": "replies",
                "priority": "This week",
                "owner": "Support / app team",
                "action": f"Reply to the {len(unanswered)} most-read unanswered 1–2★ reviews (listed below) with a concrete fix or contact.",
                "evidence": f"Each was marked helpful by 2+ people; only {rr['p']:.0%} of reviews in the last {days} days got a reply.",
            }
        )
    for i in [x for x in a["issues"]["issues"] if x["trend"] == "rising"][:3]:
        acts.append(
            {
                "key": f"rising:{i['code']}",
                "priority": "This week",
                "owner": i["owner"],
                "action": f"“{i['label']}” is rising — confirm root cause and owner.",
                "evidence": f"{i['recent']['p']:.0%} of negative reviews in the last {days} days vs {i['prior']['p']:.0%} before (p = {i['trend_p']:.3f}).",
            }
        )
    for t in tracked:
        if t["status"] == "improved":
            acts.append(
                {
                    "key": f"fix:{t['id']}:improved",
                    "priority": "FYI",
                    "owner": "Product",
                    "action": f"Fix {t['id']} is working — “{t['issue']}” fell below its baseline.",
                    "evidence": f"Now {t['now']['p']:.0%} (95% CI {t['now']['lo']:.0%}–{t['now']['hi']:.0%}) vs baseline {t['baseline']:.0%}.",
                }
            )
        elif t["status"] == "worse":
            acts.append(
                {
                    "key": f"fix:{t['id']}:worse",
                    "priority": "This week",
                    "owner": "Product",
                    "action": f"Fix {t['id']}: “{t['issue']}” is above its baseline — check the shipped change.",
                    "evidence": f"Now {t['now']['p']:.0%} vs baseline {t['baseline']:.0%}.",
                }
            )
    return acts


def render_report(settings: Settings, tax: Taxonomy, a: dict[str, Any]) -> Path:
    env = _env()
    backlog = load_backlog(settings)
    tracked = tracker(a, backlog)
    ctx = {
        "a": a,
        "c": build_charts(a, tax),
        "tax": tax,
        "root_vars": ROOT_VARS,
        "summary": summary_points(a),
        "tiles": stat_tiles(a),
        "tracker": tracked,
        "actions": next_actions(a, tax, tracked),
        "today": date.today().isoformat(),
    }
    html = env.get_template("monitor.html.j2").render(**ctx)
    out_dir = settings.reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "latest.html"
    latest.write_text(html, encoding="utf-8")
    archive = out_dir / "history"
    archive.mkdir(exist_ok=True)
    shutil.copyfile(latest, archive / f"{date.today().isoformat()}.html")
    return latest
