# MoveInSync Play Store ratings: diagnosis and monitor

This answers *“How are our Play Store ratings, and what should we fix?”*, and keeps answering it:
the results refresh themselves every 2 hours from the latest Google Play data.

## See the results

**Live site:** <https://roshankarthik.github.io/moveinsync-ratings/>

Nothing to install, no sign-up: open the link.

| Page | What it shows |
|---|---|
| [Monitor](https://roshankarthik.github.io/moveinsync-ratings/) | How the ratings are doing right now: the rating, what changed, what to do this week, what drives bad ratings, release health, replies, safety, and whether each fix is working |
| [Recommendation](https://roshankarthik.github.io/moveinsync-ratings/deliverables/recommendation.html) | What to fix and what to do first: the answer, the causes, a plan in three horizons with owners and evidence, an operations track, the reply playbook and how success is measured |

The same files are in this repository and open in any browser, offline:

| File | What it is |
|---|---|
| `deliverables/recommendation.html` | The recommendation (one file; works offline, on a phone and in print) |
| `deliverables/backlog.xlsx` | One row per ticket: owner, effort, evidence, change, acceptance criteria, success metric, baseline; plus the evidence for every issue |
| `deliverables/backlog_jira.csv` | The same tickets for Jira's CSV import (Summary, Issue Type, Priority, Labels, Description) |
| `deliverables/reply_playbook.md` | Google-compliant reply templates per issue, routing, and the current number of unanswered 1–2★ reviews per issue |
| `reports/latest.html` | The monitor |
| `docs/METHOD.md` | How the numbers are made, the accuracy results, assumptions and limits |

## How it works

Every 2 hours a scheduled GitHub workflow:

1. downloads the Play Store reviews that are new or edited since the last run, plus the store
   listing and three comparable apps;
2. sorts each new review into one of 33 issues with an AI model (NVIDIA Nemotron); on 120
   labelled test reviews it named the right main issue 99% of the time (95% CI 95–100%);
3. recomputes every number, with 95% intervals and significance tests;
4. rebuilds the monitor and the recommendation and publishes them.

Each run takes about 3 minutes. GitHub may start a scheduled run some minutes late when its
servers are busy. The pages show when they were last updated; the indicator turns amber if a
refresh has been missed.

**What updates and what does not.** Every number, chart, quote, trend and the Fix tracker update
on each run (the app gets a few written reviews a day, so many runs change little). The plan
itself (tickets, owners, priorities) comes from `backlog.yaml` and changes only when someone edits
it, so teams are not chasing a list that reorders itself. When the data shows something the plan
does not cover (a new emerging or rising issue, or a new top-5 cost), the recommendation shows a
**Not yet in the plan** box at the top.

## Run your own copy (optional)

Run `uv run ratings refresh` ([uv](https://docs.astral.sh/uv/) installs everything else). It
downloads new reviews, rebuilds both pages and opens the monitor. New reviews are sorted into
issues when a model key is configured; otherwise the pages are rebuilt from the existing issue tags.

```
uv run ratings refresh   # everything: download → tag → report → open
uv run ratings doctor    # check the setup and say what to fix
uv run ratings fetch     # only download
uv run ratings tag       # only tag new/edited reviews
uv run ratings report    # only rebuild the monitor
uv run ratings recommend # rebuild the recommendation, backlog exports and reply playbook
                         #   (--no-freeze: never write baselines; used by the scheduled run)
```

### Changing the plan or what is measured

- **The plan:** edit `backlog.yaml` (add a ticket, change an owner), run `uv run ratings recommend`
  (this records a new ticket's Fix-tracker baseline) and push. Scheduled runs never change
  baselines; `--rebaseline` resets them.
- **A new kind of complaint** (it shows up under "Other"): add an issue to `taxonomy.yaml` and bump
  `version`. The next refresh re-tags reviews under the new taxonomy.
- **Another app** (the driver app, a competitor): add it under `benchmarks` or `sibling_apps` in
  `config.yaml`.
- **Other models:** edit `llm.models` in `config.yaml` and check their accuracy before trusting
  the new numbers (see `docs/METHOD.md`).

## What's in the box

| Path | What it is |
|---|---|
| `config.yaml` | The app, comparison apps, analysis windows and models. No secrets. |
| `taxonomy.yaml` | The 33 issues reviews are sorted into, each with its journey stage, owner and root-cause type |
| `backlog.yaml` | The recommended fixes and reply templates: the judgement behind the recommendation. Every number is added from the data at build time. |
| `data/eval/` | The tagging model's measured accuracy on 120 labelled test reviews, shown on both pages |
| `docs/` | How the numbers are made (`METHOD.md`) and the Google Play sources (`SOURCES.md`); linked from the pages |
| `src/moveinsync_ratings/` | The code: `fetch` → `store` → `tagger` → `analysis` → `report` / `recommend` |
| `.github/workflows/refresh.yml` | The 2-hourly refresh and publish |
| `scripts/seed_data_branch.py` | One-time upload of the review database for the scheduled refresh |

## Privacy

Only public Google Play data is used. Reviewer names and profile pictures are never stored.
Quotes are shown without names. The pages do name employers that reviewers mentioned.
