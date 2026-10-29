import asyncio

import pytest

from acs.config import SimConfig
from acs.decisions.replay import DecisionRecord, ReplayBuyer, ReplaySeller, ReplayTriggers
from acs.decisions.system_one import FakeSystemOneClient, SystemOneBuyer, build_query
from acs.decisions.utility import UtilityBuyer, utility_decision
from acs.engine import Simulation
from acs.ledger import Ledger, LedgerError
from acs.models import BuyerRequest, Offer, SellerAction
from acs.runlog import RunLog
from acs.scenarios.example import build_world, default_shocks


def make_sim(seed=7, ticks=20, **kw) -> Simulation:
    cfg = SimConfig(seed=seed, ticks=ticks)
    return Simulation(build_world(cfg), cfg, shocks=default_shocks(), **kw)


def offer(oid, price, q=0.7, cat="snack") -> Offer:
    return Offer(offer_id=oid, market="primary", product_id=oid, company_id="C", category=cat, price=price,
                 ref_price=300, expected_quality=q, rating=None, rating_count=0, hype=0.0)


def request(offers, need=0.9) -> BuyerRequest:
    return BuyerRequest(buyer_id="B1", tick=0, cash=10_000, wealth_tier="mid", needs={"snack": need},
                        prefs={"snack": 1.0}, price_sensitivity=0.5, hype_sensitivity=0.0, offers=offers)


# ---------------------------------------------------------------- decisions

def test_utility_buys_best_surplus():
    d = utility_decision(request([offer("cheap_bad", 200, q=0.3), offer("good", 300, q=0.9)]))
    assert d.purchase == "good" and d.estimated_utility > 0


def test_utility_passes_when_nothing_is_worth_it():
    d = utility_decision(request([offer("pricey", 3000)], need=0.2))
    assert d.purchase is None and d.reason == "NO_POSITIVE_SURPLUS"


def test_system1_invalid_answer_falls_back_to_utility():
    class Bad(FakeSystemOneClient):
        async def ask(self, state, questions, *, context=None):
            return {"answers": {"purchase": {"choice": "offer_99", "probabilities": {}, "confidence": 1.0}}}

    req = request([offer("good", 300, q=0.9)])
    d = asyncio.run(SystemOneBuyer(Bad()).decide_many([req]))[0]
    assert d.source == "fallback" and d.reason == "S1_INVALID" and d.purchase == "good"


def test_system1_low_confidence_falls_back():
    class Unsure(FakeSystemOneClient):
        async def ask(self, state, questions, *, context=None):
            return {"answers": {"purchase": {"choice": "none", "probabilities": {"offer_1": 0.45, "none": 0.55},
                                             "confidence": 0.55}}}

    d = asyncio.run(SystemOneBuyer(Unsure()).decide_many([request([offer("good", 300, q=0.9)])]))[0]
    assert d.source == "fallback" and d.reason == "S1_LOW_CONFIDENCE" and d.probabilities


def test_query_has_typed_questions():
    state, questions, option_map = build_query(request([offer("a", 300), offer("b", 250)]))
    assert questions["purchase"]["type"] == "choice"
    assert set(questions["purchase"]["criteria"]) == {"offer_1", "offer_2", "none"}
    assert option_map == {"offer_1": "a", "offer_2": "b"}


# ---------------------------------------------------------------- ledger / transactions

def test_ledger_atomic_and_conserved():
    led = Ledger()
    led.open("a", 100)
    led.open("b", 0)
    with pytest.raises(LedgerError, match="INSUFFICIENT_FUNDS"):
        led.transfer("a", "b", 101, tick=0, kind="t", ref="x")
    assert led.balances == {"a": 100, "b": 0}
    led.transfer("a", "b", 40, tick=0, kind="t", ref="x")
    led.mint("b", 10, tick=0, kind="salary", ref="y")
    led.sink("a", 5, tick=0, kind="wage", ref="z")
    led.check()
    assert led.total() == 100 + 10 - 5


def _stocked(sim: Simulation):
    sim.run(3)
    return next(p for p in sim.w.products.values() if p.stock)


