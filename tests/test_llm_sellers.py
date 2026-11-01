"""LLM sellers and positioning, offline: Pydantic AI's FunctionModel stands in for the provider."""

import json

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.messages import ModelResponse, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402

from acs.config import SimConfig  # noqa: E402
from acs.decisions.llm import LLMSeller, missing_api_key  # noqa: E402
from acs.decisions.replay import DecisionRecord, ReplayBuyer, ReplayPositioning, ReplaySeller, ReplayTriggers  # noqa: E402
from acs.engine import Simulation  # noqa: E402
from acs.runlog import RunLog  # noqa: E402
from acs.scenarios.example import build_world, default_shocks  # noqa: E402


def scripted(messages, info):
    """Position everything premium (out-of-range values on purpose); cut prices when triggered."""
    prompt = json.loads(messages[-1].parts[-1].content)
    tool = info.output_tools[0].name
    if "dashboard" in prompt:
        args = {"price_changes": {p["product_id"]: -0.9 for p in prompt["dashboard"]["products"]},
                "marketing_spend": 10**12, "reason": "clear stock"}
    else:
        args = {"products": {p["product_id"]: {"price_mult": 9.0, "quality": 0.9} for p in prompt["products"]},
                "refund_pct": 50, "marketing_spend": 1000, "statement": "premium everything"}
    return ModelResponse(parts=[ToolCallPart(tool, args)])


def failing(messages, info):
    raise RuntimeError("provider down")


def make_sim(model, ticks=12, log=None):
    cfg = SimConfig(seed=3, ticks=ticks)
    llm = LLMSeller(model)
    return Simulation(build_world(cfg), cfg, seller_model=llm, positioning_model=llm, shocks=default_shocks(),
                      log=log or RunLog()), llm


def test_positioning_is_clamped_and_raises_wages():
    sim, llm = make_sim(FunctionModel(scripted))
    wage_before = sim.w.producers["W01"].wage
    product = sim.w.products[sim.w.producers["W01"].product_id]
    q_before = product.quality_mean
    sim.run(1)
    ref = sim.w.ref_prices[product.category]
    assert product.price == product.base_price == ref * 2  # 9x clamped to the 2x band
    assert product.quality_mean == 0.9
    assert sim.w.producers["W01"].wage == round(wage_before * (0.5 + 0.9) / (0.5 + q_before))
    assert all(c.refund_pct == 50 for c in sim.w.companies.values())
    assert llm.calls == 5 and llm.errors == 0


def test_llm_seller_actions_are_clamped():
    sim, llm = make_sim(FunctionModel(scripted))
    sim.run(6)  # positioning at 0, seller phase at 5
    sellers = [json.loads(d) for d in sim.log.decisions if '"kind":"seller"' in d]
    assert sellers and all(s["data"]["source"] == "llm" for s in sellers)
    for pid in sim.w.companies[sellers[0]["data"]["company_id"]].product_ids:
        p = sim.w.products[pid]
        assert p.price >= int(p.base_price * 0.8) - 1  # -90% clamped to one -20% step
    sim.check_invariants()


def test_provider_failure_falls_back_and_run_continues():
    sim, llm = make_sim(FunctionModel(failing))
    prices = {p.id: p.price for p in sim.w.products.values()}
    sim.run(11)
    assert llm.errors == llm.calls > 5
    positions = [json.loads(d)["data"] for d in sim.log.decisions if '"kind":"positioning"' in d]
    assert all(p["source"] == "fallback" for p in positions)
    assert {p.id: p.base_price for p in sim.w.products.values()} == prices  # scenario line kept


def test_llm_run_replays_identically(tmp_path):
    log = RunLog(tmp_path)
    sim, _ = make_sim(FunctionModel(scripted), log=log)
    summary = sim.run(12)
    log.write({}, summary)
    record = DecisionRecord(tmp_path / "decisions.jsonl")
    cfg = SimConfig(seed=3, ticks=12)
    replay = Simulation(build_world(cfg), cfg, buyer_model=ReplayBuyer(record), trigger_detector=ReplayTriggers(record),
                        seller_model=ReplaySeller(record), positioning_model=ReplayPositioning(record),
                        shocks=default_shocks(), log=RunLog())
    replay.run(12)
    assert replay.log.events == log.events


def test_missing_api_key_is_detected(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert missing_api_key("openai:gpt-5-mini") == "OPENAI_API_KEY"
    assert missing_api_key("anthropic:claude-haiku-5-5") is None
