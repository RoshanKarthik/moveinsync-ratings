# How the numbers are made

This note covers data sources, tools, the tagging model and its measured accuracy, the statistics,
and what the numbers can and cannot tell you.

## 1. Data

| Source | What | How |
|---|---|---|
| Google Play reviews, `com.moveinsync.ets` | 24,314 written reviews, Mar 2014 – 9 Oct 2026 (India store, English, which is over 99% of reviews) | `google-play-scraper`, newest first, full history on the first run, then incremental |
| Google Play listing | Rating, rating count, star counts, installs, version | Same library; one snapshot per run, so a rating history builds up over time |
| Comparison apps | Safetrax Commuter, WhistleDrive EmployeeApp, SMT Employee: listing plus the last year of reviews and replies | Same |
| Other MoveInSync apps | Partner (driver) Lite, Driver Partner, Field app: listing only | Same |

Reviewer names and avatars are never stored. Timestamps are stored in UTC and reported in India
time.

Google publishes star counts that are rescaled to match its recency-weighted rating (see
`SOURCES.md`). Figures derived from those counts, such as the silent-rater estimate and the
rating scenarios, are therefore approximate. They are labelled that way wherever they appear.

## 2. Issue taxonomy

`taxonomy.yaml` defines 33 codes: 29 specific issues and 4 general ones. They were built by reading
about 300 recent 1–3★ reviews and grouping complaints along the employee journey described in the
brief: sign-in → booking → pickup time and routing → tracking → boarding → contacting the driver →
service → feedback.

Every issue has a fixed **owner** and **root-cause type**:

| Root-cause type | Meaning |
|---|---|
| defect | Broken |
| design | Works, but badly designed |
| policy | A rule set by MoveInSync or the employer |
| service | The cab, driver or helpdesk failed |
| gap | A missing feature |

Because owner and type come from the taxonomy, not from the model, they are consistent everywhere.

## 3. Tagging

- **Rules first.** Short, clearly positive reviews ("good", "nice app") are tagged
  `generic_positive` by a rule. About a third of reviews never reach a model.
- **Model.** Each review gets 1–3 issue codes plus: who the complaint is about (app / service),
  whether a recent update is blamed, whether safety is raised, the employer if named, and a
  12-word specific. Reviews go 20 to a request, at temperature 0, and every answer is validated
  against the taxonomy.
- **Resilience.**
  - Malformed answers are retried, and a batch that keeps failing is split in half.
  - If the primary model fails or times out, a fallback model tags only the gaps.
  - Raw answers are cached per model and prompt version, so an interrupted run loses nothing.
  - Unchanged reviews are never re-tagged.

### Choosing the model

Free NVIDIA NIM models were probed on 20 reviews, then scored on the labelled reference sets:

| Model (thinking off) | Availability on 11 Oct 2026 | Dev set: main issue correct (n=160) |
|---|---|---|
| **nvidia/nemotron-3-ultra-550b-a55b** | ✅ ~35 s per 20 reviews | **98%** |
| nvidia/nemotron-3-super-120b-a12b | ✅ ~6 s per 20 reviews | 95% |
| nvidia/nemotron-3.5-lightning-30b-a3b | ✅ ~9 s | 84% |
| z-ai/glm-5.3-flash | ⚠️ ~70 s, then stalled | not completed |
| deepseek-ai/deepseek-v4.1-flash, moonshotai/kimi-k3, google/gemma-4-31b-it | ❌ timed out | — |
| mistral-large(-2), kimi-k2.6, llama-3.1-nemotron-70b/ultra-253b, gemma-3-12b, nemotron-4-340b, yi-large, jamba-1.5-large | ❌ 404, listed but not served | — |
| openai/gpt-oss-20b | ❌ invalid JSON | — |

Majority voting was tested too. All three working models are from one family, so their errors
overlap: a three-model vote scored *below* Ultra alone (93% vs 97% before the final prompt), and a
two-model vote traded 6 points of recall for under 1 point of precision.

**Decision:** Ultra as primary, Super as fallback (`strategy: fallback` in `config.yaml`). Voting
stays available (`strategy: vote`) for when a reliable model from another family exists.

"Thinking" was switched off (`@nothink`). With it on, a 20-review request spent its whole token
budget reasoning and returned no answer. Off, it answered correctly in a fraction of the time.

### Measured accuracy

Two reference sets of labelled reviews (`data/gold/reference_labels.jsonl`):

- **dev, 160 reviews** (stratified 70/25/25/20/20 by star). Used while writing the prompt.
- **test, 120 reviews** (60/20/20/10/10, weighted to complaints). Labelled **before any model
  output existed** and never used for tuning. **These are the numbers to quote.**

Each label lists the issues a careful reader would assign, plus defensible alternatives. A
model's main issue counts as correct if it is either.

| Held-out test (n=120) | Main issue correct (95% CI) | Tag precision | Tag recall | Safety reviews missed |
|---|---|---|---|---|
| **Nemotron-3 Ultra (primary)** | **99%** (95–100%) | **99%** | **90%** | **0 of 2** |
| Nemotron-3 Super (fallback) | 98% (94–100%) | 97% | 86% | 0 of 2 |

Per-issue precision is shown in the report next to each issue.

