"""Live application of optimizer-tuned parameters.

Reads ``data/tuning.json`` (written by the backtest optimizer) and exposes the
tuned values to the live engine. If the file is missing or a key is absent, the
config default is used — so the bot behaves normally until a validated tuning
exists, then automatically adopts the improved settings.
"""
from __future__ import annotations

import json
import os

_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "tuning.json")
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(_PATH, encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception:
            _cache = {}
    return _cache


def reload() -> None:
    global _cache
    _cache = None


def get(key: str, default):
    val = _load().get(key)
    return default if val is None else val


def active() -> dict:
    return dict(_load())
