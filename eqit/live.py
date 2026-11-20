"""Live item-receipt additions from Entropia's chat.log, kept in SQLite next to state.json.

Trust rules
* Only complete lines in the exact personal receipt format are credited:
  "YYYY-MM-DD HH:MM:SS [System] [] You received <Item> x (<n>) Value: <v> PED".
  That line says an item entered *your* inventory; it does not say why. Loot, trades,
  mission rewards or crafting may use the same line, so credits are labelled "received"
  estimates, never hunt/loot provenance. Other channels and players (incl. typed/mock copies
  of the line), globals, skill/other system lines and partial lines are ignored.
* Only catalog-tracked items are stored. Refining outputs are never credited (that would
  double count against +Refinable); crafting, refining, trades, sales and hand-ins are not
  inferred, so counts are estimates until the next import.
* A successful import atomically replaces the baseline, clears pickups and anchors the
  cursor at the log's current end: nothing logged before the import is ever counted.
* Pickups and the cursor advance in one transaction, so a crash, retry or restart can't
  double count. Any doubt about the file (replaced, truncated, cursor mid-line) re-anchors
  at the end without crediting and flags a gap until the next import.
* No chat text is stored or shown: only tracked item names, quantities, the byte cursor and
  a hash of the file's first bytes (to recognise the same file).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from collections import Counter
from contextlib import closing, contextmanager
from pathlib import Path

from .inventory import InventorySnapshot

LOOT = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[System\] \[\] "
                  r"You received (.+) x \((\d+)\) Value: \d+(?:\.\d+)? PED$")
PREFIX = 256
MAX_READ = 1 << 20
QUIET_SECONDS = 15 * 60
_FOLDERID_DOCUMENTS = "FDD39AD0-238F-46AF-ADB4-6C85480369C7"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS baseline (id INTEGER PRIMARY KEY CHECK (id = 1), snapshot TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pickups (item TEXT PRIMARY KEY, qty INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cursor (id INTEGER PRIMARY KEY CHECK (id = 1), path TEXT NOT NULL,
    file_id TEXT NOT NULL, prefix_len INTEGER NOT NULL, prefix_hash TEXT NOT NULL,
    offset INTEGER NOT NULL, gap INTEGER NOT NULL, grew_at REAL NOT NULL);
"""


def parse_pickup(line: str) -> tuple[str, int] | None:
    m = LOOT.match(line.lstrip("﻿"))
    return (m.group(1), int(m.group(2))) if m and int(m.group(2)) > 0 else None


def tracked_items(catalog) -> set[str]:
    """Hand-in items that are not refining outputs, plus refining inputs."""
    outputs = set(catalog.refining)
    items = {r.item for q in catalog.quests for s in q.stages for r in s.hand_in if r.item not in outputs}
    return items | {r.input for r in catalog.refining.values()}


def _documents_dir() -> str | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        guid = ctypes.create_string_buffer(uuid.UUID(_FOLDERID_DOCUMENTS).bytes_le, 16)
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return out.value
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except (OSError, AttributeError, ValueError):
        return None


def find_chat_log(candidates: list[str | None] | None = None) -> Path | None:
    """First existing <Documents>\\Entropia Universe\\chat.log: the Windows Documents known
    folder (OneDrive-redirected or not), then %OneDrive%\\Documents, then %USERPROFILE%\\Documents."""
    if candidates is None:
        candidates = [_documents_dir()] + [
            os.path.join(os.environ[v], "Documents") for v in ("OneDrive", "USERPROFILE") if os.environ.get(v)]
    for d in candidates:
        if d:
            p = Path(d) / "Entropia Universe" / "chat.log"
            if p.is_file():
                return p
    return None


