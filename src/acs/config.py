"""Simulation parameters (seeded, serialisable) and runtime settings (env vars)."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel


class SimConfig(BaseModel):
    seed: int = 42
    ticks: int = 100
    shortlist_k: int = 4
    desire_threshold: float = 0.3
    return_window: int = 10  # ticks (1 tick = 1 day)
    reveal_after: int = 2  # ticks until a defect shows and the buyer rates
    salary_prob: float = 0.15  # chance per tick a buyer receives income
    defect_base: float = 0.25  # p_defect = defect_base * (1 - quality)^2 + extra
    hype_decay: float = 0.85
    hype_per_sale: float = 0.04
    hype_per_marketing_dollar: float = 0.002
    quality_prior: float = 0.6  # buyer belief before any ratings
    prior_weight: float = 3.0  # pseudo-ratings behind the prior
    resell_min_hold: int = 5
    listing_ttl: int = 10
    seller_trigger_every: int = 5
    max_price_step: float = 0.2  # sellers may move a price at most +-20% per decision
    price_band: tuple[float, float] = (0.5, 2.0)  # x base price


class Settings(BaseModel):
    """Model/runtime settings. Secrets are read from the process environment only."""

    ollama_base_url: str = "http://localhost:11434"
    s1_model: str = "tev1:4b"
    s1_concurrency: int = 4
    s1_timeout_s: float = 30.0
    s1_conf_min: float = 0.6
    s1_margin_min: float = 0.15
    seller_trigger_every: int = 5

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env") -> "Settings":
        values = _read_env_file(env_file) if env_file else {}
        values.update(os.environ)  # real env vars win over .env
        mapping = {
            "OLLAMA_BASE_URL": "ollama_base_url",
            "ACS_S1_MODEL": "s1_model",
            "ACS_S1_CONCURRENCY": "s1_concurrency",
            "ACS_S1_TIMEOUT_S": "s1_timeout_s",
            "ACS_S1_CONF_MIN": "s1_conf_min",
            "ACS_S1_MARGIN_MIN": "s1_margin_min",
            "ACS_SELLER_TRIGGER_EVERY": "seller_trigger_every",
        }
        return cls(**{field: values[env] for env, field in mapping.items() if values.get(env)})


def _read_env_file(path: str | Path) -> dict[str, str]:
    p = Path(path)
    if not p.is_file():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key == "ANTHROPIC_API_KEY":
            continue  # secrets come from the shell only
        out[key] = value.strip().strip('"').strip("'")
    return out
