"""Example market: 200 agents (170 buyers, 10 sellers, 20 producers), 5 companies, 4 categories."""

from __future__ import annotations

import random

from acs.config import SimConfig
from acs.engine import World
from acs.ledger import Ledger
from acs.models import Buyer, Company, Producer, Product, Seller, Shock

CATEGORIES = {  # category -> (reference price cents, need growth per tick)
    "snack": (300, 0.12),
    "apparel": (4000, 0.03),
    "gadget": (15000, 0.015),
    "luxury": (60000, 0.005),
}

# company -> list of (category, price multiplier vs reference, quality mean, quality sd, tier)
COMPANIES = {
    "C1": ("Northwind Premium", [("gadget", 1.4, 0.85, 0.05, "premium"), ("luxury", 1.2, 0.9, 0.04, "premium")]),
    "C2": ("Budget Basics", [("snack", 0.8, 0.45, 0.15, "budget"), ("apparel", 0.7, 0.4, 0.15, "budget")]),
    "C3": ("Midline Goods", [("snack", 1.0, 0.65, 0.1, "all"), ("apparel", 1.0, 0.65, 0.1, "mid"),
                             ("gadget", 1.0, 0.6, 0.12, "mid")]),
    "C4": ("Value Tech", [("gadget", 0.75, 0.5, 0.15, "budget"), ("apparel", 1.2, 0.75, 0.08, "mid")]),
    "C5": ("Artisan Co", [("snack", 1.5, 0.85, 0.05, "premium"), ("luxury", 0.9, 0.7, 0.1, "premium")]),
}

TIERS = (("budget", 0.45), ("mid", 0.85), ("premium", 1.0))  # wealth quantile cut-offs


def build_world(config: SimConfig, n_buyers: int = 170, n_sellers: int = 10, n_producers: int = 20) -> World:
    rng = random.Random(f"{config.seed}:world")
    ledger = Ledger()
    companies: dict[str, Company] = {}
    products: dict[str, Product] = {}
    for cid, (name, lines) in COMPANIES.items():
        acct = f"acct:{cid}"
        ledger.open(acct, 500_000)  # $5,000 starting cash
        pids = []
        for cat, mult, qm, qsd, tier in lines:
            pid = f"{cid}-{cat}"
            price = int(CATEGORIES[cat][0] * mult)
            products[pid] = Product(pid, cid, cat, price, price, qm, qsd, tier)  # type: ignore[arg-type]
            pids.append(pid)
        companies[cid] = Company(cid, name, acct, pids)

    sellers = {}
    cids = list(companies)
    for i in range(n_sellers):
        sid = f"S{i + 1:02d}"
        sellers[sid] = Seller(sid, cids[i % len(cids)])

    producers = {}
    all_pids = list(products)
    for i in range(n_producers):
        pid = all_pids[i % len(all_pids)]
        p = products[pid]
        ref = CATEGORIES[p.category][0]
        capacity = max(1, round(6 * (300 / ref) ** 0.35))  # cheap goods are made in bulk
        wage = max(500, int(ref * 0.25))
        wid = f"W{i + 1:02d}"
        producers[wid] = Producer(wid, p.company_id, pid, capacity, round(rng.uniform(0.3, 0.9), 3), wage)

    wealth = sorted((rng.lognormvariate(0, 0.9) for _ in range(n_buyers)))
    median = wealth[len(wealth) // 2]
    order = list(range(n_buyers))
    rng.shuffle(order)
    buyers = {}
    for rank, i in enumerate(order):
        w = wealth[rank]
        q = rank / n_buyers
        tier = next(t for t, cut in TIERS if q < cut)
        bid = f"B{i + 1:03d}"
        acct = f"acct:{bid}"
        ledger.open(acct, int(20_000 * w))  # median buyer starts with about $200
        buyers[bid] = Buyer(
            id=bid, account=acct, wealth_tier=tier,  # type: ignore[arg-type]
            needs={c: round(rng.uniform(0, 0.6), 4) for c in CATEGORIES},
            need_rates={c: round(r * rng.uniform(0.5, 1.5), 5) for c, (_, r) in CATEGORIES.items()},
            prefs={c: round(rng.uniform(0.3, 1.0), 4) for c in CATEGORIES},
            price_sensitivity=round(min(2.0, 0.6 * (median / w) ** 0.5), 4),
            hype_sensitivity=round(rng.uniform(0.0, 0.6), 4),
            salary_mean=int(6_000 * w),
        )
    buyers = dict(sorted(buyers.items()))
    ref_prices = {c: ref for c, (ref, _) in CATEGORIES.items()}
    return World(companies, products, producers, sellers, buyers, ref_prices, ledger)


def default_shocks() -> list[Shock]:
    """A supply cut on Midline gadgets mid-run, to show sellers reacting to scarcity."""
    return [Shock(tick=50, kind="supply_cut", target="C3-gadget", value=0.2, duration=20)]
