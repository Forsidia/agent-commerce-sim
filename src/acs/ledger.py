"""Simulated payment ledger: integer cents, atomic transfers, conservation checks.

No real payments. Money only enters via ``mint`` (salaries) and only leaves via
``sink`` (wages and marketing paid outside the modelled economy), so
``total() == initial + minted - sunk`` must always hold.
"""

from __future__ import annotations

from dataclasses import dataclass


class LedgerError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Entry:
    tick: int
    kind: str
    src: str | None
    dst: str | None
    amount: int
    ref: str


class Ledger:
    def __init__(self) -> None:
        self.balances: dict[str, int] = {}
        self.entries: list[Entry] = []
        self.initial = 0
        self.minted = 0
        self.sunk = 0

    def open(self, account: str, balance: int = 0) -> None:
        if account in self.balances:
            raise LedgerError("DUPLICATE_ACCOUNT", account)
        if balance < 0:
            raise LedgerError("NEGATIVE_AMOUNT", account)
        self.balances[account] = balance
        self.initial += balance

    def balance(self, account: str) -> int:
        return self.balances[account]

    def can_pay(self, account: str, amount: int) -> bool:
        return account in self.balances and amount >= 0 and self.balances[account] >= amount

    def transfer(self, src: str, dst: str, amount: int, *, tick: int, kind: str, ref: str) -> None:
        """All-or-nothing: every check runs before any balance changes."""
        if amount <= 0:
            raise LedgerError("NON_POSITIVE_AMOUNT", ref)
        if src not in self.balances or dst not in self.balances:
            raise LedgerError("UNKNOWN_ACCOUNT", ref)
        if src == dst:
            raise LedgerError("SELF_TRANSFER", ref)
        if self.balances[src] < amount:
            raise LedgerError("INSUFFICIENT_FUNDS", ref)
        self.balances[src] -= amount
        self.balances[dst] += amount
        self.entries.append(Entry(tick, kind, src, dst, amount, ref))

    def mint(self, dst: str, amount: int, *, tick: int, kind: str, ref: str) -> None:
        if amount <= 0 or dst not in self.balances:
            raise LedgerError("BAD_MINT", ref)
        self.balances[dst] += amount
        self.minted += amount
        self.entries.append(Entry(tick, kind, None, dst, amount, ref))

    def sink(self, src: str, amount: int, *, tick: int, kind: str, ref: str) -> None:
        if amount <= 0 or src not in self.balances:
            raise LedgerError("BAD_SINK", ref)
        if self.balances[src] < amount:
            raise LedgerError("INSUFFICIENT_FUNDS", ref)
        self.balances[src] -= amount
        self.sunk += amount
        self.entries.append(Entry(tick, kind, src, None, amount, ref))

    def total(self) -> int:
        return sum(self.balances.values())

    def check(self) -> None:
        if self.total() != self.initial + self.minted - self.sunk:
            raise AssertionError(
                f"money not conserved: total={self.total()} expected={self.initial + self.minted - self.sunk}"
            )
        negative = [a for a, b in self.balances.items() if b < 0]
        if negative:
            raise AssertionError(f"negative balances: {negative[:5]}")