> **Who labelled the reference sets:** the reference labels were written by the analyst with an
> AI assistant, against the written definitions in `taxonomy.yaml`, before any tagging
> model was run. Spot-check by the author: _[to be completed: number of labels reviewed and
> number changed]_.

**Safety precision.** After the first full run, the model was found to flag *praise* about safety
("safe ride") as well as concerns. Two fixes followed:

- The prompt now asks for concerns only.
- The analysis counts a safety concern only when the review is also a complaint (≤3★, or names
  another negative issue).

The held-out scores above were re-measured after the prompt change. Reviews tagged before it keep
their tags; the analysis rule applies to them as well.

**What recall of 90% means:** a review naming three problems sometimes gets two. Issue shares are
therefore slightly conservative (they undercount), but rankings are stable, because the misses
are spread across issues.

## 4. Statistics

- **Negative** = 1–2★.
  - Issue shares are "% of negative reviews in the window that name the issue".
  - A review can name up to three issues, so shares do not sum to 100%.
- **Uncertainty.** Every share has a 95% Wilson interval and its *n*. Every average has a 95%
  normal interval.
- **Change over time.**
  - Last 90 days vs the 90 days before, two-proportion z-test. "Rising" or "falling" only at
    p < 0.05.
  - "Emerging" = last 30 days vs the previous 180, p < 0.05, with at least 3 mentions.
- **Releases.**
  - Each version with 25 or more reviews in the last year is compared with all other versions.
  - Releases are flagged "worse" or "better" only after a Bonferroni correction for the number of
    releases tested.
  - "Watch" means p < 0.05 before the correction, which is not conclusive.
- **Rating cost of an issue.**
  - Take every 1–2★ review naming the issue.
  - Replace its stars with the average of reviewers who named no complaint.
  - The result is the change in the year's average written rating.
  - A review naming several issues is split evenly between them, so the costs add up.
- **Fix tracker.** "Improved" or "worse" only when the whole 95% interval of the current share is
  past the recorded baseline.
  - The baseline is the share of 1–2★ reviews naming the fix's first issue in the 90 days before
    the recommendation, written into `backlog.yaml` once by `ratings recommend`.
  - With about 200 1–2★ reviews per 90 days, only large drops can be confirmed. The
    recommendation shows, per fix, the share at which the whole interval clears the baseline.
    Smaller effects need the internal metric each ticket names.

## 4a. How the recommendation is built

- **Plan and numbers are separate.** `backlog.yaml` holds the judgement: the change, owner,
  effort estimate, acceptance criteria and success metric. Evidence (shares, intervals, trends,
  rating cost, quotes) is filled in from the analysis at build time.
- **Ranking.** Issues are ranked by rating cost over the last 365 days, then checked for trend
  (90 days vs the 90 before), emerging signals (last 30 days) and release flags. Within each
  horizon, tickets are ordered by expected impact and urgency; that ordering is judgement.
- **Quotes** are verbatim 1–3★ reviews, most recent first, matched to the ticket's issue (and
  keywords or release where the ticket names them), never repeated across tickets, and shown
  without names.
- **Rating scenarios** change the share of 1–2★ ratings in Google's star histogram and keep the
  mix within 1–2★ and within 3–5★ as today. "The plan" scenario lowers that share by the part of
  1–2★ written reviews attributed to the issues of this-week and next-release product tickets
  (a review naming several issues is split evenly). It assumes the store's 1–2★ share moves with
  written 1–2★ reviews, which is plausible because most 1★ ratings carry text. All scenarios are
  directional.
- **Star-change log.** Play shows only a review's latest stars. From this version on, every
  refresh records star changes on existing reviews (`review_changes` table), with whether a
  developer reply came first. This measures whether resumed replies lead to better ratings.

## 5. Limits: read before acting

1. **Written reviews are not all users.** About 21% of ratings carry text, and writers skew
   negative. Use them to find *what* goes wrong and *how often, relative to other problems*, not
   to estimate how many trips go wrong.
2. **One public storefront.** There are no crash, ANR, OTP-delivery or trip data. Several
   recommendations name the internal metric that would confirm or size the issue (Android
   vitals, SMS gateway delivery logs, GPS ping frequency).
3. **Employer configuration is invisible from outside.** Some complaints (calls disabled,
   booking cut-offs, forced logout) may be one employer's settings. Employer names in reviews
   help, but they are sparse.
4. **The Play rating is recency-weighted and its exact formula is private.** Rating scenarios
   are directional. The monitor's weekly listing snapshots will measure the real effect.

## 6. Tools used

| Purpose | Tools |
|---|---|
| Language, data and storage | Python 3.12, pandas, SQLite |
| Public data access | google-play-scraper |
| Tagging models | NVIDIA NIM free tier (Nemotron-3 Ultra/Super) through the OpenAI-compatible API |
| Report and recommendation | Jinja2, with hand-built SVG charts (no chart library, so each page is one offline file) |
| Backlog exports | openpyxl (Excel), csv (Jira import) |
| Command line | Typer and Rich |
| Quality checks | pytest, ruff, mypy (strict) |
| AI assistance | Code, taxonomy drafting, reference labelling (see the note above), analysis review |
