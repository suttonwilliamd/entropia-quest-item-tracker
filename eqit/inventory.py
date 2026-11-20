"""Manual import of Entropia Universe inventory exports.

Accepted input: the in-game inventory export, a quoted tab-separated file whose
first row is exactly  Id, Name, Quantity, Value(PED), Container, ContainerRefId.
A comma-separated file with the same header is also accepted.

Error handling (all-or-nothing):
* Any file-level or row-level error rejects the whole file and raises
  InventoryImportError; the previously imported snapshot stays in use.
* Non-fatal oddities (duplicate Ids, header-only file) become warnings.
* Value(PED) is never parsed or stored; it is irrelevant to hand-ins.

Locations: a row's location is its Container label. If its ContainerRefId equals
the Id of another row in the same file (an item inside a container item), the
row inherits that parent's location. Labels are never merged or guessed:
"STORAGE (Arkadia)", "STORAGE (Calypso)" and bare "STORAGE" stay distinct.
"""
from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

EXPECTED_HEADERS = ("Id", "Name", "Quantity", "Value(PED)", "Container", "ContainerRefId")
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_REPORTED_ERRORS = 20
NO_CONTAINER = "(no container)"
_INT = re.compile(r"\d+")


class InventoryImportError(Exception):
    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = list(errors or [])


@dataclass(frozen=True)
class InventoryRow:
    item_id: str
    name: str
    quantity: int
    container: str
    container_ref_id: str


@dataclass
class InventorySnapshot:
    rows: list[InventoryRow]
    source_path: str = ""
    imported_at: str = ""
    warnings: list[str] = field(default_factory=list)
    # Live chat.log pickups since import (estimate). Loot lands in CARRIED, so these count only
    # when CARRIED is in scope; they never create a location and are not part of to_json().
    carried_pickups: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        by_id = {r.item_id: r for r in self.rows if r.item_id}
        self._loc: list[str] = []
        for r in self.rows:
            cur, seen = r, set()
            while cur.container_ref_id in by_id and cur.container_ref_id not in seen:
                seen.add(cur.container_ref_id)
                parent = by_id[cur.container_ref_id]
                if parent is cur:
                    break
                cur = parent
            self._loc.append(cur.container or NO_CONTAINER)

    def location_of(self, index: int) -> str:
        return self._loc[index]

    def locations(self) -> dict[str, dict]:
        """location label -> {"rows": n, "quantity": total}, first-seen order."""
        out: dict[str, dict] = {}
        for r, loc in zip(self.rows, self._loc):
            info = out.setdefault(loc, {"rows": 0, "quantity": 0})
            info["rows"] += 1
            info["quantity"] += r.quantity
        return out

    def quantity(self, name: str, scope: set[str] | None = None) -> int:
        """Total quantity of the exactly-named item in scoped locations (None = everywhere)."""
        name = name.strip()
        live = self.carried_pickups.get(name, 0) if scope is None or "CARRIED" in scope else 0
        return live + sum(r.quantity for r, loc in zip(self.rows, self._loc)
                          if r.name == name and (scope is None or loc in scope))

    def to_json(self) -> dict:
        return {
            "source_path": self.source_path,
            "imported_at": self.imported_at,
            "warnings": self.warnings,
            "rows": [[r.item_id, r.name, r.quantity, r.container, r.container_ref_id] for r in self.rows],
        }

    @classmethod
    def from_json(cls, data: dict) -> "InventorySnapshot":
        rows = [InventoryRow(str(a), str(b), int(c), str(d), str(e)) for a, b, c, d, e in data["rows"]]
        return cls(rows, str(data.get("source_path", "")), str(data.get("imported_at", "")),
                   [str(w) for w in data.get("warnings", [])])


def decode_bytes(data: bytes) -> str:
    """UTF-8 (with/without BOM), UTF-16 with BOM, else Windows-1252."""
    try:
        if data.startswith(b"\xef\xbb\xbf"):
            text = data[3:].decode("utf-8")
        elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        else:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("cp1252")
    except UnicodeDecodeError:
        raise InventoryImportError(
            "File encoding not recognised (expected UTF-8, UTF-16 or Windows-1252). "
            "Re-export the inventory from the game.") from None
    return text.lstrip("﻿")


def _trim_trailing_empty(cells: list[str], keep: int) -> list[str]:
    while len(cells) > keep and not cells[-1].strip():
        cells = cells[:-1]
    return cells


def parse_inventory_text(text: str, source_path: str = "") -> InventorySnapshot:
    first_line = text.split("\n", 1)[0]
    if not first_line.strip():
        raise InventoryImportError("File is empty or its first line is blank; expected the export header row.")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t" if "\t" in first_line else ",")
    n = len(EXPECTED_HEADERS)
    try:
        header = [h.strip() for h in _trim_trailing_empty(next(reader), n)]
    except csv.Error:
        raise InventoryImportError("Line 1: header row is unreadable (bad quoting).") from None
    if tuple(header) != EXPECTED_HEADERS:
        # Name only the expected columns; never echo what the file contained.
        missing = [h for h in EXPECTED_HEADERS if h not in header]
        detail = ("missing column(s): " + ", ".join(missing)) if missing else (
            "columns are out of order or extra columns present")
        raise InventoryImportError(
            f"Not an inventory export: {detail}. Expected header: " + ", ".join(EXPECTED_HEADERS) + ".")

    rows: list[InventoryRow] = []
    errors: list[str] = []
    warnings: list[str] = []
    seen_ids: dict[str, int] = {}
    try:
        for cells in reader:
            line = reader.line_num
            if not any(c.strip() for c in cells):
                continue
            cells = _trim_trailing_empty(cells, n)
            if len(cells) == n - 1:  # empty ContainerRefId written without its trailing tab
                cells = cells + [""]
            if len(cells) != n:
                errors.append(f"Line {line}: expected {n} columns, found {len(cells)}.")
                continue
            item_id, name, qty, _value, container, ref = (c.strip() for c in cells)
            if not name:
                errors.append(f"Line {line}: Name is empty.")
                continue
            if not _INT.fullmatch(qty):
                errors.append(f"Line {line}: Quantity is not a whole number.")
                continue
            if item_id:
                if item_id in seen_ids:
                    warnings.append(f"Line {line}: same Id as line {seen_ids[item_id]}; both rows kept.")
                else:
                    seen_ids[item_id] = line
            rows.append(InventoryRow(item_id, name, int(qty), container, ref))
    except csv.Error:
        errors.append(f"Line {reader.line_num}: row is unreadable (bad quoting).")

    if errors:
        shown = errors[:MAX_REPORTED_ERRORS]
        if len(errors) > len(shown):
            shown.append(f"... and {len(errors) - len(shown)} more.")
        raise InventoryImportError(
            f"{len(errors)} problem(s) found; nothing was imported and the previous inventory is unchanged.", shown)
    if not rows:
        warnings.append("Export has a header but no items.")
    return InventorySnapshot(rows, source_path, datetime.now().isoformat(timespec="seconds"), warnings)


def load_inventory_file(path: str) -> InventorySnapshot:
    try:
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            raise InventoryImportError(f"File is {size // 1024} KB; the limit is {MAX_FILE_BYTES // 1024} KB.")
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise InventoryImportError(f"Could not read file: {exc.strerror or exc}.") from exc
    return parse_inventory_text(decode_bytes(data), os.path.abspath(path))
