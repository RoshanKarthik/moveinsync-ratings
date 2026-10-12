"""SQLite storage: one file, no server, safe to commit or copy.

Reviewer names and avatars are never stored — the analysis only needs the text and metadata.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    review_id      TEXT PRIMARY KEY,
    app_id         TEXT NOT NULL,
    score          INTEGER NOT NULL,
    content        TEXT NOT NULL DEFAULT '',
    content_hash   TEXT NOT NULL,
    thumbs_up      INTEGER NOT NULL DEFAULT 0,
    app_version    TEXT,
    at             TEXT NOT NULL,
    reply_content  TEXT,
    replied_at     TEXT,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_app_at ON reviews (app_id, at);

-- Star changes on existing reviews. Play shows only the latest version of a review, so this log
-- is the only way to see whether a reply was followed by a better (or worse) rating.
CREATE TABLE IF NOT EXISTS review_changes (
    review_id      TEXT NOT NULL,
    app_id         TEXT NOT NULL,
    old_score      INTEGER NOT NULL,
    new_score      INTEGER NOT NULL,
    old_at         TEXT NOT NULL,
    new_at         TEXT NOT NULL,
    replied_before INTEGER NOT NULL,          -- 1 if a developer reply existed before the change
    seen_at        TEXT NOT NULL,
    PRIMARY KEY (review_id, new_at)
);

CREATE TABLE IF NOT EXISTS listings (
    app_id       TEXT NOT NULL,
    captured_at  TEXT NOT NULL,
    title        TEXT,
    score        REAL,
    ratings      INTEGER,
    reviews      INTEGER,
    installs     INTEGER,
    h1 INTEGER, h2 INTEGER, h3 INTEGER, h4 INTEGER, h5 INTEGER,
    version      TEXT,
    updated      TEXT,
    PRIMARY KEY (app_id, captured_at)
);

CREATE TABLE IF NOT EXISTS tags (
    review_id         TEXT PRIMARY KEY REFERENCES reviews (review_id),
    content_hash      TEXT NOT NULL,
    taxonomy_version  INTEGER NOT NULL,
    method            TEXT NOT NULL,          -- 'llm' or 'rule'
    model             TEXT,
    issues            TEXT NOT NULL,          -- JSON list, primary issue first
    target            TEXT NOT NULL,          -- app | service | both | none
    mentions_update   INTEGER NOT NULL,
    safety            INTEGER NOT NULL,
    org               TEXT,
    detail            TEXT,
    tagged_at         TEXT NOT NULL,
    agreement         REAL,                   -- share of models backing the primary issue
    votes             TEXT                    -- JSON {model: [issues]}
);

-- Raw answer of each model for each review, so runs resume and votes can be recombined.
CREATE TABLE IF NOT EXISTS answers (
    review_id         TEXT NOT NULL,
    model             TEXT NOT NULL,
    taxonomy_version  INTEGER NOT NULL,
    content_hash      TEXT NOT NULL,
    result            TEXT NOT NULL,
    answered_at       TEXT NOT NULL,
    PRIMARY KEY (review_id, model, taxonomy_version)
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    new_reviews  INTEGER DEFAULT 0,
    updated_reviews INTEGER DEFAULT 0,
    tagged       INTEGER DEFAULT 0,
    model        TEXT,
    note         TEXT
);
"""


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def content_hash(text: str | None) -> str:
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]