def test_failed_transactions_leave_state_unchanged():
    sim = make_sim()
    p = _stocked(sim)
    b = next(iter(sim.w.buyers.values()))
    before = (dict(sim.w.ledger.balances), list(p.stock), list(b.owned))

    assert sim.settle_purchase(b.id, p.id, p.price + 1).code == "STALE_PRICE"
    sim.w.ledger.balances[b.account] = p.price - 1
    sim.w.ledger.initial -= before[0][b.account] - (p.price - 1)  # keep the conservation check honest
    assert sim.settle_purchase(b.id, p.id, p.price).code == "INSUFFICIENT_FUNDS"
    assert (list(p.stock), list(b.owned)) == before[1:]
    sim.check_invariants()


def test_out_of_stock_and_double_sale_of_listing():
    sim = make_sim()
    p = _stocked(sim)
    buyers = list(sim.w.buyers.values())
    rich = [b for b in buyers if sim.w.ledger.balance(b.account) > p.price * 2][:2]
    p.stock[:] = p.stock[:1]
    first = sim.settle_purchase(rich[0].id, p.id, p.price)
    assert first.ok
    assert sim.settle_purchase(rich[1].id, p.id, p.price).code == "OUT_OF_STOCK"
    unit = rich[0].owned[-1]
    lst = sim.create_listing(rich[0].id, unit, 100)
    assert lst.ok
    assert sim.settle_purchase(rich[0].id, lst.ref, 100).code == "OWN_LISTING"
    assert sim.settle_purchase(rich[1].id, lst.ref, 100).ok
    assert sim.settle_purchase(rich[1].id, lst.ref, 100).code == "LISTING_INACTIVE"


def test_return_window_and_policy():
    sim = make_sim()
    p = _stocked(sim)
    b = next(b for b in sim.w.buyers.values() if sim.w.ledger.balance(b.account) > p.price)
    pur = sim.settle_purchase(b.id, p.id, p.price).ref
    sim.w.companies[p.company_id].refund_pct = 0
    assert sim.settle_return(b.id, pur).code == "NO_REFUND_POLICY"
    sim.w.companies[p.company_id].refund_pct = 50
    cash = sim.w.ledger.balance(b.account)
    assert sim.settle_return(b.id, pur).ok
    assert sim.w.ledger.balance(b.account) == cash + p.price * 50 // 100
    assert sim.settle_return(b.id, pur).code == "ALREADY_REFUNDED"
    sim.check_invariants()


def test_seller_action_is_clamped():
    sim = make_sim()
    p = next(iter(sim.w.products.values()))
    sim.apply_seller_action(SellerAction(company_id=p.company_id, price_changes={p.id: 5.0}, marketing_spend=10**12))
    assert p.price == int(round(p.base_price * 1.2))
    c = sim.w.companies[p.company_id]
    assert c.marketing_spend <= sim.w.ledger.balance(c.account) // 10


# ---------------------------------------------------------------- reproducibility

def test_same_seed_same_events_different_seed_differs():
    a, b, c = (make_sim(seed=s, log=RunLog()) for s in (1, 1, 2))
    for s in (a, b, c):
        s.run(20)
    assert a.log.events == b.log.events
    assert a.log.events != c.log.events


def test_replay_reproduces_fake_system1_run(tmp_path):
    log = RunLog(tmp_path)
    sim = make_sim(buyer_model=SystemOneBuyer(FakeSystemOneClient()), log=log)
    summary = sim.run(20)
    log.write({"sim": sim.cfg.model_dump()}, summary)
    assert summary["decision_sources"].get("system1", 0) > 0

    record = DecisionRecord(tmp_path / "decisions.jsonl")
    replay = make_sim(buyer_model=ReplayBuyer(record), trigger_detector=ReplayTriggers(record),
                      seller_model=ReplaySeller(record), log=RunLog())
    replay.run(20)
    assert replay.log.events == log.events


def test_supply_shock_applies_and_is_logged():
    sim = make_sim(ticks=60)
    sim.run(51)
    assert sim.w.products["C3-gadget"].supply_factor == 0.2
    assert any('"type":"shock"' in e for e in sim.log.events)


def test_utility_and_fake_s1_share_interface():
    reqs = make_sim().build_requests()
    for model in (UtilityBuyer(), SystemOneBuyer(FakeSystemOneClient())):
        out = asyncio.run(model.decide_many(reqs))
        assert [d.buyer_id for d in out] == [r.buyer_id for r in reqs]
