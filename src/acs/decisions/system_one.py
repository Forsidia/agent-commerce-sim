"""System 1 buyers: local Ollama decision models via ``POST /v1/systemone``.

A decision model does not generate text: given a state and typed questions it
returns a probability per option in one forward pass. One call per buyer per
tick asks every question that buyer needs (purchase / return / resell).

Low confidence, invalid answers and errors fall back to the utility baseline;
every raw answer is kept on the decision for later analysis.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from typing import Any, Protocol

import httpx

from acs.decisions.utility import offer_utility, utility_decision
from acs.models import BuyerDecision, BuyerRequest, ReasonCode

PROMPT_VERSION = "buyer-v1"
RESELL_LEVELS = [30, 50, 70, 90]  # % of price paid, score levels 0..3

PURCHASE_INSTRUCTIONS = (
    "You are deciding for one shopper in a market simulation. Pick the single offer they should buy "
    "this turn, or none. Buy only if the item serves a current need (or strong hype) and its price is "
    "justified by expected quality, compared with the typical price and the shopper's price sensitivity. "
    "A known defect makes an offer much less valuable."
)
RETURN_INSTRUCTIONS = (
    "Should the shopper return the item in return_candidate for the stated refund? Return defective items "
    "when the refund is worthwhile."
)
RESELL_INSTRUCTIONS = (
    "Should the shopper list the item in resell_candidate for resale? Resell when they no longer need "
    "that category."
)
RESELL_PRICE_INSTRUCTIONS = "What resale price, as a share of the price paid, should they ask?"


def _dollars(cents: int) -> str:
    return f"${cents / 100:.2f}"


def build_query(req: BuyerRequest) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Compact state + typed questions. Returns (state, questions, option_name -> offer_id)."""
    option_map: dict[str, str] = {}
    offers: dict[str, Any] = {}
    criteria: dict[str, str | None] = {}
    for i, o in enumerate(req.offers, start=1):
        name = f"offer_{i}"
        option_map[name] = o.offer_id
        offers[name] = {
            "market": o.market,
            "category": o.category,
            "price": _dollars(o.price),
            "typical_price": _dollars(o.ref_price),
            "expected_quality": round(o.expected_quality, 2),
            "rating": round(o.rating, 1) if o.rating is not None else None,
            "ratings": o.rating_count,
            "hype": round(o.hype, 2),
            "known_defect": o.defect_known,
        }
        criteria[name] = f"Buy {name} ({o.category}, {_dollars(o.price)}, {o.market} market)."
    criteria["none"] = "Buy nothing this turn."
    state: dict[str, Any] = {
        "shopper": {
            "cash": _dollars(req.cash),
            "wealth_tier": req.wealth_tier,
            "price_sensitivity": round(req.price_sensitivity, 2),
            "hype_sensitivity": round(req.hype_sensitivity, 2),
            "needs": {k: round(v, 2) for k, v in req.needs.items()},
            "preferences": {k: round(v, 2) for k, v in req.prefs.items()},
        },
        "offers": offers,
    }
    questions: dict[str, Any] = {}
    if req.offers:
        questions["purchase"] = {"type": "choice", "instructions": PURCHASE_INSTRUCTIONS, "criteria": criteria}
    if req.returnable:
        r = req.returnable
        state["return_candidate"] = {
            "category": r.category, "price_paid": _dollars(r.price_paid), "refund_pct": r.refund_pct,
            "defective": r.defective, "days_left": r.ticks_left,
        }
        questions["return_item"] = {
            "type": "noul", "instructions": RETURN_INSTRUCTIONS,
            "criteria": {"true": "Return it.", "false": "Keep it."},
        }
    if req.resellable:
        s = req.resellable
        state["resell_candidate"] = {
            "category": s.category, "price_paid": _dollars(s.price_paid),
            "new_price_now": _dollars(s.reference_price), "known_defect": s.defect_known,
        }
        questions["resell_item"] = {
            "type": "noul", "instructions": RESELL_INSTRUCTIONS,
            "criteria": {"true": "List it for resale.", "false": "Keep it."},
        }
        questions["resell_price"] = {
            "type": "score", "instructions": RESELL_PRICE_INSTRUCTIONS,
            "criteria": [f"{p}% of price paid" for p in RESELL_LEVELS],
        }
    return state, questions, option_map


