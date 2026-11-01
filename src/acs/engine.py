"""Deterministic tick-based simulation engine.

The engine owns every piece of economic state. Decision makers receive
read-only requests and return decisions; the engine validates and settles them
atomically (all checks first, then all mutations) and never asks a model to
validate or execute a transaction.

Tick order:
  [tick 0 only: positioning] -> shocks -> income -> production -> marketing -> seller phase (every K ticks)
  -> needs grow -> defects reveal + ratings -> listings expire
  -> buyer decide phase (all at once, against one snapshot)
  -> settle phase (seeded order, against live state) -> hype -> invariants
"""

from __future__ import annotations

import asyncio
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from acs.config import SimConfig
from acs.decisions.base import BuyerModel, PositioningModel, SellerModel, TriggerDetector
from acs.decisions.utility import RuleSeller, RuleTriggerDetector, UtilityBuyer, desire, offer_utility
from acs.ledger import Ledger
from acs.models import (
    Buyer,
    BuyerDecision,
    BuyerRequest,
    Company,
    CompanyBrief,
    CompanyDashboard,
    CompetitorProduct,
    Listing,
    Offer,
    PositioningPlan,
    Producer,
    Product,
    ProductBrief,
    ProductStats,
    Purchase,
    ResellCandidate,
    ReturnCandidate,
    SellerAction,
    Seller,
    Shock,
    Unit,
)
from acs.runlog import RunLog


@dataclass(frozen=True, slots=True)
class TxnResult:
    ok: bool
    code: str = "OK"
    ref: str = ""


@dataclass
class World:
    companies: dict[str, Company]
    products: dict[str, Product]
    producers: dict[str, Producer]
    sellers: dict[str, Seller]
    buyers: dict[str, Buyer]
    ref_prices: dict[str, int]  # category -> reference price (cents)
    ledger: Ledger
    units: dict[str, Unit] = field(default_factory=dict)
    purchases: dict[str, Purchase] = field(default_factory=dict)
    listings: dict[str, Listing] = field(default_factory=dict)


