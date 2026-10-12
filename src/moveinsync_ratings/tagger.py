"""Tag reviews with issues from the taxonomy using any OpenAI-compatible LLM (NVIDIA NIM by default).

Design choices:
- Batches of ~20 reviews per request keep the free-tier request budget small.
- Every answer is validated; unknown codes are dropped, malformed batches are retried and then
  split in half until single reviews remain, so one bad review never blocks a run.
- Very short positive reviews ("good", "nice app") are tagged by a rule, not the model.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator

from .config import LLMSettings
from .taxonomy import Taxonomy

Target = Literal["app", "service", "both", "none"]

SYSTEM_PROMPT = """You label Google Play reviews of MoveInSync, the app employees of large companies
in India use for their office commute: they book a cab or shuttle for their shift, get a pickup time,
track the cab live, board with an OTP or QR check-in, and raise issues with the transport helpdesk.
The employer chooses MoveInSync; employees must use it. Cabs and drivers come from vendors managed by
the employer's transport desk and MoveInSync operations.

For each review return:
- issues: 1–3 issue codes from the list below, most important first. Use the most specific code that
  fits. Only use generic_negative / generic_positive when no specific issue is stated. Use a specific
  code even if the review is positive overall (e.g. "good app but OTP never comes" → otp_not_received).
  Judge by the text, not the stars: some people give 5 stars to a complaint or 1 star to praise.
  A bare "not working" / "not opening" means crash_or_wont_open. "Not user friendly" means ui_redesign.
  Read misspellings and Hinglish for meaning ("verst"/"wrost" = worst, "ghatiya"/"bekar" = bad).
  A feature that was removed or switched off is not a feature_request: tag what is now missing
  (e.g. schedule_not_visible for a vanished co-passenger list) plus ui_redesign.
- target: what the reviewer is unhappy (or happy) about —
  "app" (the software), "service" (cab, driver, vendor, helpdesk, transport team), "both", or "none".
- update: true if the review says a recent update / new version / new UI caused the problem.
- safety: true only if the review raises a personal-safety CONCERN or complaint (women's safety,
  night drop, SOS not working, rash driving, harassment, escort/guard). Praise such as "safe ride"
  is NOT a concern: safety=false, and use praise_specific, never safety_concern, for it.
- org: the reviewer's employer if they name it (e.g. "HCL", "Tech Mahindra"), else null.
- detail: the specific problem in at most 12 plain English words (translate Hinglish), e.g.
  "OTP not received on email or SMS". Empty string for generic reviews.

Issue codes:
{codes}

Answer with JSON only, no prose, in exactly this shape:
{{"results": [{{"id": 1, "issues": ["code"], "target": "app", "update": false, "safety": false, "org": null, "detail": "..."}}]}}
Return one result per input review, using the same id."""

# Short, clearly positive reviews that need no model: "good", "nice app", "very helpful app 👍".
_POSITIVE_WORDS = re.compile(
    r"^(very |so |really |too |super |it'?s |its |is |the |this |a |app |application |nice |good |great |"
    r"excellent |awesome |best |helpful |useful |amazing |superb |wonderful |fantastic |perfect |fine |"
    r"easy |simple |smooth |convenient |reliable |cool |ok |okay |wow |love |like |thanks |thank you |"
    r"experience |service |to use |user friendly |friendly |and |one |👍 |🙂 |😊 |❤️ |good one )+$",
    re.IGNORECASE,
)


def rule_tag(score: int, text: str) -> list[str] | None:
    """Return issues for reviews the model is not needed for, else None."""
    clean = re.sub(r"[^\w\s'👍🙂😊❤️]", " ", text.lower()).strip()
    clean = re.sub(r"\s+", " ", clean)
    if not clean:
        return ["generic_positive"] if score >= 4 else ["generic_negative"]
    if score >= 4 and len(clean.split()) <= 6 and _POSITIVE_WORDS.match(clean + " "):
        return ["generic_positive"]
    return None


def parse_model(name: str) -> tuple[str, dict[str, Any], int]:
    """Split "vendor/model@mode" into (model id, extra request body, max output tokens).

    Reasoning models on NVIDIA NIM think before answering; for short classification that mostly
    costs time and, with 20 reviews per request, can exhaust the token budget before the answer.
    `@nothink` switches thinking off; `@think` forces it on with a large budget. Different model
    families read different template flags, so both common spellings are sent (unknown ones are
    ignored by the chat template).
    """
    model_id, _, mode = name.partition("@")
    if mode == "nothink":
        return model_id, {"chat_template_kwargs": {"enable_thinking": False, "thinking": False}}, 4096
    if mode == "think":
        return model_id, {"chat_template_kwargs": {"enable_thinking": True, "thinking": True}}, 16384
    return model_id, {}, 16384


class TagResult(BaseModel):
    id: int
    issues: list[str] = Field(min_length=1)
    target: Target = "none"
    update: bool = False
    safety: bool = False
    org: str | None = None
    detail: str = ""

    @field_validator("org", mode="before")
    @classmethod
    def _blank_org(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip().lower() in {"", "null", "none", "n/a", "unknown"}:
            return None
        return v

    @field_validator("target", mode="before")
    @classmethod
    def _norm_target(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v


@dataclass
class ReviewIn:
    review_id: str
    score: int
    content: str
    content_hash: str


class RateLimiter:
    """Spaces requests evenly so a burst never trips the provider's per-minute limit."""

    def __init__(self, per_minute: int) -> None:
        self.interval = 60.0 / max(per_minute, 1)
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_at)
            self.next_at = start + self.interval
        time.sleep(max(0.0, start - now))