class LiveLog:
    """status: off | idle | live | gap | unreadable | unsupported | error"""

    def __init__(self, db_path: Path, log_path: Path | None, tracked: set[str]):
        self.db_path = Path(db_path)
        self.log_path = Path(log_path) if log_path else None
        self.tracked = set(tracked)
        self.status = "off" if self.log_path is None else "idle"
        self.quiet = False
        with self._conn() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        with closing(sqlite3.connect(self.db_path, isolation_level=None, timeout=5)) as db:
            yield db

    @contextmanager
    def _tx(self):
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    # ----- reads -----
    def baseline(self) -> InventorySnapshot | None:
        with self._conn() as db:
            row = db.execute("SELECT snapshot FROM baseline").fetchone()
        return InventorySnapshot.from_json(json.loads(row[0])) if row else None

    def pickups(self) -> dict[str, int]:
        with self._conn() as db:
            return dict(db.execute("SELECT item, qty FROM pickups"))

    def _cursor(self, db) -> tuple | None:
        return db.execute("SELECT path, file_id, prefix_len, prefix_hash, offset, gap, grew_at FROM cursor").fetchone()

    @staticmethod
    def _write_cursor(db, path, file_id, head: bytes, offset: int, gap: int) -> None:
        n = min(len(head), PREFIX, offset) if offset else 0
        db.execute("DELETE FROM cursor")
        db.execute("INSERT INTO cursor VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                   (path, file_id, n, hashlib.sha256(head[:n]).hexdigest(), offset, gap, time.time()))

    def _open(self):
        fh = open(self.log_path, "rb")
        st = os.fstat(fh.fileno())
        return fh, f"{st.st_dev}:{st.st_ino}", st.st_size, fh.read(PREFIX)

    # ----- writes -----
    def reset(self, snap: InventorySnapshot, gap: bool = False) -> None:
        """Successful import: replace baseline, clear pickups, anchor at the log's end. Atomic.
        gap=True (migrating an old snapshot with no trustworthy cursor): anchor without replay
        and keep 'reimport needed' showing until the next real import."""
        anchor = None
        if self.log_path is not None:
            try:
                fh, fid, size, head = self._open()
                fh.close()
                anchor = (fid, head, size)
            except OSError:
                anchor = None
        with self._tx() as db:
            db.execute("DELETE FROM baseline")
            db.execute("INSERT INTO baseline VALUES (1, ?)", (json.dumps(snap.to_json()),))
            db.execute("DELETE FROM pickups")
            db.execute("DELETE FROM cursor")
            if anchor:
                self._write_cursor(db, str(self.log_path), anchor[0], anchor[1], anchor[2], int(gap))
        self.status = "off" if self.log_path is None else (("gap" if gap else "live") if anchor else "unreadable")

    def poll(self) -> None:
        if self.log_path is None:
            self.status = "off"
            return
        try:
            with self._conn() as db:
                if db.execute("SELECT 1 FROM baseline").fetchone() is None:
                    self.status = "idle"
                    return
            fh, fid, size, head = self._open()
            with fh:
                self._poll_open(fh, fid, size, head)
        except OSError:
            self.status = "unreadable"
        except sqlite3.Error:
            self.status = "error"

    def _poll_open(self, fh, fid: str, size: int, head: bytes) -> None:
        if head.startswith((b"\xff\xfe", b"\xfe\xff")):
            self.status = "unsupported"   # UTF-16 log: not parsed rather than misread
            return
        with self._conn() as db:
            cur = self._cursor(db)
        same = (cur is not None and cur[0] == str(self.log_path) and cur[1] == fid and size >= cur[4]
                and hashlib.sha256(head[:cur[2]]).hexdigest() == cur[3])
        if not same:  # new, replaced, rotated or truncated file: re-anchor, credit nothing
            with self._tx() as db:
                self._write_cursor(db, str(self.log_path), fid, head, size, 1)
            self.status, self.quiet = "gap", False
            return
        offset, gap, grew_at = cur[4], cur[5], cur[6]
        self.status = "gap" if gap else "live"
        self.quiet = size == offset and time.time() - grew_at > QUIET_SECONDS
        if size == offset:
            return
        base = offset - 1 if offset else 0
        fh.seek(base)
        chunk = fh.read(min(size - offset, MAX_READ) + (offset - base))
        start = 0
        if offset:   # never credit the remainder of a line the cursor landed inside
            start = 1 if chunk[:1] == b"\n" else chunk.find(b"\n", 1) + 1
        end = chunk.rfind(b"\n")
        if start <= 0 and offset or end < start:
            return   # no complete new line yet
        gains: Counter[str] = Counter()
        for raw in chunk[start:end].split(b"\n"):
            hit = parse_pickup(raw.rstrip(b"\r").decode("utf-8", "replace"))
            if hit and hit[0] in self.tracked:
                gains[hit[0]] += hit[1]
        self._apply(cur, gains, fid, head, base + end + 1, gap)

    def _apply(self, cur, gains: Counter, fid: str, head: bytes, new_offset: int, gap: int) -> None:
        with self._tx() as db:
            if self._cursor(db) != cur:   # another writer moved it: retry next poll
                return
            for item, qty in gains.items():
                db.execute("INSERT INTO pickups VALUES (?, ?) ON CONFLICT(item) DO UPDATE SET qty = qty + excluded.qty",
                           (item, qty))
            self._write_cursor(db, cur[0], fid, head, new_offset, gap)
        self.quiet = False
