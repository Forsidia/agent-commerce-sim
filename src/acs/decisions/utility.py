"""Transparent deterministic baselines: utility buyer, rule trigger detector, rule seller."""

from __future__ import annotations

from acs.models import (
    BuyerDecision,
    BuyerRequest,
    CompanyDashboard,
    Offer,
    ReasonCode,
    SellerAction,
    TriggerReport,
)

VALUE_SCALE = 1.5
DEFECT_KNOWN_FACTOR = 0.3
RESELL_NEED_MAX = 0.25


def desire(req: BuyerRequest, offer: Offer) -> float:
    """Need plus hype pull, capped at 1."""
    return min(1.0, req.needs.get(offer.category, 0.0) + req.hype_sensitivity * offer.hype)


def offer_utility(req: BuyerRequest, offer: Offer) -> float:
    """utility = 1.5 * desire * pref * expected_quality - price_sensitivity * price / ref_price"""
    quality = offer.expected_quality * (DEFECT_KNOWN_FACTOR if offer.defect_known else 1.0)
    value = VALUE_SCALE * desire(req, offer) * req.prefs.get(offer.category, 0.5) * quality
    cost = req.price_sensitivity * offer.price / offer.ref_price
    return value - cost


def clamp_utility(u: float) -> float:
    return max(-1.0, min(1.0, round(u, 6)))


def utility_decision(req: BuyerRequest, source: str = "utility") -> BuyerDecision:
    best: Offer | None = None
    best_u = 0.0
    for offer in req.offers:
        u = offer_utility(req, offer)
        if u > best_u:  # strict: ties keep the earlier (higher-ranked) offer
            best, best_u = offer, u
    ret = req.returnable
    res = req.resellable
    return_item = bool(ret and ret.defective and ret.refund_pct > 0)
    resell = bool(res and req.needs.get(res.category, 0.0) < RESELL_NEED_MAX)
    if best is not None:
        reason = ReasonCode.BEST_SURPLUS
    elif return_item:
        reason = ReasonCode.RETURN_DEFECT
    elif resell:
        reason = ReasonCode.RESELL_SURPLUS
    else:
        reason = ReasonCode.NO_POSITIVE_SURPLUS
    return BuyerDecision(
        buyer_id=req.buyer_id,
        purchase=best.offer_id if best else None,
        estimated_utility=clamp_utility(best_u),
        return_item=return_item,
        resell=resell,
        resell_price_pct=30 if (res and res.defect_known) else 70,
        reason=reason,
        source=source,
    )


class UtilityBuyer:
    name = "utility"

    async def decide_many(self, requests: list[BuyerRequest]) -> list[BuyerDecision]:
        return [utility_decision(r) for r in requests]


# ---------------------------------------------------------------- sellers

TRIGGERS = ("stockout_risk", "overstock", "rating_drop", "high_returns", "undercut", "shock")


def trigger_scores(d: CompanyDashboard) -> dict[str, float]:
    """Rule-based 0..1 scores with the same shape a System 1 screen returns."""
    s = dict.fromkeys(TRIGGERS, 0.0)
    for p in d.products:
        demand = max(p.sales_window, 1)
        if p.stock < demand * 0.5:
            s["stockout_risk"] = 1.0
        if p.stock > demand * 6 and p.sales_window <= 2:
            s["overstock"] = 1.0
        if p.rating is not None and p.rating_prev is not None and p.rating < p.rating_prev - 0.3:
            s["rating_drop"] = 1.0
        if p.sales_window and p.returns_window / p.sales_window > 0.25:
            s["high_returns"] = 1.0
        if p.competitor_min_price is not None and p.competitor_min_price < p.price * 0.85:
            s["undercut"] = 1.0
    if d.shocked:
        s["shock"] = 1.0
    return s


class RuleTriggerDetector:
    name = "rule-triggers"

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold

    async def detect_many(self, dashboards: list[CompanyDashboard]) -> list[TriggerReport]:
        out = []
        for d in dashboards:
            flags = trigger_scores(d)
            out.append(TriggerReport(company_id=d.company_id, flags=flags,
                                     fired=[k for k, v in flags.items() if v >= self.threshold]))
        return out


class RuleSeller:
    """Deterministic baseline for slow seller decisions."""

    name = "rule"

    async def decide_many(
        self, dashboards: list[CompanyDashboard], reports: list[TriggerReport]
    ) -> list[SellerAction]:
        return [self._decide(d, r) for d, r in zip(dashboards, reports, strict=True)]

    @staticmethod
    def _decide(d: CompanyDashboard, r: TriggerReport) -> SellerAction:
        changes: dict[str, float] = {}
        for p in d.products:
            demand = max(p.sales_window, 1)
            if p.stock < demand * 0.5:
                changes[p.product_id] = 0.10  # scarce: raise price
            elif p.stock > demand * 6 and p.sales_window <= 2:
                changes[p.product_id] = -0.10  # piling up: discount
            elif p.competitor_min_price is not None and p.competitor_min_price < p.price * 0.85:
                changes[p.product_id] = -0.05
        refund = None
        if "high_returns" in r.fired and d.refund_pct > 50:
            refund = 50  # tighten policy when returns bite
        marketing = None
        if "rating_drop" in r.fired or "overstock" in r.fired:
            marketing = min(d.cash // 50, max(d.marketing_spend, 500) * 2)
        elif d.marketing_spend and "stockout_risk" in r.fired:
            marketing = 0  # no point advertising what you cannot supply
        return SellerAction(
            company_id=d.company_id,
            price_changes=changes,
            refund_pct=refund,
            marketing_spend=marketing,
            reason=",".join(r.fired) or "none",
            source="rule",
        )
