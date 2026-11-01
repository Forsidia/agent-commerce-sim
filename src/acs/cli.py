"""Command line: ``acs run``, ``acs replay`` and ``acs serve``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from acs.config import Settings, SimConfig
from acs.decisions.replay import DecisionRecord, ReplayBuyer, ReplayPositioning, ReplaySeller, ReplayTriggers
from acs.decisions.system_one import FakeSystemOneClient, OllamaSystemOneClient, SystemOneBuyer
from acs.decisions.utility import UtilityBuyer
from acs.engine import Simulation
from acs.models import Shock
from acs.runlog import RunLog
from acs.scenarios.example import build_world, default_shocks


def _buyer_model(kind: str, settings: Settings):
    if kind == "rule":
        return UtilityBuyer()
    client = (FakeSystemOneClient() if kind == "fake-s1"
              else OllamaSystemOneClient(settings.ollama_base_url, settings.s1_model, settings.s1_timeout_s))
    return SystemOneBuyer(client, concurrency=settings.s1_concurrency, conf_min=settings.s1_conf_min,
                          margin_min=settings.s1_margin_min)


def _llm_seller(settings: Settings):
    from acs.decisions.llm import LLMSeller, missing_api_key

    if var := missing_api_key(settings.seller_model):
        sys.exit(f"--sellers llm with {settings.seller_model} needs {var} exported in your shell")
    try:
        return LLMSeller(settings.seller_model, concurrency=settings.llm_concurrency, timeout_s=settings.llm_timeout_s)
    except ImportError:
        sys.exit("--sellers llm needs the llm extra: uv sync --extra llm")


def cmd_run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if args.buyers == "utility":
        args.buyers = "rule"
    cfg = SimConfig(seed=args.seed, ticks=args.ticks, seller_trigger_every=settings.seller_trigger_every)
    # every run gets its own folder, so a re-run never overwrites an earlier one
    name = args.name or (f"{time.strftime('%Y%m%d-%H%M%S')}_seed{args.seed}_{args.buyers}"
                         + ("_llm-sellers" if args.sellers == "llm" else ""))
    out = Path(args.out) / name
    n = 2
    while not args.name and out.exists():  # two runs started in the same second
        out = Path(args.out) / f"{name}-{n}"
        n += 1
    log = RunLog(out)
    buyer_model = _buyer_model(args.buyers, settings)
    llm = _llm_seller(settings) if args.sellers == "llm" else None
    sim = Simulation(build_world(cfg), cfg, buyer_model=buyer_model, log=log,
                     seller_model=llm, positioning_model=None if args.no_positioning else llm,
                     shocks=[] if args.no_shocks else default_shocks())
    meta = {"seed": args.seed, "ticks": args.ticks, "buyers": args.buyers, "sellers": args.sellers,
            "seller_model": settings.seller_model if llm else None, "sim": cfg.model_dump()}
    log.start(meta)
    summary = asyncio.run(sim.run_async())
    if isinstance(buyer_model, SystemOneBuyer):
        summary["system1"] = {"model": buyer_model.client.model, "calls": buyer_model.calls,
                              "cache_hits": buyer_model.cache_hits, "errors": buyer_model.errors}
    if llm:
        summary["llm_sellers"] = llm.stats()
    log.write(meta, summary)
    _print(summary, out)
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    meta = json.loads((run_dir / "config.json").read_text())
    cfg = SimConfig(**meta["sim"])
    record = DecisionRecord(run_dir / "decisions.jsonl")
    shocks = [Shock(**json.loads(line)) for line in (run_dir / "shocks.jsonl").read_text().splitlines() if line]
    log = RunLog()
    positioning = ReplayPositioning(record) if record.counts.get("positioning") else None
    sim = Simulation(build_world(cfg), cfg, buyer_model=ReplayBuyer(record), trigger_detector=ReplayTriggers(record),
                     seller_model=ReplaySeller(record), positioning_model=positioning, shocks=shocks, log=log)
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


def cmd_serve(args: argparse.Namespace) -> int:
    from acs.web.server import serve

    serve(Path(args.runs), args.port)
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
    for key in ("system1", "llm_sellers"):
        if key in summary:
            print(f"{key}: {summary[key]}")
    if out:
        print(f"logs: {out}/")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="acs", description="Forsidia agent commerce simulation")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the example market")
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--ticks", type=int, default=100)
    r.add_argument("--buyers", choices=["rule", "s1", "fake-s1", "utility"], default="rule",
                   help="rule (utility baseline), s1 (Ollama System 1 decision model, ACS_S1_MODEL), "
                        "or fake-s1 (offline stand-in); 'utility' is an old name for rule")
    r.add_argument("--sellers", choices=["rule", "llm"], default="rule",
                   help="rule baseline, or a hosted LLM (ACS_SELLER_MODEL) for positioning and slow decisions")
    r.add_argument("--no-positioning", action="store_true", help="with --sellers llm: keep the scenario's prices")
    r.add_argument("--no-shocks", action="store_true", help="disable the scenario's scripted shocks")
    r.add_argument("--out", default="runs")
    r.add_argument("--name", help="run folder name (default: <timestamp>_seed<N>_<buyers>[_llm-sellers])")
    r.set_defaults(func=cmd_run)
    p = sub.add_parser("replay", help="re-run a recorded run without model calls and verify the event log")
    p.add_argument("run_dir")
    p.set_defaults(func=cmd_replay)
    s = sub.add_parser("serve", help="web viewer: a supermarket view of recorded runs")
    s.add_argument("--runs", default="runs")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_serve)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
