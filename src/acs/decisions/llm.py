"""Slow (System 2) seller decisions from a hosted LLM through Pydantic AI.

Works with any provider Pydantic AI supports, chosen by model string:
``anthropic:claude-haiku-5-5`` (needs ANTHROPIC_API_KEY) or ``openai:gpt-5-mini``
(needs OPENAI_API_KEY). Keys come from the shell environment only.

The model only proposes: the engine clamps every number to its bounds. Any
error or invalid output falls back to the rule baseline, so a run never stops
because of the network.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Literal

from pydantic import BaseModel, Field

from acs.decisions.utility import RuleSeller
from acs.models import (
    CompanyBrief,
    CompanyDashboard,
    PositioningPlan,
    ProductPosition,
    SellerAction,
    TriggerReport,
)

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

PROVIDER_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}

MARKET_RULES = """\
You run one company in a simulated consumer market. Money is in integer cents. 1 tick = 1 day.
- Buyers score each offer as 1.5 * desire * preference * expected_quality - price_sensitivity * price / ref_price
  and buy the best positive one. Rich buyers are less price sensitive.
- Buyers cannot see quality. They see ratings, which come in 2 ticks after a sale:
  rating = 1 + 4*quality, minus 2.5 if the unit is defective, minus 0.5 per 100% priced over ref_price.
- Defect chance = 0.25 * (1 - quality)^2. Defective units may be returned within 10 ticks for refund_pct of the price.
- Hype rises with sales, good ratings and marketing spend, and decays 15% per tick. Hype raises desire.
- Producers are paid a wage every tick they produce, sold or not. Unsold stock caps at 60 units per product.
- Buyers can resell used units to each other, which competes with your cheapest products.
Aim for long-run profit without going bankrupt."""

SELLER_INSTRUCTIONS = MARKET_RULES + """
You are called because a screen flagged your company (see `fired`). Decide:
- price_changes: product_id -> fractional change (e.g. -0.1 = 10% cut). Max +-0.2 per decision; omit unchanged products.
- refund_pct: 0, 50 or 100, or null to keep it.
- marketing_spend: cents per tick (capped at 10% of cash), or null to keep it.
- reason: one short sentence."""

POSITIONING_INSTRUCTIONS = MARKET_RULES + """
Before the market opens, position each of your products against competitors:
- price_mult: list price as a multiple of the category ref_price, within price_mult_range.
- quality: target quality mean within quality_range. Wages scale with (quality_wage_base + quality),
  so quality costs money every tick.
Also set refund_pct (0, 50, 100), opening marketing_spend in cents per tick, and a one-sentence statement
of your strategy. Include every one of your product_ids."""


class SellerOut(BaseModel):
    price_changes: dict[str, float] = Field(default_factory=dict)
    refund_pct: Literal[0, 50, 100] | None = None
    marketing_spend: int | None = None
    reason: str = ""


class PositioningOut(BaseModel):
    products: dict[str, ProductPosition]
    refund_pct: Literal[0, 50, 100] = 100
    marketing_spend: int = 0
    statement: str = ""


def missing_api_key(model: str) -> str | None:
    """Name of the env var a provider needs but is unset, else None."""
    var = PROVIDER_KEYS.get(model.partition(":")[0])
    return var if var and not os.environ.get(var) else None


class LLMSeller:
    """SellerModel + PositioningModel backed by one Pydantic AI model."""

    def __init__(self, model, *, concurrency: int = 5, timeout_s: float = 60.0):
        from pydantic_ai import Agent  # optional dependency: uv sync --extra llm

        self.model_name = model if isinstance(model, str) else getattr(model, "model_name", type(model).__name__)
        self.name = f"llm:{self.model_name}"
        self.seller_agent = Agent(model, output_type=SellerOut, instructions=SELLER_INSTRUCTIONS,
                                  defer_model_check=True, retries=2)
        self.position_agent = Agent(model, output_type=PositioningOut, instructions=POSITIONING_INSTRUCTIONS,
                                    defer_model_check=True, retries=2)
        self.sem = asyncio.Semaphore(concurrency)
        self.timeout_s = timeout_s
        self.fallback = RuleSeller()
        self.calls = 0
        self.errors = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def stats(self) -> dict:
        return {"model": self.model_name, "calls": self.calls, "errors": self.errors,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens}

    async def _ask(self, agent, prompt: str):
        async with self.sem:
            self.calls += 1
            t0 = time.perf_counter()
            result = await asyncio.wait_for(agent.run(prompt), self.timeout_s)
            usage = result.usage
            self.input_tokens += usage.input_tokens or 0
            self.output_tokens += usage.output_tokens or 0
            return result.output, round((time.perf_counter() - t0) * 1000, 1)

    # ---------------------------------------------------------------- SellerModel

    async def decide_many(
        self, dashboards: list[CompanyDashboard], reports: list[TriggerReport]
    ) -> list[SellerAction]:
        return list(await asyncio.gather(*(self._decide(d, r) for d, r in zip(dashboards, reports, strict=True))))

    async def _decide(self, d: CompanyDashboard, r: TriggerReport) -> SellerAction:
        prompt = json.dumps({"dashboard": d.model_dump(), "fired": r.fired}, sort_keys=True)
        try:
            out, ms = await self._ask(self.seller_agent, prompt)
        except Exception as e:  # network, auth, validation after retries, timeout
            self.errors += 1
            action = (await self.fallback.decide_many([d], [r]))[0]
            return action.model_copy(update={"source": "fallback", "reason": f"LLM_ERROR:{type(e).__name__}"})
        return SellerAction(company_id=d.company_id, price_changes=out.price_changes, refund_pct=out.refund_pct,
                            marketing_spend=out.marketing_spend, reason=out.reason[:300], source="llm",
                            latency_ms=ms)

    # ---------------------------------------------------------------- PositioningModel

    async def position_many(self, briefs: list[CompanyBrief]) -> list[PositioningPlan]:
        return list(await asyncio.gather(*(self._position(b) for b in briefs)))

    async def _position(self, b: CompanyBrief) -> PositioningPlan:
        try:
            out, ms = await self._ask(self.position_agent, json.dumps(b.model_dump(), sort_keys=True))
        except Exception as e:
            self.errors += 1
            return PositioningPlan(company_id=b.company_id, statement=f"LLM_ERROR:{type(e).__name__}",
                                   source="fallback")  # empty plan: keep the scenario's product line
        return PositioningPlan(company_id=b.company_id, products=out.products, refund_pct=out.refund_pct,
                               marketing_spend=out.marketing_spend, statement=out.statement[:300], source="llm",
                               latency_ms=ms)
