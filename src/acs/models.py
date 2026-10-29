"""Domain state (mutable dataclasses, engine-owned) and decision schemas (Pydantic)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- state (engine-owned)

Market = Literal["primary", "secondary"]


@dataclass(slots=True)
class Unit:
    id: str
    product_id: str
    quality: float  # 0..1, hidden from buyers
    defective: bool  # hidden until revealed
    produced_tick: int
    owner: str | None = None  # buyer id; None while in company stock
    revealed: bool = False  # defect visible after use
    destroyed: bool = False  # returned units are written off


@dataclass(slots=True)
class Product:
    id: str
    company_id: str
    category: str
    base_price: int  # cents
    price: int  # cents, current list price
    quality_mean: float
    quality_sd: float
    tier: Literal["budget", "mid", "premium", "all"]
    stock: list[str] = field(default_factory=list)  # unit ids, FIFO
    max_stock: int = 60
    hype: float = 0.0
    rating_sum: float = 0.0
    rating_count: int = 0
    sales_tick: int = 0  # primary sales in the current tick
    supply_factor: float = 1.0  # admin shocks scale production
    defect_extra: float = 0.0  # admin shocks add defect probability

    @property
    def rating(self) -> float | None:
        return self.rating_sum / self.rating_count if self.rating_count else None


@dataclass(slots=True)
class Company:
    id: str
    name: str
    account: str
    product_ids: list[str]
    refund_pct: int = 100  # return policy: 0, 50 or 100
    marketing_spend: int = 0  # cents per tick


@dataclass(slots=True)
class Producer:
    id: str
    company_id: str
    product_id: str
    capacity: int  # units per tick
    skill: float  # 0..1, lowers quality variance
    wage: int  # cents per tick


@dataclass(slots=True)
class Seller:
    id: str
    company_id: str


@dataclass(slots=True)
class Buyer:
    id: str
    account: str
    wealth_tier: Literal["budget", "mid", "premium"]
    needs: dict[str, float]  # category -> 0..1
    need_rates: dict[str, float]
    prefs: dict[str, float]  # category -> weight
    price_sensitivity: float
    hype_sensitivity: float
    salary_mean: int  # cents
    owned: list[str] = field(default_factory=list)  # unit ids


@dataclass(slots=True)
class Purchase:
    id: str
    unit_id: str
    buyer_id: str
    payee: str  # account paid
    price: int
    tick: int
    market: Market
    refunded: bool = False
    rated: bool = False


@dataclass(slots=True)
class Listing:
    id: str
    unit_id: str
    seller_id: str  # buyer id
    price: int
    tick: int
    active: bool = True


# ---------------------------------------------------------------- decision schemas


class ReasonCode(StrEnum):
    BEST_SURPLUS = "BEST_SURPLUS"
    NO_POSITIVE_SURPLUS = "NO_POSITIVE_SURPLUS"
    RETURN_DEFECT = "RETURN_DEFECT"
    RESELL_SURPLUS = "RESELL_SURPLUS"
    S1_CHOICE = "S1_CHOICE"
    S1_LOW_CONFIDENCE = "S1_LOW_CONFIDENCE"
    S1_INVALID = "S1_INVALID"
    S1_ERROR = "S1_ERROR"
    REPLAY = "REPLAY"


class Offer(BaseModel):
    offer_id: str  # product id (primary) or listing id (secondary)
    market: Market
    product_id: str
    company_id: str
    category: str
    price: int
    ref_price: int  # category reference price, for normalising cost
    expected_quality: float  # buyer's belief: blend of rating and prior
    rating: float | None
    rating_count: int
    hype: float
    defect_known: bool = False


class ReturnCandidate(BaseModel):
    purchase_id: str
    unit_id: str
    category: str
    price_paid: int
    refund_pct: int
    defective: bool  # only set once revealed
    ticks_left: int


class ResellCandidate(BaseModel):
    unit_id: str
    product_id: str
    category: str
    price_paid: int
    reference_price: int  # current primary price of the product
    defect_known: bool


class BuyerRequest(BaseModel):
    buyer_id: str
    tick: int
    cash: int
    wealth_tier: str
    needs: dict[str, float]
    prefs: dict[str, float]
    price_sensitivity: float
    hype_sensitivity: float
    offers: list[Offer]
    returnable: ReturnCandidate | None = None
    resellable: ResellCandidate | None = None


class BuyerDecision(BaseModel):
    buyer_id: str
    purchase: str | None = None  # offer_id or None (pass)
    estimated_utility: float = Field(0.0, ge=-1.0, le=1.0)
    return_item: bool = False
    resell: bool = False
    resell_price_pct: int = Field(60, ge=10, le=100)
    reason: ReasonCode
    source: str  # utility | system1 | fallback | replay | fake-s1
    confidence: float | None = None
    probabilities: dict[str, float] | None = None
    latency_ms: float | None = None


class ProductStats(BaseModel):
    product_id: str
    category: str
    price: int
    base_price: int
    stock: int
    sales_window: int
    returns_window: int
    rating: float | None
    rating_prev: float | None
    hype: float
    competitor_min_price: int | None


class CompanyDashboard(BaseModel):
    company_id: str
    tick: int
    cash: int
    refund_pct: int
    marketing_spend: int
    products: list[ProductStats]
    shocked: bool  # an admin shock touched this company in the window


class TriggerReport(BaseModel):
    company_id: str
    flags: dict[str, float]  # trigger name -> probability / 0..1 score
    fired: list[str]


class SellerAction(BaseModel):
    company_id: str
    price_changes: dict[str, float] = Field(default_factory=dict)  # product_id -> fraction, e.g. 0.1
    refund_pct: Literal[0, 50, 100] | None = None
    marketing_spend: int | None = None  # cents per tick
    reason: str = ""
    source: str = "rule"


class Shock(BaseModel):
    """Admin/scenario intervention applied at a tick boundary."""

    tick: int
    kind: Literal["supply_cut", "defect_spike", "hype", "salary", "cash"]
    target: str | None = None  # product id or company id (None = all)
    value: float
    duration: int = 0  # ticks; 0 = permanent