@dataclass
class UpsertResult:
    new: int = 0
    updated: int = 0
    unchanged: int = 0


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    # ── reviews ───────────────────────────────────────────────────────────────────────────
    def upsert_reviews(self, app_id: str, rows: Iterable[dict[str, Any]]) -> UpsertResult:
        """Insert new reviews; update edited ones (new text, new stars, or a new reply)."""
        result = UpsertResult()
        seen = now_iso()
        with self.tx() as c:
            for r in rows:
                text = (r.get("content") or "").strip()
                h = content_hash(text)
                at = _iso(r["at"])
                replied = _iso(r.get("repliedAt")) if r.get("repliedAt") else None
                old = c.execute(
                    "SELECT content_hash, score, at, reply_content, replied_at FROM reviews WHERE review_id=?",
                    (r["reviewId"],),
                ).fetchone()
                values = (
                    app_id,
                    int(r["score"]),
                    text,
                    h,
                    int(r.get("thumbsUpCount") or 0),
                    r.get("appVersion") or r.get("reviewCreatedVersion"),
                    at,
                    r.get("replyContent"),
                    replied,
                )
                if old is None:
                    c.execute(
                        "INSERT INTO reviews (app_id, score, content, content_hash, thumbs_up, app_version,"
                        " at, reply_content, replied_at, review_id, first_seen, last_seen)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (*values, r["reviewId"], seen, seen),
                    )
                    result.new += 1
                    continue
                changed = (
                    old["content_hash"] != h
                    or old["score"] != int(r["score"])
                    or old["at"] != at
                    or old["reply_content"] != r.get("replyContent")
                )
                c.execute(
                    "UPDATE reviews SET app_id=?, score=?, content=?, content_hash=?, thumbs_up=?,"
                    " app_version=?, at=?, reply_content=?, replied_at=?, last_seen=? WHERE review_id=?",
                    (*values, seen, r["reviewId"]),
                )
                if old["score"] != int(r["score"]):
                    reply_at = old["replied_at"] or replied
                    c.execute(
                        "INSERT OR IGNORE INTO review_changes VALUES (?,?,?,?,?,?,?,?)",
                        (
                            r["reviewId"],
                            app_id,
                            old["score"],
                            int(r["score"]),
                            old["at"],
                            at,
                            int(reply_at is not None and reply_at <= at),
                            seen,
                        ),
                    )
                if changed:
                    result.updated += 1
                else:
                    result.unchanged += 1
        return result

    def latest_review_at(self, app_id: str) -> str | None:
        row = self.conn.execute("SELECT MAX(at) FROM reviews WHERE app_id=?", (app_id,)).fetchone()
        return row[0] if row else None

    def review_count(self, app_id: str) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM reviews WHERE app_id=?", (app_id,)).fetchone()[0])

    # ── listings ──────────────────────────────────────────────────────────────────────────
    def add_listing(self, app_id: str, d: dict[str, Any]) -> None:
        hist = list(d.get("histogram") or [None] * 5) + [None] * 5
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO listings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    app_id,
                    now_iso(),
                    d.get("title"),
                    d.get("score"),
                    d.get("ratings"),
                    d.get("reviews"),
                    d.get("realInstalls"),
                    *hist[:5],
                    d.get("version"),
                    d.get("lastUpdatedOn"),
                ),
            )

    # ── tags ──────────────────────────────────────────────────────────────────────────────
    def untagged(self, app_id: str, since: str, taxonomy_version: int) -> list[sqlite3.Row]:
        """Reviews with no tag, a tag from an older taxonomy, or text edited since tagging."""
        return self.conn.execute(
            """
            SELECT r.review_id, r.score, r.content, r.content_hash
            FROM reviews r LEFT JOIN tags t ON t.review_id = r.review_id
            WHERE r.app_id = ? AND r.at >= ?
              AND (t.review_id IS NULL OR t.taxonomy_version < ? OR t.content_hash != r.content_hash)
            ORDER BY r.at DESC
            """,
            (app_id, since, taxonomy_version),
        ).fetchall()

    def save_tags(self, rows: Iterable[dict[str, Any]]) -> int:
        n = 0
        with self.tx() as c:
            for t in rows:
                c.execute(
                    "INSERT OR REPLACE INTO tags VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        t["review_id"],
                        t["content_hash"],
                        t["taxonomy_version"],
                        t["method"],
                        t.get("model"),
                        json.dumps(t["issues"]),
                        t["target"],
                        int(bool(t["mentions_update"])),
                        int(bool(t["safety"])),
                        t.get("org"),
                        t.get("detail"),
                        now_iso(),
                        t.get("agreement"),
                        json.dumps(t.get("votes") or {}),
                    ),
                )
                n += 1
        return n

    def get_answers(self, model: str, taxonomy_version: int) -> dict[str, tuple[str, dict[str, Any]]]:
        rows = self.conn.execute(
            "SELECT review_id, content_hash, result FROM answers WHERE model=? AND taxonomy_version=?",
            (model, taxonomy_version),
        ).fetchall()
        return {r["review_id"]: (r["content_hash"], json.loads(r["result"])) for r in rows}

    def save_answers(self, model: str, taxonomy_version: int, rows: list[tuple[str, str, dict[str, Any]]]) -> None:
        stamp = now_iso()
        with self.tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO answers VALUES (?,?,?,?,?,?)",
                [(rid, model, taxonomy_version, h, json.dumps(res, ensure_ascii=False), stamp) for rid, h, res in rows],
            )

    # ── runs ──────────────────────────────────────────────────────────────────────────────
    def start_run(self, model: str | None) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO runs (started_at, status, model) VALUES (?, 'running', ?)", (now_iso(), model))
            return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, status: str, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(
                f"UPDATE runs SET finished_at=?, status=?{', ' + sets if sets else ''} WHERE id=?",
                (now_iso(), status, *fields.values(), run_id),
            )


def _iso(value: Any) -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS'. The scraper returns naive datetimes in the machine's local time
    (datetime.fromtimestamp), so naive values are converted from local time — otherwise a run on a
    laptop in IST and one on a UTC CI runner would disagree by 5.5 h."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        utc = value.astimezone(UTC)  # naive → treated as local time
        return utc.replace(microsecond=0, tzinfo=None).isoformat(sep=" ")
    raise TypeError(f"not a timestamp: {value!r}")
