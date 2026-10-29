"""JSONL run output.

``events.jsonl`` is deterministic (no wall-clock, rounded floats), so two runs
with the same seed and decisions are byte-identical. Model latency and raw
probabilities go to ``decisions.jsonl``, which replay reads back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


class RunLog:
    def __init__(self, out_dir: str | Path | None = None):
        self.out_dir = Path(out_dir) if out_dir else None
        self.events: list[str] = []
        self.decisions: list[str] = []
        self.shocks: list[str] = []
        self.ticks: list[dict] = []

    def event(self, tick: int, type_: str, **data: Any) -> None:
        self.events.append(dumps({"tick": tick, "type": type_, **data}))

    def decision(self, tick: int, kind: str, data: dict) -> None:
        self.decisions.append(dumps({"tick": tick, "kind": kind, "data": data}))

    def shock(self, data: dict) -> None:
        self.shocks.append(dumps(data))

    def tick_summary(self, summary: dict) -> None:
        self.ticks.append(summary)

    def write(self, config: dict, summary: dict) -> Path | None:
        if self.out_dir is None:
            return None
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "config.json").write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
        for name, lines in (("events", self.events), ("decisions", self.decisions), ("shocks", self.shocks)):
            (self.out_dir / f"{name}.jsonl").write_text("".join(line + "\n" for line in lines))
        (self.out_dir / "ticks.jsonl").write_text("".join(dumps(t) + "\n" for t in self.ticks))
        (self.out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        return self.out_dir
