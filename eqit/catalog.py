"""Quest catalog: ordered hand-in stages plus refining recipes.

Every quest must cite a source. The bundled catalog (data/catalog.json) holds only
source-backed data; add quests there rather than guessing requirements.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

BUNDLED_CATALOG = Path(__file__).with_name("data") / "catalog.json"


class CatalogError(Exception):
    pass


@dataclass(frozen=True)
class Requirement:
    item: str
    quantity: int


@dataclass(frozen=True)
class ManualCheck:
    """Requirement the user confirms by hand (e.g. a full-TT gear piece). Never inferred from
    inventory: export names/condition for gear aren't verified and aggregate TT isn't per-item."""
    id: str
    text: str
    hud: str


@dataclass(frozen=True)
class Stage:
    name: str
    hand_in: tuple[Requirement, ...]
    manual: tuple[ManualCheck, ...] = ()


@dataclass(frozen=True)
class Quest:
    id: str
    name: str
    source: str
    stages: tuple[Stage, ...]
    npc: str = ""
    npc_location: str = ""
    ordered: bool = True            # False: stages are listed in base order, not enforced
    more_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class Recipe:
    input: str
    input_qty: int
    output: str
    output_qty: int


@dataclass
class Catalog:
    quests: list[Quest]
    refining: dict[str, Recipe]  # keyed by output item name

    def get(self, quest_id: str | None) -> Quest | None:
        return next((q for q in self.quests if q.id == quest_id), None)


def _text(obj: dict, key: str, where: str) -> str:
    val = obj.get(key)
    if not isinstance(val, str) or not val.strip():
        raise CatalogError(f"{where}: '{key}' must be non-empty text.")
    return val.strip()


def _count(obj: dict, key: str, where: str) -> int:
    val = obj.get(key)
    if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
        raise CatalogError(f"{where}: '{key}' must be a positive whole number.")
    return val


def parse_catalog(data: object) -> Catalog:
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise CatalogError("Catalog must be a JSON object with \"schema\": 1.")
    refining: dict[str, Recipe] = {}
    for i, r in enumerate(data.get("refining", []), 1):
        where = f"refining #{i}"
        if not isinstance(r, dict):
            raise CatalogError(f"{where}: must be an object.")
        rec = Recipe(_text(r, "input", where), _count(r, "input_qty", where),
                     _text(r, "output", where), _count(r, "output_qty", where))
        if rec.output in refining:
            raise CatalogError(f"{where}: second recipe for {rec.output!r}.")
        refining[rec.output] = rec

    quests: list[Quest] = []
    for qi, q in enumerate(data.get("quests", []), 1):
        where = f"quest #{qi}"
        if not isinstance(q, dict):
            raise CatalogError(f"{where}: must be an object.")
        qid = _text(q, "id", where)
        if any(x.id == qid for x in quests):
            raise CatalogError(f"{where}: duplicate id {qid!r}.")
        source = _text(q, "source", where)  # no source, no quest
        raw_stages = q.get("stages")
        if not isinstance(raw_stages, list) or not raw_stages:
            raise CatalogError(f"{where}: 'stages' must be a non-empty ordered list.")
        stages = []
        for si, s in enumerate(raw_stages, 1):
            swhere = f"{where} stage {si}"
            if not isinstance(s, dict) or not isinstance(s.get("hand_in"), list):
                raise CatalogError(f"{swhere}: needs a 'hand_in' list.")
            reqs = []
            for ri, r in enumerate(s["hand_in"], 1):
                rwhere = f"{swhere} item {ri}"
                if not isinstance(r, dict):
                    raise CatalogError(f"{rwhere}: must be an object.")
                req = Requirement(_text(r, "item", rwhere), _count(r, "quantity", rwhere))
                if any(x.item == req.item for x in reqs):
                    raise CatalogError(f"{rwhere}: {req.item!r} listed twice in one stage.")
                reqs.append(req)
            checks = []
            for ci, c in enumerate(s.get("manual", []), 1):
                cwhere = f"{swhere} manual {ci}"
                if not isinstance(c, dict):
                    raise CatalogError(f"{cwhere}: must be an object.")
                check = ManualCheck(_text(c, "id", cwhere), _text(c, "text", cwhere), _text(c, "hud", cwhere))
                if any(x.id == check.id for x in checks):
                    raise CatalogError(f"{cwhere}: duplicate id {check.id!r}.")
                checks.append(check)
            stages.append(Stage(_text(s, "name", swhere), tuple(reqs), tuple(checks)))
        if len({s.name for s in stages}) != len(stages):
            raise CatalogError(f"{where}: stage names must be unique.")
        ordered = q.get("ordered", True)
        more = q.get("more_sources", [])
        if not isinstance(ordered, bool) or not isinstance(more, list) or not all(isinstance(m, str) for m in more):
            raise CatalogError(f"{where}: 'ordered' must be true/false and 'more_sources' a list of text.")
        quests.append(Quest(qid, _text(q, "name", where), source, tuple(stages),
                            str(q.get("npc", "")), str(q.get("npc_location", "")), ordered, tuple(more)))
    return Catalog(quests, refining)


def load_catalog(path: str | Path = BUNDLED_CATALOG) -> Catalog:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise CatalogError(f"Could not read catalog {path}: {exc}") from exc
    return parse_catalog(data)
