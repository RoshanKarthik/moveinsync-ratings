"""Score tagging models against reference labels.

Reference file (JSONL), one review per line:
    {"review_id", "score", "text", "issues": [...], "also_ok": [...], "target", "safety", "set": "dev"|"test"}

`issues` are the labels a careful reader would give (primary first); `also_ok` are defensible
alternatives that should not count as errors. The "dev" set was used while writing the prompt;
"test" was labelled afterwards and never used for tuning, so its scores are the honest ones.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from rich.console import Console
from rich.table import Table

from .config import Settings
from .store import Store, content_hash
from .tagger import ReviewIn, Tagger, Vote, combine, rule_tag, tag_reviews
from .taxonomy import NON_SPECIFIC, Taxonomy


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion k/n."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass
class Score:
    name: str
    n: int = 0
    primary_hits: int = 0
    pred_codes: int = 0
    pred_ok: int = 0
    gold_codes: int = 0
    gold_found: int = 0
    target_hits: int = 0
    safety_tp: int = 0
    safety_fp: int = 0
    safety_fn: int = 0
    per_code: dict[str, Counter[str]] = field(default_factory=dict)
    misses: list[dict[str, Any]] = field(default_factory=list)

    def add(self, gold: dict[str, Any], issues: list[str], target: str, safety: bool) -> None:
        ok = set(gold["issues"]) | set(gold.get("also_ok", []))
        self.n += 1
        hit = bool(issues) and issues[0] in ok
        self.primary_hits += hit
        self.pred_codes += len(issues)
        self.pred_ok += sum(c in ok for c in issues)
        self.gold_codes += len(gold["issues"])
        self.gold_found += sum(c in issues for c in gold["issues"])
        self.target_hits += target == gold["target"] or "both" in (target, gold["target"])
        if safety and gold["safety"]:
            self.safety_tp += 1
        elif safety:
            self.safety_fp += 1
        elif gold["safety"]:
            self.safety_fn += 1
        for c in set(issues) | set(gold["issues"]):
            if c in NON_SPECIFIC:
                continue
            cnt = self.per_code.setdefault(c, Counter())
            if c in issues and c in ok:
                cnt["tp"] += 1
            elif c in issues:
                cnt["fp"] += 1
            if c in gold["issues"] and c not in issues:
                cnt["fn"] += 1
        if not hit:
            self.misses.append({"text": gold["text"][:200], "gold": gold["issues"], "got": issues})

    def summary(self) -> dict[str, Any]:
        lo, hi = wilson(self.primary_hits, self.n)
        return {
            "name": self.name,
            "n": self.n,
            "primary_accuracy": self.primary_hits / max(self.n, 1),
            "primary_ci95": [lo, hi],
            "label_precision": self.pred_ok / max(self.pred_codes, 1),
            "label_recall": self.gold_found / max(self.gold_codes, 1),
            "target_accuracy": self.target_hits / max(self.n, 1),
            "safety": {"tp": self.safety_tp, "fp": self.safety_fp, "fn": self.safety_fn},
            "per_code": {
                c: {
                    "precision": v["tp"] / max(v["tp"] + v["fp"], 1),
                    "recall": v["tp"] / max(v["tp"] + v["fn"], 1),
                    "support": v["tp"] + v["fn"],
                    "predicted": v["tp"] + v["fp"],
                }
                for c, v in sorted(self.per_code.items())
            },
        }


def load_reference(settings: Settings, which: str | None = None) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in settings.gold_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if which is None or r.get("set", "dev") == which]


class _MemoryCache:
    """Eval answers go to the real answer cache too: same prompt + same text = same answer."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def get_answers(self, model: str, taxonomy_version: int) -> dict[str, tuple[str, dict[str, Any]]]:
        return self.store.get_answers(model, taxonomy_version)

    def save_answers(self, model: str, taxonomy_version: int, rows: list[tuple[str, str, dict[str, Any]]]) -> None:
        self.store.save_answers(model, taxonomy_version, rows)


def run_eval(
    settings: Settings,
    tax: Taxonomy,
    store: Store,
    models: list[str],
    console: Console,
    which: str | None = None,
) -> list[dict[str, Any]]:
    gold = load_reference(settings, which)
    if not gold:
        console.print(f"[red]No reference labels found in {settings.gold_path}[/]")
        return []
    items = [ReviewIn(g["review_id"], g["score"], g["text"], content_hash(g["text"])) for g in gold]
    taggers = [Tagger(settings.llm, tax, model=m) for m in models]
    console.print(f"Scoring {len(models)} model(s) on {len(gold)} reference reviews ({which or 'all sets'}).")

    per_model: dict[str, dict[str, Any]] = {}
    for t in taggers:
        with console.status(f"  {t.model} …"):
            rows = tag_reviews(items, [t], taxonomy_version=tax.version, cache=_MemoryCache(store))
        per_model[t.model] = {r["review_id"]: r for r in rows}

    scores: list[Score] = []
    for t in taggers:
        s = Score(t.model)
        for g in gold:
            row = per_model[t.model].get(g["review_id"])
            if row:
                s.add(g, row["issues"], row["target"], row["safety"])
        scores.append(s)

    if len(taggers) >= 2:
        s = Score("vote: " + " + ".join(m.split("/")[-1] for m in models))
        order = [t.model for t in taggers]
        for g in gold:
            ruled = rule_tag(g["score"], g["text"])
            if ruled is not None:
                s.add(g, ruled, "app", False)
                continue
            results = {}
            for m in order:
                row = per_model[m].get(g["review_id"])
                if row and row["votes"]:
                    results[m] = _as_result(row)
            if results:
                v: Vote = combine(results, order)
                s.add(g, v.issues, v.target, v.safety)
        scores.append(s)

    table = Table(title="Tagging quality vs reference labels")
    for col in (
        "Model",
        "n",
        "Primary issue correct (95% CI)",
        "Label precision",
        "Label recall",
        "Target",
        "Safety missed",
    ):
        table.add_column(col)
    out = []
    for s in scores:
        d = s.summary()
        out.append(d)
        lo, hi = d["primary_ci95"]
        table.add_row(
            d["name"],
            str(d["n"]),
            f"{d['primary_accuracy']:.0%} ({lo:.0%}–{hi:.0%})",
            f"{d['label_precision']:.0%}",
            f"{d['label_recall']:.0%}",
            f"{d['target_accuracy']:.0%}",
            f"{d['safety']['fn']} of {d['safety']['tp'] + d['safety']['fn']}",
        )
    console.print(table)

    folder = settings.root / "data" / "eval"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"eval_{date.today().isoformat()}_{which or 'all'}.json"
    path.write_text(
        json.dumps(
            {
                "set": which or "all",
                "taxonomy_version": tax.version,
                "results": out,
                "misses": {s.name: s.misses for s in scores},
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    console.print(f"Details: {path}")
    return out


def _as_result(row: dict[str, Any]) -> Any:
    from .tagger import TagResult

    return TagResult(
        id=1,
        issues=row["issues"],
        target=row["target"],
        update=row["mentions_update"],
        safety=row["safety"],
        org=row["org"],
        detail=row["detail"],
    )
