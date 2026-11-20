"""Pure tracking logic: location scope, have/need, refinable yield, HUD lines.

Rules:
* Progress is the stage the user selects. Inventory never advances a stage.
* Only items named in the current stage are counted; other loot is ignored.
* "have" = refined/finished items actually held in scoped locations.
  "refinable" = potential output from raw input held in scope, in whole
  batches only. The two are never added together silently.
"""
from __future__ import annotations

from dataclasses import dataclass

from .catalog import Catalog, Quest, Recipe, Stage
from .inventory import InventorySnapshot

LOCAL_LOCATIONS = ("CARRIED", "STORAGE (Arkadia)")
DEFAULT_AU_ALIASES: tuple[str, ...] = ()  # AU counts only via a label the user enters explicitly


def is_local(label: str, au_aliases: list[str] | tuple[str, ...]) -> bool:
    """Exact label match only. Bare 'STORAGE' is never treated as Arkadia/AU."""
    return label in LOCAL_LOCATIONS or label in au_aliases


def reconcile_scope(scope: list[str] | None, known: list[str], current: list[str],
                    au_aliases: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Merge a fresh import's locations into the saved selection.

    Previously seen locations keep the user's choice. Newly seen locations are
    selected only if they are local (CARRIED, STORAGE (Arkadia), an AU alias).
    Returns (scope, known, newly_seen).
    """
    scope = list(scope or [])
    known = list(known or [])
    new = [loc for loc in current if loc not in known]
    for loc in new:
        known.append(loc)
        if is_local(loc, au_aliases) and loc not in scope:
            scope.append(loc)
    return scope, known, new


@dataclass(frozen=True)
class ItemStatus:
    item: str
    need: int
    have: int | None       # None = no inventory imported yet
    refinable: int = 0     # potential refined output from scoped raw input
    recipe: Recipe | None = None

    @property
    def short(self) -> int | None:
        return None if self.have is None else max(0, self.need - self.have)

    @property
    def short_after_refining(self) -> int | None:
        return None if self.have is None else max(0, self.need - self.have - self.refinable)

    @property
    def covered(self) -> int | None:
        """Progress credit: physical + refinable, capped to need."""
        return None if self.have is None else min(self.need, self.have + self.refinable)

    @property
    def refine_credit(self) -> int:
        """Part of `covered` that still has to be refined (not physically held)."""
        return 0 if self.have is None else self.covered - min(self.need, self.have)

    @property
    def refine_batches(self) -> int:
        """Whole batches worth refining now: enough to cover the shortfall, capped by raw held."""
        if self.recipe is None or not self.short:
            return 0
        wanted = -(-self.short // self.recipe.output_qty)
        return min(wanted, self.refinable // self.recipe.output_qty)


def progress_text(st: ItemStatus) -> str:
    """Main-window progress, e.g. '2,000/2,000 refine (0 physical)'."""
    if st.have is None:
        return f"?/{st.need:,}"
    text = f"{st.covered:,}/{st.need:,}"
    return f"{text} refine ({st.have:,} physical)" if st.refine_credit else text


def action_text(st: ItemStatus) -> str:
    if st.have is None:
        return ""
    parts = []
    if st.refine_batches:
        r = st.recipe
        parts.append(f"Refine {st.refine_batches * r.input_qty} {r.input} → {st.refine_batches * r.output_qty}")
    if st.short_after_refining:
        parts.append(f"gather {st.short_after_refining}" if parts else f"Gather {st.short_after_refining}")
    return "; ".join(parts) or "Ready"


def shortfall_summary(statuses: list[ItemStatus]) -> tuple[str, str]:
    """(headline, action) for the main window: after-refining shortfall first."""
    if not statuses:
        return "Nothing to hand in for this stage", ""
    if any(s.have is None for s in statuses):
        return "Import inventory to see what is missing", ""
    short = [f"{s.item} {s.short_after_refining}" for s in statuses if s.short_after_refining]
    refine = [f"{s.refine_batches * s.recipe.input_qty} {s.recipe.input}" for s in statuses if s.refine_batches]
    if not short:
        return ("Ready after refining" if refine else "Ready to hand in"), (
            "Refine " + ", ".join(refine) if refine else "")
    action = ("Refine " + ", ".join(refine) + ", then gather the rest") if refine else "Gather the rest"
    return "Short after refining: " + " · ".join(short), action


def refinable_yield(catalog: Catalog, item: str, inv: InventorySnapshot | None, scope: set[str]) -> int:
    recipe = catalog.refining.get(item)
    if recipe is None or inv is None:
        return 0
    raw = inv.quantity(recipe.input, scope)
    return (raw // recipe.input_qty) * recipe.output_qty


def stage_status(catalog: Catalog, stage: Stage, inv: InventorySnapshot | None,
                 scope: set[str]) -> list[ItemStatus]:
    return [ItemStatus(r.item, r.quantity,
                       None if inv is None else inv.quantity(r.item, scope),
                       refinable_yield(catalog, r.item, inv, scope),
                       catalog.refining.get(r.item))
            for r in stage.hand_in]


def clamp_stage(quest: Quest, index: object) -> int:
    """Valid stage index; len(stages) means the quest is complete."""
    try:
        i = int(index)
    except (TypeError, ValueError):
        return 0
    return max(0, min(i, len(quest.stages)))


@dataclass(frozen=True)
class HudLine:
    text: str
    value: str = ""
    kind: str = "info"   # title | stage | ok | refine | short | unknown | info
    mark: str = ""       # "refine" when progress includes not-yet-refined material


HUD_NAME_CHARS = 24
HUD_MAX_ITEMS = 8


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


MANUAL_DONE, MANUAL_OPEN = "✓", "check"


def stage_label(quest: Quest, i: int) -> str:
    """Ordered quests: '2/7 Foot Guards'. Unordered (upgrades): 'Foot Guards · 2 of 7',
    i.e. a position in the base list, not an enforced sequence."""
    s, n = quest.stages[i], len(quest.stages)
    return _clip(f"{i + 1}/{n} {s.name}" if quest.ordered else f"{s.name} · {i + 1} of {n}", HUD_NAME_CHARS + 8)


def hud_layout_texts(quest: Quest) -> tuple[list[str], list[str]]:
    """Widest-possible column 0 / column 1 strings across every stage of a quest, so the
    HUD can keep one fixed width (and fixed arrow slots) while browsing. Values are
    bounded by need/need because displayed progress is capped to need."""
    col0 = [stage_label(quest, i) for i in range(len(quest.stages))]
    col0 += ["All stages complete"] + [_clip(r.item, HUD_NAME_CHARS) for s in quest.stages for r in s.hand_in]
    col0 += [_clip(c.hud, HUD_NAME_CHARS) for s in quest.stages for c in s.manual]
    col1 = [f"{r.quantity:,}/{r.quantity:,}" for s in quest.stages for r in s.hand_in] + [MANUAL_DONE, MANUAL_OPEN]
    return col0, col1


def hud_lines(catalog: Catalog, quest: Quest | None, stage_index: int,
              inv: InventorySnapshot | None, scope: set[str], checked: set[str] = frozenset()) -> list[HudLine]:
    """Compact HUD content. Only quest, stage, item names and have/need, plus manual
    checks (ticked by the user, never inferred); no values, locations or NPC positions."""
    if quest is None:
        return [HudLine("No quest selected", kind="info")]
    out = [HudLine(_clip(quest.name, HUD_NAME_CHARS + 8), kind="title")]
    if stage_index >= len(quest.stages):
        out.append(HudLine("All stages complete", kind="stage"))  # stage row: keeps browse arrows
        return out
    stage = quest.stages[stage_index]
    out.append(HudLine(stage_label(quest, stage_index), kind="stage"))
    for c in stage.manual:
        done = c.id in checked
        out.append(HudLine(_clip(c.hud, HUD_NAME_CHARS), MANUAL_DONE if done else MANUAL_OPEN,
                           "ok" if done else "short"))
    statuses = stage_status(catalog, stage, inv, scope)
    for st in statuses[:HUD_MAX_ITEMS]:
        if st.have is None:
            out.append(HudLine(_clip(st.item, HUD_NAME_CHARS), f"?/{st.need:,}", "unknown"))
        else:
            kind = "ok" if st.short == 0 else "refine" if st.short_after_refining == 0 else "short"
            out.append(HudLine(_clip(st.item, HUD_NAME_CHARS), f"{st.covered:,}/{st.need:,}", kind,
                               "refine" if st.refine_credit else ""))
    if len(statuses) > HUD_MAX_ITEMS:
        out.append(HudLine(f"+{len(statuses) - HUD_MAX_ITEMS} more", kind="info"))
    return out
