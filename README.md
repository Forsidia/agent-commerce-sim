# agent-commerce-sim

Forsidia's open-source multi-agent commerce simulation. Agents run shops and buy
and sell virtual products from each other in a supermarket-style market. Buyers
make fast **System 1** decisions; sellers make occasional slow **System 2**
decisions with a hosted LLM. The goal is to study how decision quality, latency
and cost trade off in agent-to-agent markets.

It runs at three levels. Each level needs a bit more set up:

| Mode | Buyers | Sellers | Needs |
|---|---|---|---|
| Offline | utility baseline or fake System 1 | rules | nothing |
| API key | utility baseline | Claude Haiku or OpenAI (`--sellers llm`) | an API key, no GPU |
| Full | Ollama decision model (`--buyers s1`) | LLM | GPU with Ollama ≥ 0.35, plus an API key |

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Forsidia/agent-commerce-sim.git
cd agent-commerce-sim
uv sync --extra dev        # includes the LLM extra; use `uv sync --extra llm` without the test tools
uv run pytest              # 20 offline tests, no network or GPU
```

## 1. Offline

```bash
uv run acs run                          # 100 days, utility buyers, rule sellers, seed 42 (about 4 s)
uv run acs run --seed 7 --ticks 50      # other seed and length
uv run acs run --buyers fake-s1         # offline stand-in for System 1, exercises the gate and fallback
uv run acs run --no-shocks              # drop the scripted supply cut at day 50
```

## 2. API key (no GPU)

Sellers position their product line before day 0 (price and quality for each
product, return policy and opening ad spend). After that, every 5 days a rule
screen checks for stockouts, overstock, rating drops, high returns, undercutting
and shocks. The LLM is called only for the companies it flags.

Export the key in your shell. It is never read from `.env`.

```bash
export ANTHROPIC_API_KEY=...                 # Claude Haiku is the default seller model
uv run acs run --sellers llm
```

To use OpenAI instead:

```bash
export OPENAI_API_KEY=...
ACS_SELLER_MODEL=openai:gpt-5-mini uv run acs run --sellers llm
```

**What to expect:**
- **Calls:** about 5 for positioning plus up to 5 every 5 days, so roughly 100 calls
  for a 100-day run. Token counts are printed at the end and saved in
  `summary.json`.
- **Bounds:** the engine clamps every model output. Prices move at most ±20% per
  decision and stay within 0.5–2× the base price. Positioning prices fall within
  0.5–2× the category reference, and quality within 0.3–0.95. Higher quality
  raises producer wages.
- **Failures:** if a call fails (network, auth, timeout or invalid output), that
  company falls back to the rule seller for that decision. The run keeps going,
  and the decision is logged with `source: fallback`.
- **Skipping positioning:** `--no-positioning` keeps the scenario's prices and
  quality and uses the LLM only for the slow decisions.

## 3. Full (GPU, System 1 buyers)

```bash
ollama pull tev1:4b                          # Ollama >= 0.35; called via POST /v1/systemone
uv run acs run --buyers s1 --ticks 10        # smoke test first
uv run acs run --buyers s1 --sellers llm     # everything
```

Other decision models are `nimble-v3:9b-q4_K_M` (non-commercial licence) and
`d1:3b`. Set the model with `ACS_S1_MODEL`. If the model is unsure, gives an
invalid answer or errors, the buyer falls back to the utility baseline.

## Watch it: the 3D market viewer

```bash
uv run acs serve                 # http://127.0.0.1:8765, reads ./runs
```

Open the URL in a browser. While the server is up, start runs in another
terminal. Each run appears in the dropdown, and a run that is still going shows
**live** and is followed tick by tick. That's useful for the slower
`--sellers llm` and `--buyers s1` runs.

What the viewer shows:
- **Shops:** a low-poly supermarket with one striped-awning shop per company.
  Shelves show the real stock, with products modelled by category: snack cans,
  folded apparel, gadgets with screens, and gems for luxury. Price tags update
  live, and a gold ring on the floor shows hype. A red beacon marks a product
  hit by a shock.
- **Buyers:** they walk in from the plaza and are coloured by wealth tier
  (budget, mid, premium). A buyer picks an item from a shelf, pays a seller at
  the shop's counter, then walks home. The label above their head says what
  they bought and for how much.
- **Resale corner:** buyers list used items on the tables, and other buyers buy
  them. Returns go to the refund desk.
- **Panels:**
  - *Top sold items*: a cumulative leaderboard of units and revenue.
  - *Today* and *So far*: daily and running stats.
  - *Live purchases*: a ticker of who bought what.
  - *Shops*: cash sparkline, prices, stock and hype for each company.
  - *Seller decisions & shocks*: what each seller changed and why. LLM reasons
    appear here.

**Controls:**

| Key | Action |
|---|---|
| Space | play / pause |
| 1–9, or click a shop | fly the camera to that shop |
| 0 or Esc | back to the overview |
| O | slow cinematic orbit |
| H | hide the UI, for clean recordings |
| Mouse | drag to orbit, scroll to zoom |

The speed selector goes up to 8×, and the slider jumps to any day.

**For video:** use a desktop browser with a real GPU, press **H** and **O**, and
play at 1–2×. three.js loads from the jsDelivr CDN, so the browser needs
internet access.

## Run output and replay

Each run writes to `runs/seed<N>_<buyers>[_llm-sellers]/`. Event and tick logs
are appended every tick, which is how the viewer follows live runs.

| File | Contents |
|---|---|
| `config.json` | Seed, modes, seller model, every simulation parameter |
| `events.jsonl` | Deterministic event log: sales, returns, listings, ratings, seller actions, positioning, shocks |
| `decisions.jsonl` | Every decision, with System 1 probabilities, confidence, latency, and the LLM's reasons |
| `shocks.jsonl` | Admin and scenario interventions |
| `ticks.jsonl` | Per-day prices, stock, hype and cash |
| `summary.json` | Totals, failed transactions, profit per company, Gini, LLM token usage. Written last; a run without it is still going |

```bash
uv run acs replay runs/seed42_utility_llm-sellers   # re-run from recorded decisions with no model calls; checks the events are identical
```

Model runs are not deterministic: Haiku does not accept `temperature`. They are
reproduced by recording each decision and replaying it.

There is no database. Simulation state lives in memory, and each run is a
folder of JSONL and JSON files, which the viewer and any analysis read
directly.

## Configuration

Settings come from environment variables, or from `.env` (copy it from
`.env.example`). Real environment variables win. API keys are only read from
the shell environment, never from `.env`.

| Variable | Default | |
|---|---|---|
| `ACS_SELLER_MODEL` | `anthropic:claude-haiku-5-5` | any Pydantic AI model string |
| `ACS_LLM_CONCURRENCY` / `ACS_LLM_TIMEOUT_S` | `5` / `60` | |
| `ACS_SELLER_TRIGGER_EVERY` | `5` | days between seller screens |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | |
| `ACS_S1_MODEL` | `tev1:4b` | |
| `ACS_S1_CONCURRENCY` / `ACS_S1_TIMEOUT_S` | `4` / `30` | |
| `ACS_S1_CONF_MIN` / `ACS_S1_MARGIN_MIN` | `0.6` / `0.15` | System 1 confidence gate |

## Architecture

```
src/acs/
  engine.py            tick loop; owns all state; atomic settlement; invariants; positioning
  ledger.py            integer-cent ledger; mint (salaries) / sink (wages, marketing)
  models.py            state dataclasses + Pydantic decision schemas
  decisions/
    base.py            BuyerModel / TriggerDetector / SellerModel / PositioningModel protocols
    utility.py         UtilityBuyer, RuleTriggerDetector, RuleSeller (baselines)
    system_one.py      /v1/systemone query builder, Ollama + fake clients, confidence gate
    llm.py             LLMSeller: positioning + slow seller decisions via Pydantic AI (Anthropic/OpenAI)
    replay.py          replay recorded decisions
  web/
    server.py          `acs serve`: stdlib HTTP server over runs/
    static/index.html  three.js 3D market viewer
  scenarios/example.py 200 agents (170 buyers, 10 sellers, 20 producers), 5 companies, 4 categories
  runlog.py, config.py, cli.py