# ---------------------------------------------------------------- clients


class SystemOneClient(Protocol):
    model: str

    async def ask(self, state: dict, questions: dict, *, context: BuyerRequest | None = None) -> dict: ...


class OllamaSystemOneClient:
    """Raw HTTP client for Ollama's ``/v1/systemone`` (Ollama >= 0.35)."""

    def __init__(self, base_url: str, model: str, timeout_s: float = 30.0, keep_alive: str = "30m"):
        self.model = model
        self.keep_alive = keep_alive
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)

    async def ask(self, state: dict, questions: dict, *, context: BuyerRequest | None = None) -> dict:
        body = {"model": self.model, "state": state, "questions": questions, "keep_alive": self.keep_alive}
        resp = await self._http.post("/v1/systemone", json=body)
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        await self._http.aclose()


class FakeSystemOneClient:
    """Offline stand-in with the same answer shape as the real endpoint.

    Scores options with the utility function plus deterministic, state-hashed
    noise, then softmaxes them, so it is reproducible, mostly agrees with the
    baseline, and sometimes is unsure. Optional ``latency_s`` simulates load.
    """

    def __init__(self, model: str = "fake-s1", noise: float = 0.15, temperature: float = 0.15,
                 latency_s: float = 0.0):
        self.model = model
        self.noise = noise
        self.temperature = temperature
        self.latency_s = latency_s

    async def ask(self, state: dict, questions: dict, *, context: BuyerRequest | None = None) -> dict:
        if context is None:
            raise ValueError("FakeSystemOneClient needs the originating request as context")
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        seed = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).digest()
        answers: dict[str, Any] = {}
        if "purchase" in questions:
            scores = {f"offer_{i}": offer_utility(context, o) + self.noise * (seed[i] / 255 - 0.5)
                      for i, o in enumerate(context.offers, start=1)}
            scores["none"] = 0.0
            mx = max(scores.values())
            exp = {k: math.exp((v - mx) / self.temperature) for k, v in scores.items()}
            z = sum(exp.values())
            probs = {k: round(v / z, 6) for k, v in exp.items()}
            choice = max(probs, key=lambda k: probs[k])
            answers["purchase"] = {"choice": choice, "probabilities": probs, "confidence": probs[choice]}
        base = utility_decision(context)
        if "return_item" in questions:
            answers["return_item"] = {"noul": 0.9 if base.return_item else 0.1}
        if "resell_item" in questions:
            answers["resell_item"] = {"noul": 0.85 if base.resell else 0.15}
            level = RESELL_LEVELS.index(base.resell_price_pct) if base.resell_price_pct in RESELL_LEVELS else 2
            answers["resell_price"] = {"score": float(level)}
        return {"answers": answers}


# ---------------------------------------------------------------- buyer


def _answer(resp: dict, name: str, order: list[str]) -> dict | None:
    answers = resp.get("answers")
    if isinstance(answers, dict):
        return answers.get(name)
    if isinstance(answers, list) and name in order:  # positional form
        i = order.index(name)
        return answers[i] if i < len(answers) else None
    return None


def _probabilities(ans: dict) -> dict[str, float]:
    probs = ans.get("probabilities") or {}
    if isinstance(probs, list):  # [{"option": .., "probability": ..}] form
        probs = {p.get("option") or p.get("name"): p.get("probability") for p in probs}
    return {str(k): float(v) for k, v in probs.items() if v is not None}


