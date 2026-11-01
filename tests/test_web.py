import json

from acs.config import SimConfig
from acs.engine import Simulation
from acs.runlog import RunLog
from acs.scenarios.example import build_world, default_shocks
from acs.web.server import list_runs, run_payload


def test_live_run_streams_and_viewer_payload(tmp_path):
    run = tmp_path / "r1"
    log = RunLog(run)
    cfg = SimConfig(seed=5, ticks=12)
    log.start({"seed": 5, "ticks": 12, "buyers": "utility", "sim": cfg.model_dump()})
    sim = Simulation(build_world(cfg), cfg, shocks=default_shocks(), log=log)
    sim.run(6)
    assert list_runs(tmp_path)[0]["live"]
    partial = run_payload(tmp_path, "r1", 0)
    assert partial["live"] and len(partial["ticks"]) == 6
    assert len(partial["buyers"]) == 170 and partial["sellers"]["C1"]
    sim.run(6)
    log.write({"seed": 5, "ticks": 12, "buyers": "utility", "sim": cfg.model_dump()}, sim.summary())
    done = run_payload(tmp_path, "r1", 6)
    assert not done["live"] and [t["tick"] for t in done["ticks"]] == list(range(6, 12))
    assert all(int(k) >= 6 for k in done["events"])
    sales = sum(1 for evs in run_payload(tmp_path, "r1", 0)["events"].values() for e in evs if e["t"] == "sale")
    assert sales == sim.totals["sales_primary"] + sim.totals["sales_secondary"]
    # the streamed event log matches the final one
    assert (run / "events.jsonl").read_text().splitlines() == log.events
    assert json.loads((run / "summary.json").read_text())["ticks"] == 12