```

**Each tick (a day) runs in this order:**
1. Positioning (day 0 only, with `--sellers llm`)
2. Shocks
3. Salaries
4. Production (wages paid; quality and defects drawn)
5. Marketing
6. Seller triggers and actions (every K days)
7. Needs grow
8. Defects revealed and ratings left
9. All buyers decide, against the same snapshot
10. Settlement, in seeded order, against live state
11. Hype update
12. Invariant checks

**Design rules:**
- **Models never touch state.** They return decisions; the engine validates them,
  clamps them to bounds, and settles them atomically.
- **Shortlisting is rule-based.** The engine filters offers before any model
  call. Buyers with nothing to decide are skipped.
- **Every model has a fallback.** Low confidence, an invalid answer or an error
  falls back to a transparent baseline. The run never stops because of a model.
- **Seeded randomness.** Each subsystem has its own seeded RNG, and the event log
  contains no wall-clock time.

## Economy

- **Buyers:** lognormal wealth and wealth tiers, random salaries, needs that grow
  per category, preferences, price sensitivity and hype sensitivity.
- **Companies:** sell similar products at different quality levels. Low quality
  means more defects, and defects lower ratings.
- **Markets:** the primary market is the shops. The secondary market is
  buyer-to-buyer resale.
- **Returns:** allowed within 10 days. The refund is full, 50% or none,
  depending on the seller's policy.
- **Hype:** rises with sales, good ratings and marketing, and decays every day.

## Status

**Done:**
- Deterministic economy with both markets, production, returns, ratings and hype.
- Utility and rule baselines.
- System 1 client with a confidence gate.
- LLM sellers (Anthropic or OpenAI) for positioning and slow decisions.
- Record and replay.
- Scripted shocks.
- Live 3D viewer.
- 20 offline tests.

**Next:**
1. **Benchmark System 1 on the RTX 3060** (`tev1:4b`, `nimble-v3`, `d1:3b`):
   agreement, robustness to offer order, calibration, latency.
2. **Economy tuning:**
   - Many buyers pick the same resale listing in one tick, which causes about
     14k `LISTING_INACTIVE` failures per run.
   - C1, C2 and C3 lose money under the rule sellers (seed 42); only C4 and C5 profit.
   - The gate thresholds give the fake System 1 a fallback rate of about 93%.
3. **Admin controls in the viewer:** start runs and apply shocks live.

## Research goals

- How much of a slow model's decision quality can a fast System 1 model keep, at
  a fraction of the latency and cost?
- How does market behaviour (prices, hype cycles, returns, wealth inequality)
  respond to the decision model, the seller policy and shocks?
- What confidence gating gets the best trade-off between quality and calls?
