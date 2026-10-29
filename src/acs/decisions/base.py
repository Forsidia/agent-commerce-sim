"""Interfaces shared by deterministic and model-backed decision makers.

The engine only ever talks to these protocols, so a utility rule, a local
System 1 decision model, a Haiku-backed seller or a replay of recorded
decisions are interchangeable.
"""

from __future__ import annotations

from typing import Protocol

from acs.models import BuyerDecision, BuyerRequest, CompanyDashboard, SellerAction, TriggerReport


class BuyerModel(Protocol):
    name: str

    async def decide_many(self, requests: list[BuyerRequest]) -> list[BuyerDecision]:
        """Return one decision per request, in the same order."""
        ...


class TriggerDetector(Protocol):
    """Fast screen: does a company need a slow decision now?"""

    name: str

    async def detect_many(self, dashboards: list[CompanyDashboard]) -> list[TriggerReport]: ...


class SellerModel(Protocol):
    """Slow decision for a triggered company."""

    name: str

    async def decide_many(
        self, dashboards: list[CompanyDashboard], reports: list[TriggerReport]
    ) -> list[SellerAction]: ...
