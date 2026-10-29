# agent-commerce-sim

Forsidia's open-source multi-agent commerce simulation. Agents run storefronts
and buy and sell virtual products from each other. Most decisions are made by
fast local **System 1** decision models; occasional slow decisions go to an LLM
(**System 2**, Claude Haiku). The goal is to study how decision quality, latency
and cost trade off in agent-to-agent markets.

## Setup

```bash
uv sync --extra dev
uv run pytest
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

## Run

```bash
uv run acs run --seed 42 --ticks 100                 # deterministic utility buyers
uv run acs run --buyers fake-s1                      # offline fake System 1 (no GPU)
uv run acs run --buyers s1                           # Ollama decision model (ACS_S1_MODEL)
uv run acs replay runs/seed42_fake-s1                # re-run from recorded decisions, verify identical
```

Each run writes these files to `runs/seed<N>_<buyers>/`:

| File | Contents |
|---|---|
| `events.jsonl` | Deterministic event log |
| `decisions.jsonl` | Every decision, with System 1 probabilities, confidence and latency |
| `shocks.jsonl` | Admin and scenario interventions |
| `ticks.jsonl` | Per-tick prices, stock, hype and cash |
| `summary.json` | Sales, revenue, profit, returns, failed transactions, buyer utility, Gini |

### System 1 with Ollama

To use a local decision model:

```bash
ollama pull tev1:4b
cp .env.example .env
uv run acs run --buyers s1
```

- Requires Ollama 0.35 or later.
- The model is called through `POST /v1/systemone`.
- Other models: `nimble-v3:9b-q4_K_M` (non-commercial licence) and `d1:3b`.

`ANTHROPIC_API_KEY` is never stored in `.env`. Export it in your shell when the
Haiku sellers land.

## Architecture

```
src/acs/
  engine.py            tick loop; owns all state; atomic settlement; invariants
  ledger.py            integer-cent ledger; mint (salaries) / sink (wages, marketing)
  models.py            state dataclasses + Pydantic decision schemas
  decisions/
    base.py            BuyerModel / TriggerDetector / SellerModel protocols
    utility.py         UtilityBuyer, RuleTriggerDetector, RuleSeller (baselines)
    system_one.py      /v1/systemone query builder, Ollama + fake clients, confidence gate
    replay.py          replay recorded decisions
  scenarios/example.py 200 agents (170 buyers, 10 sellers, 20 producers), 5 companies, 4 categories
  runlog.py, config.py, cli.py
```

**Each tick runs in this order:**
1. Shocks
2. Salaries
3. Production (wages paid; quality and defects drawn)
4. Marketing
5. Seller triggers and actions (every K ticks)
6. Needs grow
7. Defects revealed and ratings left
8. All buyers decide, against the same snapshot
9. Settlement, in seeded order, against live state
10. Hype update
11. Invariant checks

**Design rules:**
- **Models never touch state.** They return decisions; the engine validates them
  and settles them atomically.
- **Shortlisting is rule-based.** The engine filters offers before any model call.
  Buyers with nothing to decide are skipped.
- **System 1 answers are gated.** One call per buyer per tick asks typed questions
  (`purchase`, `return_item`, `resell_item`, `resell_price`). Low confidence, an
  invalid answer or an error falls back to the utility baseline.
- **Seeded randomness.** Each subsystem has its own seeded RNG, and the event log
  contains no wall-clock time.
- **Replay instead of determinism.** Non-deterministic models reproduce exactly
  through `acs replay`.

## Economy

- **Buyers:** lognormal wealth and wealth tiers, random salaries, needs that grow
  per category, preferences, price sensitivity and hype sensitivity.
- **Companies:** sell similar products at different quality levels. Low quality
  means more defects, and defects lower ratings.
- **Markets:** the primary market is company storefronts. The secondary market is
  buyer-to-buyer resale.
- **Returns:** allowed within 10 ticks. The refund is full, 50% or none,
  depending on the seller's policy.
- **Hype:** rises with sales, good ratings and marketing, and decays every tick.

## Status

**Done (base):**
- Deterministic economy with both markets, production, returns, ratings and hype.
- Utility and rule baselines.
- System 1 client (Ollama and fake) with a confidence gate.
- Record and replay.
- Scripted shocks.
- 14 offline tests.

**Next:**
1. **Benchmark on the RTX 3060** (`tev1:4b`, `nimble-v3`, `d1:3b`): agreement,
   robustness to offer order, calibration, latency.
2. **Economy tuning:**
   - Many buyers pick the same resale listing in one tick, which causes about
     14k `LISTING_INACTIVE` failures per run.
   - C1 and C2 lose money.
   - The gate thresholds give the fake System 1 a fallback rate of about 93%.
3. **Haiku sellers** through Pydantic AI (`SellerModel`), with System 1 seller
   triggers.
4. **Admin single-page app:** live controls and shocks.

## Research goals

- How much of a slow model's decision quality can a fast System 1 model keep, at
  a fraction of the latency and cost?
- How does market behaviour (prices, hype cycles, returns, wealth inequality)
  respond to the decision model, the seller policy and shocks?
- What confidence gating gets the best trade-off between quality and calls?