class Simulation:
    def __init__(
        self,
        world: World,
        config: SimConfig,
        *,
        buyer_model: BuyerModel | None = None,
        trigger_detector: TriggerDetector | None = None,
        seller_model: SellerModel | None = None,
        positioning_model: PositioningModel | None = None,
        shocks: list[Shock] | None = None,
        log: RunLog | None = None,
    ):
        self.w = world
        self.cfg = config
        self.buyer_model = buyer_model or UtilityBuyer()
        self.triggers = trigger_detector or RuleTriggerDetector()
        self.seller_model = seller_model or RuleSeller()
        self.positioning_model = positioning_model  # None keeps the scenario's product line as built
        self.log = log or RunLog()
        self.tick = 0
        # one RNG per subsystem so adding randomness in one place does not shift another
        self.rng_income = random.Random(f"{config.seed}:income")
        self.rng_prod = random.Random(f"{config.seed}:production")
        self.rng_order = random.Random(f"{config.seed}:order")
        self.pending_shocks: list[Shock] = sorted(shocks or [], key=lambda s: s.tick)
        self.active_shocks: list[tuple[int, Shock]] = []  # (end tick, shock)
        self.salary_factor = 1.0
        self.last_shock: dict[str, int] = {}  # company id -> tick
        self.sales_hist: dict[str, list[int]] = defaultdict(list)  # product -> per-tick sales
        self.return_hist: dict[str, list[int]] = defaultdict(list)
        self.rating_snap: dict[str, float | None] = {}
        self.unit_purchase: dict[str, str] = {}  # unit -> latest purchase id
        self.listed_units: dict[str, str] = {}  # unit -> active listing id
        self._seq: Counter[str] = Counter()
        self.produced = 0
        self.destroyed = 0
        self.totals: Counter[str] = Counter()
        self.failed: Counter[str] = Counter()
        self.decision_sources: Counter[str] = Counter()
        self.buyer_utility = 0.0
        self.revenue: Counter[str] = Counter()  # company -> cents
        self.costs: Counter[str] = Counter()  # company -> wages + marketing + refunds

    # ------------------------------------------------------------ helpers

    def _id(self, prefix: str) -> str:
        self._seq[prefix] += 1
        return f"{prefix}{self._seq[prefix]:06d}"

    def expected_quality(self, p: Product) -> float:
        n = p.rating_count
        if not n:
            return self.cfg.quality_prior
        return (p.rating_sum / 5 + self.cfg.quality_prior * self.cfg.prior_weight) / (n + self.cfg.prior_weight)

    def _fail(self, code: str, ref: str, **data) -> TxnResult:
        self.failed[code] += 1
        self.log.event(self.tick, "txn_failed", code=code, ref=ref, **data)
        return TxnResult(False, code, ref)

    # ------------------------------------------------------------ settlement (public for tests/admin)

    def settle_purchase(self, buyer_id: str, offer_id: str, expected_price: int) -> TxnResult:
        """Atomic: validate everything, then move money and the unit together."""
        b = self.w.buyers.get(buyer_id)
        if b is None:
            return self._fail("UNKNOWN_BUYER", buyer_id)
        if offer_id in self.w.products:
            p = self.w.products[offer_id]
            if p.price != expected_price:
                return self._fail("STALE_PRICE", offer_id, buyer=buyer_id)
            if not p.stock:
                return self._fail("OUT_OF_STOCK", offer_id, buyer=buyer_id)
            if not self.w.ledger.can_pay(b.account, p.price):
                return self._fail("INSUFFICIENT_FUNDS", offer_id, buyer=buyer_id)
            company = self.w.companies[p.company_id]
            unit_id = p.stock.pop(0)
            self.w.ledger.transfer(b.account, company.account, p.price, tick=self.tick, kind="sale", ref=unit_id)
            self.revenue[company.id] += p.price
            return self._deliver(b, unit_id, company.account, p.price, "primary")
        lst = self.w.listings.get(offer_id)
        if lst is None:
            return self._fail("UNKNOWN_OFFER", offer_id, buyer=buyer_id)
        if not lst.active:
            return self._fail("LISTING_INACTIVE", offer_id, buyer=buyer_id)
        if lst.seller_id == buyer_id:
            return self._fail("OWN_LISTING", offer_id, buyer=buyer_id)
        if lst.price != expected_price:
            return self._fail("STALE_PRICE", offer_id, buyer=buyer_id)
        unit = self.w.units[lst.unit_id]
        seller = self.w.buyers[lst.seller_id]
        if unit.owner != seller.id or unit.destroyed:
            return self._fail("SELLER_NOT_OWNER", offer_id, buyer=buyer_id)
        if not self.w.ledger.can_pay(b.account, lst.price):
            return self._fail("INSUFFICIENT_FUNDS", offer_id, buyer=buyer_id)
        self.w.ledger.transfer(b.account, seller.account, lst.price, tick=self.tick, kind="resale", ref=unit.id)
        lst.active = False
        del self.listed_units[unit.id]
        seller.owned.remove(unit.id)
        return self._deliver(b, unit.id, seller.account, lst.price, "secondary")

    def _deliver(self, b: Buyer, unit_id: str, payee: str, price: int, market: str) -> TxnResult:
        unit = self.w.units[unit_id]
        unit.owner = b.id
        b.owned.append(unit_id)
        product = self.w.products[unit.product_id]
        product.sales_tick += 1
        b.needs[product.category] = max(0.0, b.needs[product.category] - 0.7)
        pid = self._id("T")
        self.w.purchases[pid] = Purchase(pid, unit_id, b.id, payee, price, self.tick, market)  # type: ignore[arg-type]
        self.unit_purchase[unit_id] = pid
        self.totals[f"sales_{market}"] += 1
        self.log.event(self.tick, "txn_ok", market=market, purchase=pid, buyer=b.id, unit=unit_id,
                       product=product.id, price=price)
        return TxnResult(True, "OK", pid)

    def settle_return(self, buyer_id: str, purchase_id: str) -> TxnResult:
        pur = self.w.purchases.get(purchase_id)
        if pur is None or pur.buyer_id != buyer_id:
            return self._fail("UNKNOWN_PURCHASE", purchase_id, buyer=buyer_id)
        if pur.market != "primary":
            return self._fail("NOT_RETURNABLE", purchase_id, buyer=buyer_id)
        if pur.refunded:
            return self._fail("ALREADY_REFUNDED", purchase_id, buyer=buyer_id)
        if self.tick - pur.tick > self.cfg.return_window:
            return self._fail("RETURN_WINDOW_CLOSED", purchase_id, buyer=buyer_id)
        unit = self.w.units[pur.unit_id]
        if unit.owner != buyer_id or unit.id in self.listed_units or self.unit_purchase.get(unit.id) != purchase_id:
            return self._fail("NOT_HELD", purchase_id, buyer=buyer_id)
        product = self.w.products[unit.product_id]
        company = self.w.companies[product.company_id]
        refund = pur.price * company.refund_pct // 100
        if refund <= 0:
            return self._fail("NO_REFUND_POLICY", purchase_id, buyer=buyer_id)
        if not self.w.ledger.can_pay(company.account, refund):
            return self._fail("INSUFFICIENT_FUNDS", purchase_id, buyer=buyer_id)
        b = self.w.buyers[buyer_id]
        self.w.ledger.transfer(company.account, b.account, refund, tick=self.tick, kind="refund", ref=purchase_id)
        pur.refunded = True
        unit.owner = None
        unit.destroyed = True
        b.owned.remove(unit.id)
        self.destroyed += 1
        self.costs[company.id] += refund
        self.return_hist[product.id][-1] += 1
        self.totals["returns"] += 1
        self.totals["refunds_cents"] += refund
        self.log.event(self.tick, "return", purchase=purchase_id, buyer=buyer_id, refund=refund, product=product.id)
        return TxnResult(True, "OK", purchase_id)

    def create_listing(self, buyer_id: str, unit_id: str, price: int) -> TxnResult:
        unit = self.w.units.get(unit_id)
        if unit is None or unit.owner != buyer_id or unit.destroyed:
            return self._fail("NOT_OWNER", unit_id, buyer=buyer_id)
        if unit_id in self.listed_units:
            return self._fail("ALREADY_LISTED", unit_id, buyer=buyer_id)
        if price <= 0:
            return self._fail("BAD_PRICE", unit_id, buyer=buyer_id)
        lid = self._id("L")
        self.w.listings[lid] = Listing(lid, unit_id, buyer_id, price, self.tick)
        self.listed_units[unit_id] = lid
        self.totals["listings"] += 1
        self.log.event(self.tick, "listing", listing=lid, seller=buyer_id, unit=unit_id, price=price)
        return TxnResult(True, "OK", lid)

    # ------------------------------------------------------------ admin

    def submit_shock(self, shock: Shock) -> None:
        """Queue an intervention; it applies at the start of ``shock.tick`` (>= current tick)."""
        if shock.tick < self.tick:
            shock = shock.model_copy(update={"tick": self.tick})
        self.pending_shocks.append(shock)
        self.pending_shocks.sort(key=lambda s: s.tick)

    def _shock_targets(self, target: str | None) -> list[Product]:
        if target is None:
            return list(self.w.products.values())
        if target in self.w.products:
            return [self.w.products[target]]
        if target in self.w.companies:
            return [self.w.products[p] for p in self.w.companies[target].product_ids]
        return []

    def _apply_shocks(self) -> None:
        t = self.tick
        for end, s in [a for a in self.active_shocks if a[0] == t]:
            self._set_shock(s, revert=True)
            self.active_shocks.remove((end, s))
            self.log.event(t, "shock_end", kind=s.kind, target=s.target)
        while self.pending_shocks and self.pending_shocks[0].tick <= t:
            s = self.pending_shocks.pop(0)
            self._set_shock(s, revert=False)
            if s.duration > 0 and s.kind in ("supply_cut", "defect_spike", "salary"):
                self.active_shocks.append((t + s.duration, s))
            self.log.shock(s.model_dump())
            self.log.event(t, "shock", **s.model_dump(exclude={"tick"}))

    def _set_shock(self, s: Shock, *, revert: bool) -> None:
        products = self._shock_targets(s.target)
        for p in products:
            self.last_shock[p.company_id] = self.tick
        if s.kind == "supply_cut":
            for p in products:
                p.supply_factor = 1.0 if revert else s.value
        elif s.kind == "defect_spike":
            for p in products:
                p.defect_extra = 0.0 if revert else s.value
        elif s.kind == "salary":
            self.salary_factor = 1.0 if revert else s.value
        elif s.kind == "hype" and not revert:
            for p in products:
                p.hype = min(1.0, max(0.0, p.hype + s.value))
        elif s.kind == "cash" and not revert and s.target in self.w.companies:
            acct = self.w.companies[s.target].account
            amount = int(round(abs(s.value) * 100))
            if s.value > 0:
                self.w.ledger.mint(acct, amount, tick=self.tick, kind="admin_cash", ref=s.target)
            elif amount:
                amount = min(amount, self.w.ledger.balance(acct))
                if amount:
                    self.w.ledger.sink(acct, amount, tick=self.tick, kind="admin_cash", ref=s.target)

    # ------------------------------------------------------------ phases

    def _income(self) -> None:
        paid = 0
        for b in self.w.buyers.values():
            if self.rng_income.random() < self.cfg.salary_prob:
                amount = int(b.salary_mean * self.salary_factor * self.rng_income.uniform(0.5, 1.5))
                if amount > 0:
                    self.w.ledger.mint(b.account, amount, tick=self.tick, kind="salary", ref=b.id)
                    paid += amount
        self.totals["salary_cents"] += paid

    def _produce(self) -> None:
        made: Counter[str] = Counter()
        for pr in self.w.producers.values():
            p = self.w.products[pr.product_id]
            if len(p.stock) >= p.max_stock:
                continue  # idle: storage full, no wage owed this tick
            company = self.w.companies[pr.company_id]
            if not self.w.ledger.can_pay(company.account, pr.wage):
                self._fail("WAGE_UNPAID", pr.id, company=company.id)
                continue
            self.w.ledger.sink(company.account, pr.wage, tick=self.tick, kind="wage", ref=pr.id)
            self.costs[company.id] += pr.wage
            self.totals["wages_cents"] += pr.wage
            expected = pr.capacity * p.supply_factor
            n = int(expected) + (1 if self.rng_prod.random() < expected - int(expected) else 0)
            for _ in range(min(n, p.max_stock - len(p.stock))):
                q = min(0.99, max(0.01, self.rng_prod.gauss(p.quality_mean, p.quality_sd * (1.5 - pr.skill))))
                defective = self.rng_prod.random() < self.cfg.defect_base * (1 - q) ** 2 + p.defect_extra
                uid = self._id("U")
                self.w.units[uid] = Unit(uid, p.id, round(q, 4), defective, self.tick)
                p.stock.append(uid)
                made[p.id] += 1
                self.produced += 1
        if made:
            self.log.event(self.tick, "production", units=dict(sorted(made.items())))

    def _marketing(self) -> dict[str, float]:
        boost: dict[str, float] = {}
        for c in self.w.companies.values():
            spend = min(c.marketing_spend, self.w.ledger.balance(c.account))
            if spend <= 0:
                continue
            self.w.ledger.sink(c.account, spend, tick=self.tick, kind="marketing", ref=c.id)
            self.costs[c.id] += spend
            self.totals["marketing_cents"] += spend
            per = self.cfg.hype_per_marketing_dollar * spend / 100 / len(c.product_ids)
            for pid in c.product_ids:
                boost[pid] = boost.get(pid, 0.0) + per
        return boost

    def dashboard(self, c: Company) -> CompanyDashboard:
        k = self.cfg.seller_trigger_every
        stats = []
        for pid in c.product_ids:
            p = self.w.products[pid]
            rivals = [o.price for o in self.w.products.values()
                      if o.category == p.category and o.company_id != c.id and o.stock]
            stats.append(ProductStats(
                product_id=pid, category=p.category, price=p.price, base_price=p.base_price,
                stock=len(p.stock), sales_window=sum(self.sales_hist[pid][-k:]),
                returns_window=sum(self.return_hist[pid][-k:]),
                rating=round(p.rating, 4) if p.rating is not None else None,
                rating_prev=self.rating_snap.get(pid), hype=round(p.hype, 4),
                competitor_min_price=min(rivals) if rivals else None,
            ))
        return CompanyDashboard(
            company_id=c.id, tick=self.tick, cash=self.w.ledger.balance(c.account), refund_pct=c.refund_pct,
            marketing_spend=c.marketing_spend, products=stats,
            shocked=self.tick - self.last_shock.get(c.id, -10**9) <= k,
        )

    async def _seller_phase(self) -> None:
        dashboards = [self.dashboard(c) for c in self.w.companies.values()]
        reports = await self.triggers.detect_many(dashboards)
        for r in reports:
            self.log.decision(self.tick, "trigger", r.model_dump())
        triggered = [(d, r) for d, r in zip(dashboards, reports, strict=True) if r.fired]
        if triggered:
            actions = await self.seller_model.decide_many([d for d, _ in triggered], [r for _, r in triggered])
            for a in actions:
                self.log.decision(self.tick, "seller", a.model_dump())
                self.apply_seller_action(a)
        for p in self.w.products.values():
            self.rating_snap[p.id] = round(p.rating, 4) if p.rating is not None else None

    def apply_seller_action(self, a: SellerAction) -> dict:
        """Clamp a (possibly model-generated) action to engine bounds, then apply it."""
        c = self.w.companies.get(a.company_id)
        if c is None:
            return {}
        applied: dict = {"prices": {}}
        lo, hi = self.cfg.price_band
        step = self.cfg.max_price_step
        for pid, frac in sorted(a.price_changes.items()):
            if pid not in c.product_ids:
                continue
            p = self.w.products[pid]
            frac = max(-step, min(step, frac))
            new = int(round(p.price * (1 + frac)))
            new = max(int(p.base_price * lo), min(int(p.base_price * hi), new), 1)
            if new != p.price:
                applied["prices"][pid] = [p.price, new]
                p.price = new
        if a.refund_pct is not None and a.refund_pct != c.refund_pct:
            applied["refund_pct"] = [c.refund_pct, a.refund_pct]
            c.refund_pct = a.refund_pct
        if a.marketing_spend is not None:
            spend = max(0, min(a.marketing_spend, self.w.ledger.balance(c.account) // 10))
            if spend != c.marketing_spend:
                applied["marketing_spend"] = [c.marketing_spend, spend]
                c.marketing_spend = spend
        self.log.event(self.tick, "seller_action", company=c.id, source=a.source, reason=a.reason, applied=applied)
        return applied

    def company_brief(self, c: Company) -> CompanyBrief:
        products = []
        for pid in c.product_ids:
            p = self.w.products[pid]
            makers = [pr for pr in self.w.producers.values() if pr.product_id == pid]
            rates = [b.need_rates.get(p.category, 0.0) for b in self.w.buyers.values()]
            rivals = [CompetitorProduct(company_id=o.company_id, company_name=self.w.companies[o.company_id].name,
                                        price=o.price)
                      for o in self.w.products.values() if o.category == p.category and o.company_id != c.id]
            products.append(ProductBrief(
                product_id=pid, category=p.category, ref_price=self.w.ref_prices[p.category], price=p.price,
                quality_mean=p.quality_mean, capacity_per_tick=sum(pr.capacity for pr in makers),
                wage_per_tick=sum(pr.wage for pr in makers),
                need_growth=round(sum(rates) / max(len(rates), 1), 5), competitors=rivals,
            ))
        cash = sorted(self.w.ledger.balance(b.account) for b in self.w.buyers.values())
        return CompanyBrief(
            company_id=c.id, name=c.name, cash=self.w.ledger.balance(c.account), products=products,
            buyers_by_tier=dict(sorted(Counter(b.wealth_tier for b in self.w.buyers.values()).items())),
            median_buyer_cash=cash[len(cash) // 2] if cash else 0,
            price_mult_range=self.cfg.position_price_mult, quality_range=self.cfg.position_quality,
            quality_wage_base=self.cfg.quality_wage_base,
        )

    async def _positioning_phase(self) -> None:
        briefs = [self.company_brief(c) for c in self.w.companies.values()]
        plans = await self.positioning_model.position_many(briefs)
        for plan in plans:
            self.log.decision(self.tick, "positioning", plan.model_dump())
            self.apply_positioning(plan)

    def apply_positioning(self, plan: PositioningPlan) -> dict:
        """Clamp a positioning plan, then reset price and quality; higher quality costs more in wages."""
        c = self.w.companies.get(plan.company_id)
        if c is None:
            return {}
        lo, hi = self.cfg.position_price_mult
        qlo, qhi = self.cfg.position_quality
        base = self.cfg.quality_wage_base
        applied: dict = {"products": {}}
        for pid, pos in sorted(plan.products.items()):
            if pid not in c.product_ids:
                continue
            p = self.w.products[pid]
            ref = self.w.ref_prices[p.category]
            price = max(1, int(round(ref * max(lo, min(hi, pos.price_mult)))))
            quality = round(max(qlo, min(qhi, pos.quality)), 4)
            for pr in self.w.producers.values():
                if pr.product_id == pid:
                    pr.wage = max(1, int(round(pr.wage * (base + quality) / (base + p.quality_mean))))
            applied["products"][pid] = {"price": [p.price, price], "quality": [p.quality_mean, quality]}
            p.price = p.base_price = price
            p.quality_mean = quality
        if plan.refund_pct is not None:
            applied["refund_pct"] = [c.refund_pct, plan.refund_pct]
            c.refund_pct = plan.refund_pct
        if plan.marketing_spend is not None:
            spend = max(0, min(plan.marketing_spend, self.w.ledger.balance(c.account) // 10))
            applied["marketing_spend"] = [c.marketing_spend, spend]
            c.marketing_spend = spend
        self.log.event(self.tick, "positioning", company=c.id, source=plan.source, statement=plan.statement,
                       applied=applied)
        return applied

    def _grow_needs(self) -> None:
        for b in self.w.buyers.values():
            for cat, rate in b.need_rates.items():
                b.needs[cat] = min(1.0, b.needs[cat] + rate)

    def _reveal_and_rate(self) -> None:
        due = self.tick - self.cfg.reveal_after
        for pur in self.w.purchases.values():
            if pur.rated or pur.tick != due:
                continue
            pur.rated = True
            unit = self.w.units[pur.unit_id]
            unit.revealed = True
            p = self.w.products[unit.product_id]
            over = max(0.0, pur.price / self.w.ref_prices[p.category] - 1.0)
            rating = 1 + 4 * unit.quality - (2.5 if unit.defective else 0.0) - 0.5 * over
            rating = round(max(1.0, min(5.0, rating)), 3)
            p.rating_sum += rating
            p.rating_count += 1
            self.log.event(self.tick, "rating", product=p.id, purchase=pur.id, rating=rating,
                           defective=unit.defective)

    def _expire_listings(self) -> None:
        for lst in self.w.listings.values():
            if lst.active and self.tick - lst.tick >= self.cfg.listing_ttl:
                lst.active = False
                self.listed_units.pop(lst.unit_id, None)
                self.log.event(self.tick, "listing_expired", listing=lst.id)

    def build_requests(self) -> list[BuyerRequest]:
        cfg = self.cfg
        primary = [p for p in self.w.products.values() if p.stock]
        listings = [lst for lst in self.w.listings.values() if lst.active]
        requests = []
        for b in self.w.buyers.values():
            cash = self.w.ledger.balance(b.account)
            req = BuyerRequest(
                buyer_id=b.id, tick=self.tick, cash=cash, wealth_tier=b.wealth_tier,
                needs={k: round(v, 4) for k, v in b.needs.items()}, prefs=b.prefs,
                price_sensitivity=b.price_sensitivity, hype_sensitivity=b.hype_sensitivity, offers=[],
            )
            candidates: list[tuple[float, str, Offer]] = []
            for p in primary:
                if p.price > cash:
                    continue
                o = Offer(offer_id=p.id, market="primary", product_id=p.id, company_id=p.company_id,
                          category=p.category, price=p.price, ref_price=self.w.ref_prices[p.category],
                          expected_quality=round(self.expected_quality(p), 4),
                          rating=round(p.rating, 3) if p.rating is not None else None,
                          rating_count=p.rating_count, hype=round(p.hype, 4))
                if desire(req, o) >= cfg.desire_threshold:
                    candidates.append((offer_utility(req, o), o.offer_id, o))
            for lst in listings:
                if lst.seller_id == b.id or lst.price > cash:
                    continue
                unit = self.w.units[lst.unit_id]
                p = self.w.products[unit.product_id]
                o = Offer(offer_id=lst.id, market="secondary", product_id=p.id, company_id=p.company_id,
                          category=p.category, price=lst.price, ref_price=self.w.ref_prices[p.category],
                          expected_quality=round(self.expected_quality(p), 4),
                          rating=round(p.rating, 3) if p.rating is not None else None,
                          rating_count=p.rating_count, hype=round(p.hype, 4),
                          defect_known=unit.revealed and unit.defective)
                if desire(req, o) >= cfg.desire_threshold:
                    candidates.append((offer_utility(req, o), o.offer_id, o))
            candidates.sort(key=lambda c: (-c[0], c[1]))
            req.offers = [o for _, _, o in candidates[: cfg.shortlist_k]]
            req.returnable = self._returnable(b)
            req.resellable = self._resellable(b)
            if req.offers or req.returnable or req.resellable:
                requests.append(req)
        return requests

    def _returnable(self, b: Buyer) -> ReturnCandidate | None:
        for uid in reversed(b.owned):
            pid = self.unit_purchase.get(uid)
            pur = self.w.purchases[pid] if pid else None
            unit = self.w.units[uid]
            if (pur and pur.market == "primary" and not pur.refunded and unit.revealed and unit.defective
                    and uid not in self.listed_units and self.tick - pur.tick <= self.cfg.return_window):
                p = self.w.products[unit.product_id]
                return ReturnCandidate(
                    purchase_id=pur.id, unit_id=uid, category=p.category, price_paid=pur.price,
                    refund_pct=self.w.companies[p.company_id].refund_pct, defective=True,
                    ticks_left=self.cfg.return_window - (self.tick - pur.tick))
        return None

    def _resellable(self, b: Buyer) -> ResellCandidate | None:
        for uid in b.owned:
            if uid in self.listed_units:
                continue
            pur = self.w.purchases[self.unit_purchase[uid]]
            if self.tick - pur.tick < self.cfg.resell_min_hold:
                continue
            unit = self.w.units[uid]
            p = self.w.products[unit.product_id]
            return ResellCandidate(unit_id=uid, product_id=p.id, category=p.category, price_paid=pur.price,
                                   reference_price=p.price, defect_known=unit.revealed and unit.defective)
        return None

    def _settle_decisions(self, requests: list[BuyerRequest], decisions: list[BuyerDecision]) -> None:
        pairs = list(zip(requests, decisions, strict=True))
        self.rng_order.shuffle(pairs)
        for req, d in pairs:
            if d.buyer_id != req.buyer_id:
                self._fail("DECISION_MISMATCH", req.buyer_id)
                continue
            self.decision_sources[d.source] += 1
            self.log.decision(self.tick, "buyer", d.model_dump())
            self.log.event(self.tick, "decision", buyer=d.buyer_id, purchase=d.purchase, ret=d.return_item,
                           resell=d.resell, source=d.source, reason=d.reason.value)
            if d.return_item and req.returnable:
                self.settle_return(req.buyer_id, req.returnable.purchase_id)
            if d.resell and req.resellable:
                r = req.resellable
                self.create_listing(req.buyer_id, r.unit_id, max(1, r.price_paid * d.resell_price_pct // 100))
            if d.purchase is not None:
                offer = next((o for o in req.offers if o.offer_id == d.purchase), None)
                if offer is None:
                    self._fail("NOT_SHORTLISTED", d.purchase, buyer=req.buyer_id)
                    continue
                if self.settle_purchase(req.buyer_id, offer.offer_id, offer.price).ok:
                    self.buyer_utility += offer_utility(req, offer)

    def _update_hype(self, boost: dict[str, float]) -> None:
        for p in self.w.products.values():
            rating_factor = 1.25 * p.rating / 5 if p.rating is not None else 1.0
            h = self.cfg.hype_decay * p.hype + self.cfg.hype_per_sale * p.sales_tick * rating_factor
            p.hype = round(min(1.0, max(0.0, h + boost.get(p.id, 0.0))), 6)

    def check_invariants(self) -> None:
        self.w.ledger.check()
        in_stock = sum(len(p.stock) for p in self.w.products.values())
        held = sum(len(b.owned) for b in self.w.buyers.values())
        if self.produced != in_stock + held + self.destroyed:
            raise AssertionError(f"units not conserved: produced={self.produced} stock={in_stock} "
                                 f"held={held} destroyed={self.destroyed}")
        for uid, lid in self.listed_units.items():
            lst = self.w.listings[lid]
            if not lst.active or self.w.units[uid].owner != lst.seller_id:
                raise AssertionError(f"listing {lid} not owned by seller")
        refunded = sum(1 for p in self.w.purchases.values() if p.refunded)
        if refunded != self.totals["returns"]:
            raise AssertionError("refunds do not match purchases")

    # ------------------------------------------------------------ loop

    async def step(self) -> dict:
        t = self.tick
        if t == 0 and self.positioning_model is not None:
            await self._positioning_phase()
        self._apply_shocks()
        for p in self.w.products.values():
            p.sales_tick = 0
            self.return_hist[p.id].append(0)
        self._income()
        self._produce()
        boost = self._marketing()
        if t > 0 and t % self.cfg.seller_trigger_every == 0:
            await self._seller_phase()
        self._grow_needs()
        self._reveal_and_rate()
        self._expire_listings()
        requests = self.build_requests()
        decisions = await self.buyer_model.decide_many(requests) if requests else []
        self._settle_decisions(requests, decisions)
        for p in self.w.products.values():
            self.sales_hist[p.id].append(p.sales_tick)
        self._update_hype(boost)
        self.check_invariants()
        summary = self.tick_summary(len(requests))
        self.log.tick_summary(summary)
        self.log.event(t, "tick_end", money=self.w.ledger.total(),
                       sales=sum(p.sales_tick for p in self.w.products.values()))
        self.tick += 1
        return summary

    def tick_summary(self, n_requests: int) -> dict:
        return {
            "tick": self.tick,
            "requests": n_requests,
            "sales": sum(p.sales_tick for p in self.w.products.values()),
            "prices": {p.id: p.price for p in self.w.products.values()},
            "stock": {p.id: len(p.stock) for p in self.w.products.values()},
            "hype": {p.id: round(p.hype, 4) for p in self.w.products.values()},
            "cash": {c.id: self.w.ledger.balance(c.account) for c in self.w.companies.values()},
        }

    async def run_async(self, ticks: int | None = None) -> dict:
        for _ in range(ticks if ticks is not None else self.cfg.ticks):
            await self.step()
        return self.summary()

    def run(self, ticks: int | None = None) -> dict:
        return asyncio.run(self.run_async(ticks))

    def summary(self) -> dict:
        companies = {}
        for c in self.w.companies.values():
            companies[c.id] = {
                "name": c.name,
                "cash": self.w.ledger.balance(c.account),
                "revenue": self.revenue[c.id],
                "costs": self.costs[c.id],
                "profit": self.revenue[c.id] - self.costs[c.id],
                "refund_pct": c.refund_pct,
                "products": {pid: {"price": self.w.products[pid].price,
                                   "category": self.w.products[pid].category,
                                   "quality": self.w.products[pid].quality_mean,
                                   "rating": round(self.w.products[pid].rating or 0, 3),
                                   "ratings": self.w.products[pid].rating_count,
                                   "sold": sum(self.sales_hist[pid]),
                                   "returns": sum(self.return_hist[pid])}
                             for pid in c.product_ids},
            }
        cash = sorted(self.w.ledger.balance(b.account) for b in self.w.buyers.values())
        return {
            "ticks": self.tick,
            "totals": dict(sorted(self.totals.items())),
            "failed_txns": dict(sorted(self.failed.items())),
            "decision_sources": dict(sorted(self.decision_sources.items())),
            "buyer_utility": round(self.buyer_utility, 4),
            "buyer_cash_gini": round(gini(cash), 4),
            "units": {"produced": self.produced, "destroyed": self.destroyed},
            "money": {"total": self.w.ledger.total(), "minted": self.w.ledger.minted, "sunk": self.w.ledger.sunk},
            "companies": companies,
        }


def gini(values: list[int]) -> float:
    n = len(values)
    total = sum(values)
    if n == 0 or total == 0:
        return 0.0
    cum = sum((i + 1) * v for i, v in enumerate(sorted(values)))
    return (2 * cum) / (n * total) - (n + 1) / n
