"""Persisted settings/progress in %LOCALAPPDATA%\\EntropiaQuestItemTracker\\state.json.

Writes are atomic (temp file + replace). A corrupt file is renamed aside, never
deleted, and the app starts with defaults.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .tracker import DEFAULT_AU_ALIASES

OPACITY_MIN, OPACITY_MAX = 0.25, 1.0


def default_state() -> dict:
    return {
        "version": 1,
        "hud_opacity": 0.85,
        "hud_topmost": True,
        "hud_position": None,        # "+x+y"
        "selected_quest": "viceroy",
        "stage_by_quest": {},        # quest id -> stage index (len(stages) = complete)
        "manual_checks": {},         # quest id -> stage name -> [ticked manual check ids]
        "scope": None,               # selected location labels; None until first import
        "known_locations": [],
        "au_aliases": list(DEFAULT_AU_ALIASES),
        "inventory": None,           # last good import snapshot
    }


def state_path() -> Path:
    base = os.environ.get("EQIT_STATE_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA") or str(Path.home()), "EntropiaQuestItemTracker")
    return Path(base) / "state.json"


def clamp_opacity(value: object) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default_state()["hud_opacity"]
    if v != v:  # NaN
        return default_state()["hud_opacity"]
    return round(max(OPACITY_MIN, min(OPACITY_MAX, v)), 2)


def load_state(path: Path) -> dict:
    state = default_state()
    if not path.exists():
        return state
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        try:
            path.replace(path.with_name(f"state.corrupt-{int(time.time())}.json"))
        except OSError:
            pass
        return state
    for key, default in state.items():
        if key in data and (default is None or isinstance(data[key], type(default))):
            state[key] = data[key]
    state["hud_opacity"] = clamp_opacity(data.get("hud_opacity", state["hud_opacity"]))
    if not (state["scope"] is None or _str_list(state["scope"])):
        state["scope"] = None
    for key in ("known_locations", "au_aliases"):
        if not _str_list(state[key]):
            state[key] = default_state()[key]
    if not isinstance(state["hud_position"], (str, type(None))):
        state["hud_position"] = None
    if not isinstance(state["inventory"], (dict, type(None))):
        state["inventory"] = None
    state["stage_by_quest"] = {k: v for k, v in state["stage_by_quest"].items() if isinstance(v, int)}
    return state


def _str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, path)
