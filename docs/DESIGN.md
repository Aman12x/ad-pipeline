# Design notes

Engineering companion to the [README](../README.md): architecture, failure
modes, and the reasoning behind the design decisions.

Real ad pipelines are hard for reasons a static-CSV project never meets. This
project simulates and handles all of them:

| Production problem | Simulated how | Handled how |
|---|---|---|
| **Conversions restate retroactively** — platforms keep re-attributing conversions for ~7 days after the click | Reported conversions mature from ~50% → 100% of their final value depending on observation date | Every run re-pulls a 14-day trailing window and **upserts** on the natural key; append-only would silently under-report |
| **Rate limits** | Meta endpoint returns HTTP 429 (with `Retry-After`) every 4th call | Exponential backoff honoring `Retry-After`; the e2e test suite survives a 429 on every run sequence |
| **Platforms over-attribute** — Google + Meta + GA4 each claim the same conversion | First-party orders are ~85% of what platforms claim | Platform vs first-party conversions kept as **separate columns, never summed**; the gap ships as an `overclaim_ratio` metric |
| **Schema quirks** | Google: camelCase, `costMicros`, int64-as-strings; Meta: conversions buried in an `actions` array | All normalization isolated in one layer (`pipeline/load.py`), unit-tested against malformed payloads |
| **Bad rows** | — | Dead-lettered to `data/rejected/` with a reason; one garbage row never aborts a run |

## Architecture

```mermaid
flowchart LR
    subgraph sim["simulator (FastAPI)"]
        G["/google/campaign_stats"]
        M["/meta/insights<br/>429 every 4th call"]
        F["/firstparty/orders"]
    end
    subgraph pl["pipeline"]
        E[extract<br/>retry + backoff] --> R[(raw JSON<br/>landed as-is)]
        R --> L[load<br/>normalize + validate]
        L --> DLQ[(rejected/<br/>dead letters)]
        L --> S[(staging<br/>upsert on PK)]
        S --> T[dbt build<br/>models + tests] --> FA[(fact + marts)]
        FA --> Q[quality checks]
    end
    G & M & F --> E
    FA --> D[Streamlit dashboard]
    FA --> P[PNG report export]
```

Every run: pull trailing 14-day window → land raw JSON untouched (replayable)
→ normalize/validate → upsert staging → `dbt build` (fact + mart models and
their schema/singular tests) → data-quality gate → record run metadata in
`pipeline_runs`.

The transform layer lives in `dbt/`: staging tables are declared as dbt
sources with column tests; the fact table and marts are models; singular
tests enforce cross-column invariants (PK uniqueness, clicks ≤ impressions,
no negative metrics, WoW contribution shares summing to 1). A test failure
fails the build, which fails the run — bad data never reaches the marts.

### Marts

| Model | Question it answers | SQL of note |
|---|---|---|
| `mart_campaign_daily` / `mart_channel_daily` | What are CAC/ROAS/overclaim right now? | — |
| `mart_campaign_rolling` | Is efficiency trending, and is the creative fatiguing? | 7-day windows (`rows between 6 preceding`), `ctr_vs_peak` via windowed max over the campaign's own history |
| `mart_channel_pacing` | Will we hit the monthly budget? | MTD running sum vs a budget prorated with `last_day()`; joined to the `monthly_budgets` seed |
| `mart_campaign_weekly` | Which campaign drove this week's change? | `lag()` per campaign, each campaign's share of the channel's total spend delta (sums to 1 per channel-week, enforced by a singular test) |

## From raw data to decision

![Daily spend by channel](../assets/spend_by_channel.png)
![ROAS by campaign](../assets/roas.png)
![Platform overclaim](../assets/overclaim.png)

The marts answer the questions a growth team actually asks: which campaigns
are above/below break-even ROAS (shift budget), what CAC really is when
measured against **first-party orders** instead of platform claims, and how
much each platform over-attributes (here: 7–45%).

## Testing

- **Parser unit tests** — schema normalization, malformed payloads
  dead-lettered without killing the batch, sanity rules (clicks ≤ impressions)
- **End-to-end** — boots the real simulator, runs the pipeline three times and
  asserts: restatement picked up (same stat date, higher conversions),
  idempotency (repeat run adds zero rows), 429 survived, quality gates pass,
  every run tracked
- Tests pin their `as_of` dates and the simulator derives everything from
  `as_of`, so CI is deterministic — green today, green in a year

## Orchestration

`airflow/dags/ad_pipeline_dag.py` runs the stages as separate tasks
(extract → load → transform → quality) on a daily schedule with retries. The
DAG maps Airflow's logical date to the pipeline's `as_of`, so
`airflow dags backfill` replays history correctly. The same stages run
without Airflow via `python -m pipeline.run` — orchestration is a wrapper,
not a dependency.

## Design decisions

- **Trailing-window upserts, not append-only** — the restatement problem makes
  append-only ingestion silently wrong; this is the single most important
  design choice in any ad pipeline.
- **Raw payloads landed before parsing** — any load bug can be replayed from
  disk without re-hitting (rate-limited) APIs.
- **Fact/marts fully rebuilt from staging each run** (dbt `table`
  materializations) — trivially idempotent; switching to dbt `incremental`
  models is a scale problem this data volume doesn't have yet.
- **First-party truth joined at (source, campaign, day)** via UTM tags —
  click-ID-level joins need click-level exports; campaign-day granularity
  answers the budget questions without them.
- **Quality checks as a pipeline stage** — PK uniqueness, non-negative
  metrics, clicks ≤ impressions, freshness — failing the run rather than
  publishing bad marts.
- **The most recent day is excluded from efficiency metrics** — first-party
  orders land with a 1-day lag; showing a CAC computed on zero orders would
  be a lie. The dashboard says so instead.

## Swapping in real APIs

The simulator exists so the pipeline can be built and tested without
credentials — but the seams are real: replace the three URL builders in
`pipeline/extract.py` with authenticated calls (Google Ads GAQL, Meta
`/insights`, your orders DB) and everything downstream runs unchanged.