def _extract_json(text: str) -> dict[str, Any]:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in reply")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    return data


class Tagger:
    def __init__(self, settings: LLMSettings, taxonomy: Taxonomy, model: str | None = None) -> None:
        if not settings.api_key:
            raise RuntimeError(
                f"{settings.api_key_env} is not set. Add it to the .env file in the project folder "
                f"(get a free key at https://build.nvidia.com)."
            )
        self.settings = settings
        # A model may carry a mode suffix: "vendor/model@nothink" or "@think" (see parse_model).
        self.model = model or settings.model
        self.model_id, self.extra_body, self.max_tokens = parse_model(self.model)
        self.taxonomy = taxonomy
        self.client = OpenAI(
            base_url=settings.base_url, api_key=settings.api_key, timeout=settings.timeout_s, max_retries=0
        )
        self.system = SYSTEM_PROMPT.format(codes=taxonomy.prompt_block())
        # Cached answers are tied to model + exact prompt, so editing the prompt never reuses stale ones.
        self.cache_key = f"{self.model}#{hashlib.sha1(self.system.encode('utf-8')).hexdigest()[:8]}"
        self.limiter = RateLimiter(settings.requests_per_minute)
        self.valid = set(taxonomy.codes)
        self.requests = 0

    # ── one request ───────────────────────────────────────────────────────────────────────
    def _call(self, batch: Sequence[ReviewIn]) -> dict[int, TagResult]:
        payload = [{"id": i + 1, "stars": r.score, "text": r.content[:1500]} for i, r in enumerate(batch)]
        delay = 5.0
        last_error: Exception | None = None
        for _ in range(self.settings.max_retries):
            self.limiter.wait()
            try:
                self.requests += 1
                resp = self.client.chat.completions.create(
                    model=self.model_id,
                    messages=[
                        {"role": "system", "content": self.system},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    temperature=0,
                    max_tokens=self.max_tokens,
                    extra_body=self.extra_body or None,
                )
                text = resp.choices[0].message.content or ""
                data = _extract_json(text)
                out: dict[int, TagResult] = {}
                for item in data.get("results", []):
                    try:
                        res = TagResult.model_validate(item)
                    except ValidationError:
                        continue
                    res.issues = [c for c in dict.fromkeys(res.issues) if c in self.valid][:3]
                    if res.issues and 1 <= res.id <= len(batch):
                        out[res.id] = res
                return out
            except (ValueError, json.JSONDecodeError) as e:  # malformed answer — retry once more
                last_error = e
            except Exception as e:  # network, 429, 5xx
                last_error = e
                time.sleep(delay)
                delay = min(delay * 2, 90)
        raise RuntimeError(f"model request failed after retries: {last_error}")

    def _tag_batch(self, batch: Sequence[ReviewIn]) -> dict[str, TagResult]:
        """Tag a batch; anything the model skipped or garbled is retried in smaller pieces."""
        try:
            got = self._call(batch)
        except RuntimeError:
            if len(batch) == 1:
                raise
            got = {}
        done = {batch[i - 1].review_id: res for i, res in got.items()}
        missing = [r for r in batch if r.review_id not in done]
        if missing and len(batch) > 1:
            if len(missing) == len(batch):
                mid = len(batch) // 2
                for part in (batch[:mid], batch[mid:]):
                    done.update(self._tag_batch(part))
            else:
                done.update(self._tag_batch(missing))
        return done

    def tag_batches(
        self,
        batches: Sequence[Sequence[ReviewIn]],
        *,
        workers: int = 4,
    ) -> Iterator[tuple[Sequence[ReviewIn], dict[str, TagResult]]]:
        """Yield (batch, results) as batches finish. Failed reviews are simply absent."""
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._tag_batch, b): b for b in batches}
            for fut in as_completed(futures):
                try:
                    results = fut.result()
                except RuntimeError:
                    results = {}
                yield futures[fut], results


# ── several models voting ─────────────────────────────────────────────────────────────────────
@dataclass
class Vote:
    issues: list[str]
    target: str
    update: bool
    safety: bool
    org: str | None
    detail: str
    agreement: float  # share of models that back the chosen primary issue
    models: dict[str, list[str]]


