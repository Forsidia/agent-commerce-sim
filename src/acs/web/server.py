"""``acs serve``: a read-only web viewer over the run directories.

Stdlib only. The page polls ``/api/run/<name>?since=<tick>``, so it follows a
run that is still being written (no ``summary.json`` yet) as well as replaying
finished ones. Static facts the logs do not carry (buyer tiers, product lines)
come from rebuilding the seeded world, which is deterministic.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from acs.config import SimConfig
from acs.scenarios.example import build_world

STATIC = Path(__file__).parent / "static"
KEEP = {"txn_ok", "return", "listing", "shock", "shock_end", "seller_action", "positioning"}


def list_runs(root: Path) -> list[dict]:
    out = []
    for d in sorted(root.glob("*/config.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = json.loads(d.read_text())
        ticks = d.parent / "ticks.jsonl"
        done = sum(1 for _ in ticks.open()) if ticks.exists() else 0
        out.append({"name": d.parent.name, "buyers": meta.get("buyers"), "sellers": meta.get("sellers", "rule"),
                    "seller_model": meta.get("seller_model"), "ticks": meta.get("ticks"), "done": done,
                    "live": not (d.parent / "summary.json").exists()})
    return out


@lru_cache(maxsize=16)
def static_world(sim_json: str) -> dict:
    w = build_world(SimConfig(**json.loads(sim_json)))
    return {
        "companies": [{"id": c.id, "name": c.name,
                       "products": [{"id": p, "category": w.products[p].category} for p in c.product_ids]}
                      for c in w.companies.values()],
        "buyers": {b.id: b.wealth_tier for b in w.buyers.values()},
        "sellers": {c: [s.id for s in w.sellers.values() if s.company_id == c] for c in w.companies},
        "categories": w.ref_prices,
    }


def compact(e: dict) -> dict | None:
    t = e["type"]
    if t == "txn_ok":
        return {"t": "sale", "m": e["market"], "b": e["buyer"], "p": e["product"], "price": e["price"]}
    if t == "return":
        return {"t": "return", "b": e["buyer"], "p": e["product"], "refund": e["refund"]}
    if t == "listing":
        return {"t": "listing", "b": e["seller"], "price": e["price"]}
    if t in ("shock", "shock_end"):
        return {"t": t, "kind": e["kind"], "target": e.get("target"), "value": e.get("value")}
    if t == "seller_action":
        return {"t": "seller", "c": e["company"], "src": e["source"], "reason": e["reason"], "applied": e["applied"]}
    if t == "positioning":
        return {"t": "positioning", "c": e["company"], "src": e["source"], "statement": e["statement"],
                "applied": e["applied"]}
    return None


def run_payload(root: Path, name: str, since: int) -> dict:
    d = root / name
    meta = json.loads((d / "config.json").read_text())
    summary_path = d / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else None
    ticks = [json.loads(line) for line in (d / "ticks.jsonl").read_text().splitlines() if line]
    events: dict[int, list] = defaultdict(list)
    failed: Counter[int] = Counter()
    defects: Counter[int] = Counter()
    active: set[str] = set()
    listings_active: dict[int, int] = {}
    unit_listing: dict[str, str] = {}
    for line in (d / "events.jsonl").read_text().splitlines():
        if not line:
            continue
        e = json.loads(line)
        t, tick = e["type"], e["tick"]
        # listings are tracked from tick 0 so the counts stay right when only recent ticks are sent
        if t == "listing":
            active.add(e["listing"])
            unit_listing[e["unit"]] = e["listing"]
        elif t == "listing_expired":
            active.discard(e["listing"])
        elif t == "txn_ok" and e["market"] == "secondary":
            active.discard(unit_listing.get(e["unit"], ""))
        elif t == "tick_end":
            listings_active[tick] = len(active)
        if tick < since:
            continue
        if t == "txn_failed":
            failed[tick] += 1
        elif t == "rating" and e["defective"]:
            defects[tick] += 1
            events[tick].append({"t": "defect", "p": e["product"]})
        elif t in KEEP and (c := compact(e)):
            events[tick].append(c)
    for row in ticks:
        row["listings_active"] = listings_active.get(row["tick"], 0)
        row["failed"] = failed[row["tick"]]
        row["defects"] = defects[row["tick"]]
    return {
        "name": name, "meta": meta, "summary": summary, "live": summary is None,
        **static_world(json.dumps(meta["sim"], sort_keys=True)),
        "ticks": [r for r in ticks if r["tick"] >= since],
        "events": {str(k): v for k, v in sorted(events.items())},
    }


def make_handler(root: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(json.dumps(obj, separators=(",", ":")).encode(), "application/json", code)

        def do_GET(self):
            url = urlparse(self.path)
            path = unquote(url.path)
            try:
                if path in ("/", "/index.html"):
                    return self._send((STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                if path == "/api/runs":
                    return self._json(list_runs(root))
                if path.startswith("/api/run/"):
                    name = path.removeprefix("/api/run/")
                    if "/" in name or name.startswith(".") or not (root / name / "config.json").exists():
                        return self._json({"error": "unknown run"}, 404)
                    since = int(parse_qs(url.query).get("since", ["0"])[0])
                    return self._json(run_payload(root, name, since))
            except (OSError, ValueError, KeyError) as e:  # a live run may be mid-write; the page retries
                return self._json({"error": str(e)}, 503)
            self._json({"error": "not found"}, 404)

    return Handler


def serve(root: Path, port: int = 8765) -> None:
    root.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(root))
    print(f"acs viewer: http://127.0.0.1:{port}/  (runs from {root.resolve()}, Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
