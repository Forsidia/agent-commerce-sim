"""Replay recorded decisions so non-deterministic model runs reproduce exactly."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from acs.models import (
    BuyerDecision,
    BuyerRequest,
    CompanyDashboard,
    SellerAction,
    TriggerReport,
)


class ReplayError(Exception):
    pass


class DecisionRecord:
    """Loads ``decisions.jsonl``: one line per decision, keyed by (kind, tick, agent)."""

    def __init__(self, path: str | Path):
        self.by_key: dict[tuple[str, int, str], dict] = {}
        counts: dict[str, int] = defaultdict(int)
        for line in Path(path).read_text().splitlines():
            if not line:
                continue
            rec = json.loads(line)
            agent = rec["data"].get("buyer_id") or rec["data"]["company_id"]
            self.by_key[(rec["kind"], rec["tick"], agent)] = rec["data"]
            counts[rec["kind"]] += 1
        self.counts = dict(counts)

    def get(self, kind: str, tick: int, agent: str) -> dict:
        try:
            return self.by_key[(kind, tick, agent)]
        except KeyError:
            raise ReplayError(f"no recorded {kind} decision for {agent} at tick {tick}") from None


class ReplayBuyer:
    name = "replay"

    def __init__(self, record: DecisionRecord):
        self.record = record

    async def decide_many(self, requests: list[BuyerRequest]) -> list[BuyerDecision]:
        return [BuyerDecision(**self.record.get("buyer", r.tick, r.buyer_id)) for r in requests]


class ReplayTriggers:
    name = "replay"

    def __init__(self, record: DecisionRecord):
        self.record = record

    async def detect_many(self, dashboards: list[CompanyDashboard]) -> list[TriggerReport]:
        return [TriggerReport(**self.record.get("trigger", d.tick, d.company_id)) for d in dashboards]


class ReplaySeller:
    name = "replay"

    def __init__(self, record: DecisionRecord):
        self.record = record

    async def decide_many(
        self, dashboards: list[CompanyDashboard], reports: list[TriggerReport]
    ) -> list[SellerAction]:
        return [SellerAction(**self.record.get("seller", d.tick, d.company_id)) for d in dashboards]
