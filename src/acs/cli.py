"""Command line: ``acs run`` and ``acs replay``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from acs.config import Settings, SimConfig
from acs.decisions.replay import DecisionRecord, ReplayBuyer, ReplaySeller, ReplayTriggers
from acs.decisions.system_one import FakeSystemOneClient, OllamaSystemOneClient, SystemOneBuyer
from acs.decisions.utility import UtilityBuyer
from acs.engine import Simulation
from acs.models import Shock
from acs.runlog import RunLog
from acs.scenarios.example import build_world, default_shocks


def _buyer_model(kind: str, settings: Settings):
    if kind == "utility":
        return UtilityBuyer()
    client = (FakeSystemOneClient() if kind == "fake-s1"
              else OllamaSystemOneClient(settings.ollama_base_url, settings.s1_model, settings.s1_timeout_s))
    return SystemOneBuyer(client, concurrency=settings.s1_concurrency, conf_min=settings.s1_conf_min,
                          margin_min=settings.s1_margin_min)


def cmd_run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    cfg = SimConfig(seed=args.seed, ticks=args.ticks, seller_trigger_every=settings.seller_trigger_every)
    out = Path(args.out) / f"seed{args.seed}_{args.buyers}"
    log = RunLog(out)
    buyer_model = _buyer_model(args.buyers, settings)
    sim = Simulation(build_world(cfg), cfg, buyer_model=buyer_model, log=log,
                     shocks=[] if args.no_shocks else default_shocks())
    summary = asyncio.run(sim.run_async())
    if isinstance(buyer_model, SystemOneBuyer):
        summary["system1"] = {"model": buyer_model.client.model, "calls": buyer_model.calls,
                              "cache_hits": buyer_model.cache_hits, "errors": buyer_model.errors}
    log.write({"seed": args.seed, "ticks": args.ticks, "buyers": args.buyers, "sim": cfg.model_dump()}, summary)
    _print(summary, out)
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    meta = json.loads((run_dir / "config.json").read_text())
    cfg = SimConfig(**meta["sim"])
    record = DecisionRecord(run_dir / "decisions.jsonl")
    shocks = [Shock(**json.loads(line)) for line in (run_dir / "shocks.jsonl").read_text().splitlines() if line]
    log = RunLog()
    sim = Simulation(build_world(cfg), cfg, buyer_model=ReplayBuyer(record), trigger_detector=ReplayTriggers(record),
                     seller_model=ReplaySeller(record), shocks=shocks, log=log)
    summary = sim.run()
    original = (run_dir / "events.jsonl").read_text().splitlines()
    identical = original == log.events
    print(f"replayed {sim.tick} ticks from {run_dir}: events {'IDENTICAL' if identical else 'DIFFER'}")
    if not identical:
        diff = next(i for i, (a, b) in enumerate(zip(original, log.events)) if a != b) \
            if any(a != b for a, b in zip(original, log.events)) else min(len(original), len(log.events))
        print(f"first difference at event {diff}")
        return 1
    _print(summary, None)
    return 0


def _print(summary: dict, out: Path | None) -> None:
    t = summary["totals"]
    print(f"ticks={summary['ticks']} primary_sales={t.get('sales_primary', 0)} "
          f"resales={t.get('sales_secondary', 0)} returns={t.get('returns', 0)} "
          f"buyer_utility={summary['buyer_utility']} gini={summary['buyer_cash_gini']}")
    print(f"decision sources: {summary['decision_sources']}  failed: {summary['failed_txns']}")
    for cid, c in summary["companies"].items():
        print(f"  {cid} {c['name']:<18} revenue=${c['revenue'] / 100:>10,.2f} "
              f"profit=${c['profit'] / 100:>10,.2f} refund={c['refund_pct']}%")
    if "system1" in summary:
        print(f"system1: {summary['system1']}")
    if out:
        print(f"logs: {out}/")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="acs", description="Forsidia agent commerce simulation")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the example market")
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--ticks", type=int, default=100)
    r.add_argument("--buyers", choices=["utility", "fake-s1", "s1"], default="utility",
                   help="utility baseline, offline fake System 1, or Ollama System 1 (ACS_S1_MODEL)")
    r.add_argument("--no-shocks", action="store_true", help="disable the scenario's scripted shocks")
    r.add_argument("--out", default="runs")
    r.set_defaults(func=cmd_run)
    p = sub.add_parser("replay", help="re-run a recorded run without model calls and verify the event log")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_replay)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
