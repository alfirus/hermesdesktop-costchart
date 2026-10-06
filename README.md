# hermesdesktop-costchart

A **Hermes Desktop plugin**: one page of daily charts showing what the fleet
spends on each AI lane — **Xiaomi (MiMo token plan)**, **OpenCode Go
(muse-spark via OpenCode Zen)**, **Meta (muse-spark)** and **Local LM
Studio** — and an estimate of **how many tokens Vectorizer saves us**
by answering knowledge questions from retrieval instead of context-heavy reads.

```
costchart/                      ← the plugin package (this repo root)
├── plugin.yaml                 # agent-plugin manifest
├── __init__.py                 # no-op register() — the halves below are the product
├── dashboard/
│   ├── manifest.json           # wires plugin_api.py (backend-only: tab hidden)
│   └── plugin_api.py           # FastAPI router → /api/plugins/costchart/daily
└── desktop/
    └── plugin.js               # the single-page chart (ROUTES_AREA /costchart)
```

## What the page shows

- **Daily cost by provider (USD, est.)** — stacked bars. Xiaomi and local come
  from the session ledgers' `estimated_cost_usd`; Meta and OpenCode Go are
  priced separately (below).
- **Daily tokens by provider** — input + output tokens.
- **Daily tokens saved by Vectorizer (est.)** — see the model below.
- Summary cards + a **Method** block stating every assumption.

**Meta pricing note:** the ledger has no price for `api.meta.ai`
(`cost_status: unknown` on every muse-spark session), so the backend computes
the Meta lane from the model's public list rates instead of showing a fake $0 —
`muse-spark-1.3-contributor` at **$0.10/M input, $0.20/M output, $0.002/M cached
input** (the Contributor tier: training rights in exchange for the discount;
the standard `muse-spark-1.3` ID is $1.25/$4.25). Sources: Baseer model listing,
explainx.ai cost page, MyClaw.ai pricing table (Oct 2026).

**OpenCode Go pricing note:** `muse-spark-1.3-contributor` sessions arriving via
`opencode.ai/zen/go` (`billing_provider: opencode-go`) are attributed to their
own lane — not Meta. Same honesty policy: the ledger carries no zen price
(`cost_status: unknown`, `estimated_cost_usd` 0 on 30 of 31 sessions checked
2026-10-06), so unpriced zen sessions fall back to the same Contributor list
rates above (no separate zen list rate is documented; OpenCode Zen Go passes
through the model price). A ledger estimate wins whenever present and priced —
the one priced zen session keeps its own cost instead of being repriced.

## How the data is computed

Backend scans every profile's session ledger (`<hermes root>/profiles/*/state.db`
plus the root `state.db`), opened **read-only**.

- **Provider lanes** by `billing_base_url` / `billing_provider`:
  `token-plan-sgp.xiaomimimo.com` → xiaomi ·
  `opencode.ai/zen/go` (or `billing_provider: opencode-go`) → opencode-go ·
  `api.meta.ai` → meta ·
  `localhost:1234` / `127.0.0.1:1234` → local · anything else → other.
  (`muse-spark` sessions arriving via the zen URL land in opencode-go, not meta;
  only genuine `api.meta.ai` sessions stay in the meta lane.)
- **Daily attribution**: the ledger stores totals per session and long sessions
  span days, so each session's totals are spread across its active days
  pro-rata by that day's message weight (`messages.token_count`, falling back to
  content length / 4 as a token estimate). Days are MYT (UTC+8).
- **Vectorizer savings (floor estimate)**:

  ```
  saved/day = vectorizer_calls/day × max(0, REPLACED_PER_LOOKUP × avg_knowledge_read
                                          − avg_vectorizer_result)
  usd_saved = saved_tokens × blended_usd_per_token   (fleet est. cost ÷ non-cache tokens)
  ```

  Both averages are **measured from the ledgers** (tool-result sizes:
  knowledge = `session_search`, `read_file`, `skill_view`, `web_extract`,
  `search_files`; retrieval = `vectorizer_ask` / `vectorizer_search*` results).
  `REPLACED_PER_LOOKUP = 2.0` is a stated assumption (a retrieval replaces a
  search probe AND a source read). The model is a deliberate floor: it excludes
  the search-explore tail (e.g. `session_search` results up to ~26k tokens) and
  the cache re-reads an avoided token skips on every later turn of a long
  session — where the real multiplier lives.

## Install

```bash
# copy the package into your Hermes root
git clone https://github.com/alfirus/hermesdesktop-costchart.git \
  "<hermes-root>/plugins/costchart"
```

Then two switches:

1. **Backend**: add `costchart` to `plugins.enabled` in `config.yaml`
   (user plugin backends are never imported before they are explicitly enabled —
   GHSA-mcfc-hp25-cjv7).
2. **Desktop half**: open **Capabilities → Plugins** in the Desktop app and
   toggle **Cost Chart** on (unified-package halves are opt-in). The sidebar
   gains a **Cost Chart** row (also via ⌘K → "Open Cost Chart").

The page lives at route `/costchart`, polls every 5 minutes, and needs no
credentials of its own.

## Requirements

- Hermes Desktop app (the page is a desktop plugin; the backend also serves the
  web dashboard's `/api/plugins/costchart/` namespace).
- FastAPI available in the Hermes runtime (standard).

## Notes

- Costs are the ledgers' `estimated_cost_usd` — fine for ranking spend, not for
  invoicing.
- Local LM Studio shows $0 API cost (compute/electricity only).
- The page computes nothing outside your machine; no data leaves the host.

MIT licensed.