class SystemOneBuyer:
    """Buyer decisions from a System 1 model, gated by confidence, falling back to utility."""

    def __init__(self, client: SystemOneClient, *, concurrency: int = 4, conf_min: float = 0.6,
                 margin_min: float = 0.15, cache: bool = True):
        self.client = client
        self.name = f"system1:{client.model}"
        self.conf_min = conf_min
        self.margin_min = margin_min
        self._sem = asyncio.Semaphore(concurrency)
        self._cache: dict[str, dict] | None = {} if cache else None
        self.calls = 0
        self.cache_hits = 0
        self.errors = 0

    async def decide_many(self, requests: list[BuyerRequest]) -> list[BuyerDecision]:
        return list(await asyncio.gather(*(self._decide(r) for r in requests)))

    async def _decide(self, req: BuyerRequest) -> BuyerDecision:
        state, questions, option_map = build_query(req)
        if not questions:
            return utility_decision(req)
        key = hashlib.sha256(json.dumps(
            [self.client.model, PROMPT_VERSION, state, questions], sort_keys=True).encode()).hexdigest()
        latency_ms = 0.0
        try:
            if self._cache is not None and key in self._cache:
                self.cache_hits += 1
                resp = self._cache[key]
            else:
                async with self._sem:
                    t0 = time.perf_counter()
                    resp = await self.client.ask(state, questions, context=req)
                    latency_ms = (time.perf_counter() - t0) * 1000
                self.calls += 1
                if self._cache is not None:
                    self._cache[key] = resp
        except Exception:  # network/HTTP/parse errors must never stall the sim
            self.errors += 1
            return utility_decision(req).model_copy(
                update={"source": "fallback", "reason": ReasonCode.S1_ERROR})
        return self._parse(req, resp, list(questions), option_map, latency_ms)

    def _parse(self, req: BuyerRequest, resp: dict, order: list[str], option_map: dict[str, str],
               latency_ms: float) -> BuyerDecision:
        fallback = utility_decision(req)
        purchase: str | None = None
        confidence: float | None = None
        probs: dict[str, float] | None = None
        reason = ReasonCode.S1_CHOICE
        if "purchase" in order:
            ans = _answer(resp, "purchase", order)
            choice = ans.get("choice") if isinstance(ans, dict) else None
            if choice != "none" and choice not in option_map:
                return fallback.model_copy(update={
                    "source": "fallback", "reason": ReasonCode.S1_INVALID, "latency_ms": latency_ms})
            probs = _probabilities(ans)
            ranked = sorted(probs.values(), reverse=True)
            confidence = float(ans.get("confidence", ranked[0] if ranked else 0.0))
            margin = ranked[0] - ranked[1] if len(ranked) > 1 else 1.0
            if confidence < self.conf_min or margin < self.margin_min:
                return fallback.model_copy(update={
                    "source": "fallback", "reason": ReasonCode.S1_LOW_CONFIDENCE, "confidence": confidence,
                    "probabilities": probs, "latency_ms": latency_ms})
            purchase = None if choice == "none" else option_map[choice]
        ret = _answer(resp, "return_item", order) if "return_item" in order else None
        res = _answer(resp, "resell_item", order) if "resell_item" in order else None
        price = _answer(resp, "resell_price", order) if "resell_price" in order else None
        level = int(round(float(price.get("score", 2)))) if isinstance(price, dict) else 2
        chosen = next((o for o in req.offers if o.offer_id == purchase), None)
        est = offer_utility(req, chosen) if chosen else 0.0
        return BuyerDecision(
            buyer_id=req.buyer_id,
            purchase=purchase,
            estimated_utility=max(-1.0, min(1.0, round(est, 6))),
            return_item=bool(ret and float(ret.get("noul", 0)) >= 0.5),
            resell=bool(res and float(res.get("noul", 0)) >= 0.5),
            resell_price_pct=RESELL_LEVELS[max(0, min(level, len(RESELL_LEVELS) - 1))],
            reason=reason,
            source="system1",
            confidence=confidence,
            probabilities=probs,
            latency_ms=latency_ms,
        )