def combine(results: dict[str, TagResult], order: Sequence[str]) -> Vote:
    """Merge one review's answers from several models.

    An issue is kept when a majority of the models that answered name it. If no issue has a
    majority, the first model in `order` (the best one on the reference set) decides. Safety is
    raised if *any* model raises it — missing a safety report is worse than an extra one.
    """
    answered = [m for m in order if m in results]
    if not answered:
        raise ValueError("no model answered")
    need = len(answered) // 2 + 1
    votes: dict[str, int] = {}
    rank: dict[str, float] = {}
    for m in answered:
        for pos, code in enumerate(results[m].issues):
            votes[code] = votes.get(code, 0) + 1
            rank[code] = rank.get(code, 0.0) + pos
    kept = [c for c in votes if votes[c] >= need]
    if kept:
        kept.sort(key=lambda c: (-votes[c], rank[c] / votes[c]))
    else:
        kept = list(results[answered[0]].issues)
    agreement = votes.get(kept[0], 1) / len(answered)

    targets = [results[m].target for m in answered]
    target = max(set(targets), key=lambda t: (targets.count(t), -targets.index(t)))
    lead = results[answered[0]]
    orgs = [results[m].org for m in answered if results[m].org]
    detail = next((results[m].detail for m in answered if kept[0] in results[m].issues and results[m].detail), "")
    return Vote(
        issues=kept[:3],
        target=target,
        update=sum(results[m].update for m in answered) >= need,
        safety=any(results[m].safety for m in answered) or "safety_concern" in kept,
        org=lead.org or (orgs[0] if orgs else None),
        detail=detail or lead.detail,
        agreement=round(agreement, 3),
        models={m: results[m].issues for m in answered},
    )


class AnswerCache(Protocol):
    """Where raw per-model answers are kept, so runs can resume and votes can be recombined."""

    def get_answers(self, model: str, taxonomy_version: int) -> dict[str, tuple[str, dict[str, Any]]]: ...

    def save_answers(self, model: str, taxonomy_version: int, rows: list[tuple[str, str, dict[str, Any]]]) -> None: ...


def tag_reviews(
    reviews: Sequence[ReviewIn],
    taggers: Sequence[Tagger],
    *,
    taxonomy_version: int,
    cache: AnswerCache,
    on_progress: Callable[[str, int, int], None] | None = None,
    workers: int = 4,
    strategy: str = "vote",
) -> list[dict[str, Any]]:
    """Rule-tag trivial reviews; get every model's answer for the rest (reusing cached answers);
    combine them into one row per review. Reviews no model could tag are left for the next run."""
    rows: list[dict[str, Any]] = []
    to_model: list[ReviewIn] = []
    for r in reviews:
        ruled = rule_tag(r.score, r.content)
        if ruled is None:
            to_model.append(r)
            continue
        target = "app" if ruled == ["generic_positive"] else "none"
        rows.append(_row(r, taxonomy_version, "rule", None, Vote(ruled, target, False, False, None, "", 1.0, {})))
    if not to_model or not taggers:
        return rows

    answers: dict[str, dict[str, TagResult]] = {}  # review_id → model → result
    for t in taggers:
        cached = cache.get_answers(t.cache_key, taxonomy_version)
        todo: list[ReviewIn] = []
        for r in to_model:
            if strategy == "fallback" and answers.get(r.review_id):
                continue  # an earlier (better) model already answered
            hit = cached.get(r.review_id)
            if hit and hit[0] == r.content_hash:
                answers.setdefault(r.review_id, {})[t.model] = TagResult.model_validate(hit[1])
            else:
                todo.append(r)
        size = t.settings.batch_size
        batches = [todo[i : i + size] for i in range(0, len(todo), size)]
        done = 0
        if on_progress:
            on_progress(t.model, 0, len(todo))
        for batch, results in t.tag_batches(batches, workers=workers):
            by_id = {r.review_id: r for r in batch}
            save = []
            for rid, res in results.items():
                answers.setdefault(rid, {})[t.model] = res
                save.append((rid, by_id[rid].content_hash, res.model_dump()))
            cache.save_answers(t.cache_key, taxonomy_version, save)
            done += len(batch)
            if on_progress:
                on_progress(t.model, done, len(todo))

    order = [t.model for t in taggers]
    method = "vote" if strategy == "vote" and len(taggers) > 1 else "llm"
    for r in to_model:
        per_model = answers.get(r.review_id)
        if per_model and strategy == "fallback":
            first = next(m for m in order if m in per_model)
            per_model = {first: per_model[first]}
        if per_model:
            rows.append(
                _row(
                    r,
                    taxonomy_version,
                    method,
                    "+".join(m for m in order if m in per_model),
                    combine(per_model, order),
                )
            )
    return rows


def _row(r: ReviewIn, taxonomy_version: int, method: str, model: str | None, v: Vote) -> dict[str, Any]:
    return {
        "review_id": r.review_id,
        "content_hash": r.content_hash,
        "taxonomy_version": taxonomy_version,
        "method": method,
        "model": model,
        "issues": v.issues,
        "target": v.target,
        "mentions_update": v.update,
        "safety": v.safety,
        "org": v.org.strip()[:60] if v.org else None,
        "detail": (v.detail or "").strip()[:160],
        "agreement": v.agreement,
        "votes": v.models,
    }
