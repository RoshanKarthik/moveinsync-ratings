"""Alerts for the scheduled refresh: what someone should look at today, sent once to Slack.

Triggers, each built from `analyse()` with the same wording as the monitor's to-do list:
- a new review raising a safety concern (last EMERGING_DAYS days)
- an issue newly emerging (see analysis.emerging_section)
- a release flagged 'worse' or 'watch'
- Google's published rating falling
- stale data: the last refresh failed, no new review for days, or reviews left untagged

Every alert has a key. Sent keys are stored in the review database, which the workflow keeps on
the `data` branch, so an alert goes out once, not every 2 hours. Conditions that can end (an
emerging issue, a rating drop, stale data) are re-armed when they clear, so a recurrence alerts again.
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import pandas as pd

from .analysis import EMERGING_DAYS
from .config import Settings
from .report import next_actions
from .store import Store
from .taxonomy import Taxonomy

Kind = Literal["safety", "emerging", "release", "rating", "stale"]
REARM: tuple[Kind, ...] = ("emerging", "rating", "stale")
MAINTAINER = "Monitor maintainer"


@dataclass
class Alert:
    key: str
    kind: Kind
    priority: str
    owner: str
    title: str
    detail: str

    @property
    def text(self) -> str:
        return f"[{self.priority}] {self.title} Owner: {self.owner}. {self.detail}"


def _short(text: str, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + " …"


# ── triggers ──────────────────────────────────────────────────────────────────────────────────
def safety_alerts(a: dict[str, Any], tax: Taxonomy, days: int = EMERGING_DAYS) -> list[Alert]:
    since = pd.Timestamp(a["end_iso"]) - pd.Timedelta(days=days)
    return [
        Alert(
            key=f"safety:{r['id']}",
            kind="safety",
            priority="Today",
            owner=tax.owners["safety_team"],
            title=f"Safety concern in a {r['stars']}★ review of {r['date']}"
            + ("." if r["replied"] else ", not yet answered."),
            detail=f"“{_short(r['text'])}”",
        )
        for r in a["safety"]["reviews"]
        if pd.Timestamp(r["at"]) > since
    ]


def action_alerts(a: dict[str, Any], tax: Taxonomy) -> list[Alert]:
    """Emerging issues and flagged releases, worded exactly as on the monitor's to-do list."""
    out: list[Alert] = []
    for act in next_actions(a, tax, []):
        kind = act["key"].split(":", 1)[0]
        if kind in ("emerging", "release"):
            out.append(Alert(act["key"], kind, act["priority"], act["owner"], act["action"], act["evidence"]))  # type: ignore[arg-type]
    return out


def rating_alert(a: dict[str, Any], settings: Settings) -> Alert | None:
    cfg = settings.alerts
    hist = [h for h in (a["listing"]["main"] or {}).get("history", []) if h["score"] is not None]
    if len(hist) < 2:
        return None
    cur = hist[-1]
    since = pd.Timestamp(cur["at_full"]) - pd.Timedelta(days=cfg.rating_days)
    earlier = [h for h in hist[:-1] if pd.Timestamp(h["at_full"]) >= since]
    if not earlier:
        return None
    top = max(earlier, key=lambda h: h["score"])
    if top["score"] - cur["score"] < cfg.rating_drop:
        return None
    return Alert(
        key="rating:drop",
        kind="rating",
        priority="This week",
        owner="Product",
        title=f"Play Store rating fell from {top['score']:.2f}★ to {cur['score']:.2f}★.",
        detail=f"Google's published rating: {top['score']:.2f}★ from {top['ratings']:,} ratings on {top['at']}, "
        f"{cur['score']:.2f}★ from {cur['ratings']:,} on {cur['at']}. Check the latest release and the "
        "monitor's 'What changed' list.",
    )


def stale_alerts(
    a: dict[str, Any], tax: Taxonomy, store: Store, settings: Settings, now: datetime | None = None
) -> list[Alert]:
    now = now or datetime.now(UTC)
    out: list[Alert] = []
    last = store.conn.execute(
        "SELECT finished_at, status, note FROM runs WHERE status != 'running' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if last and last["status"] == "failed":
        out.append(
            Alert(
                "stale:refresh",
                "stale",
                "Today",
                MAINTAINER,
                f"The last refresh failed ({last['finished_at']}); the site shows older data.",
                f"Error: {_short(last['note'] or 'no message', 300)}",
            )
        )
    age = (now - pd.Timestamp(a["end_iso"]).to_pydatetime()).total_seconds() / 86400
    if age > settings.alerts.stale_days:
        r = a["windows"]["recent"]
        out.append(
            Alert(
                "stale:reviews",
                "stale",
                "Today",
                MAINTAINER,
                f"No new review for {age:.0f} days (newest {a['data_through']}).",
                f"The last {r['days']} days averaged {r['n'] / r['days']:.1f} reviews a day, so the download "
                "has probably stopped working. Run `ratings doctor`.",
            )
        )
    untagged = len(store.untagged(settings.app.id, settings.tag_since, tax.version))
    if untagged:
        out.append(
            Alert(
                "stale:untagged",
                "stale",
                "Today",
                MAINTAINER,
                f"{untagged:,} review{'s are' if untagged > 1 else ' is'} not tagged, so issue shares leave "
                f"{'them' if untagged > 1 else 'it'} out.",
                f"Tagging failed or was skipped. Check the {settings.llm.api_key_env} secret and run `ratings doctor`.",
            )
        )
    return out


def collect(
    a: dict[str, Any], tax: Taxonomy, store: Store, settings: Settings, now: datetime | None = None
) -> list[Alert]:
    rating = rating_alert(a, settings)
    return [
        *stale_alerts(a, tax, store, settings, now),
        *safety_alerts(a, tax),
        *action_alerts(a, tax),
        *([rating] if rating else []),
    ]


# ── sending, once ─────────────────────────────────────────────────────────────────────────────
def _esc(s: str) -> str:
    """Slack mrkdwn treats &, < and > as control characters."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_payload(alerts: list[Alert], a: dict[str, Any], site_url: str | None) -> dict[str, str]:
    n = len(alerts)
    lines = [
        f"*{_esc(a['app']['name'])}: {n} new alert{'s' if n > 1 else ''}* (Play Store reviews to {a['data_through']})"
    ]
    for x in alerts:
        lines.append(f"• *{_esc(x.title)}* {_esc(x.priority)} · owner: {_esc(x.owner)}\n    {_esc(x.detail)}")
    if site_url:
        lines.append(f"<{site_url}|Open the ratings monitor>")
    return {"text": "\n".join(lines)}


def post_slack(url: str, payload: dict[str, str]) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as resp:  # the URL is the user's own secret
        body = resp.read().decode("utf-8", "replace")
        if resp.status != 200 or body.strip() != "ok":
            raise RuntimeError(f"Slack answered {resp.status}: {body[:200]}")


def dispatch(alerts: list[Alert], store: Store, send: Callable[[list[Alert]], None] | None) -> list[Alert]:
    """Re-arm cleared conditions, then send the alerts not sent before and record them.
    With no sender (no webhook configured) nothing is recorded, so they go out once one is set.
    Returns the alerts that were new this run."""
    store.rearm_alerts(REARM, {x.key for x in alerts})
    sent = store.sent_alert_keys()
    new = [x for x in alerts if x.key not in sent]
    if new and send is not None:
        send(new)  # raises on failure, so nothing is recorded and the next run retries
        store.record_alerts((x.key, x.kind, x.text) for x in new)
    return new
