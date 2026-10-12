"""Download store listings and reviews from Google Play (public data, no login)."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from google_play_scraper import Sort, app, reviews

from .config import AppRef
from .store import Store, UpsertResult

PAGE = 200
# Edited reviews jump back to the top of the "newest" list, so on an incremental run we keep
# paging until we are this far past the newest review we already had.
OVERLAP = timedelta(days=21)

Progress = Callable[[int, str], None]


def _retry(fn: Callable[[], Any], attempts: int = 5) -> Any:
    for i in range(attempts):
        try:
            return fn()
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(2 * (i + 1) ** 2)
    raise AssertionError("unreachable")


def _page(ref: AppRef, token: Any) -> tuple[list[dict[str, Any]], Any]:
    result: tuple[list[dict[str, Any]], Any] = reviews(
        ref.id, lang=ref.lang, country=ref.country, sort=Sort.NEWEST, count=PAGE, continuation_token=token
    )
    return result


def fetch_listing(store: Store, ref: AppRef) -> dict[str, Any]:
    d: dict[str, Any] = _retry(lambda: app(ref.id, lang=ref.lang, country=ref.country))
    store.add_listing(ref.id, d)
    return d


def fetch_reviews(
    store: Store,
    ref: AppRef,
    *,
    since: datetime | None = None,
    full: bool = False,
    progress: Progress | None = None,
) -> UpsertResult:
    """Page through reviews newest-first and upsert them.

    - First run (or `full=True`): everything Google Play returns.
    - Later runs: stop once a page ends more than OVERLAP before the newest stored review.
    - `since`: never go further back than this (used for benchmark apps).
    """
    latest = None if full else store.latest_review_at(ref.id)
    stop_before = None
    if latest:
        stop_before = datetime.fromisoformat(latest).replace(tzinfo=UTC) - OVERLAP
    if since and (stop_before is None or since > stop_before):
        stop_before = since

    total = UpsertResult()
    token = None
    fetched = 0
    while True:
        batch, token = _retry(partial(_page, ref, token))
        if not batch:
            break
        res = store.upsert_reviews(ref.id, batch)
        total.new += res.new
        total.updated += res.updated
        total.unchanged += res.unchanged
        fetched += len(batch)
        oldest = min(r["at"] for r in batch).astimezone(UTC)
        if progress:
            progress(fetched, oldest.strftime("%d %b %Y"))
        if stop_before and oldest < stop_before:
            break
        if token is None or token.token is None:
            break
    return total
