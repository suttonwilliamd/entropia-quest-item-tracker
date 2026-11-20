"""Live chat.log receipts: sanitized exact-format fixtures in temp files + temp SQLite only."""
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from eqit import live
from eqit.catalog import load_catalog
from eqit.inventory import InventoryRow, InventorySnapshot
from eqit.live import LiveLog, find_chat_log, parse_pickup, tracked_items

SECRET = "ZZSECRETZZ"


def got(item, n, value="0.10", ts="2026-11-20 12:00:00"):
    return f"{ts} [System] [] You received {item} x ({n}) Value: {value} PED\r\n"


NOT_RECEIPTS = [
    f"2026-11-20 12:00:01 [Globals] [] {SECRET} killed a creature (Atrox) with a value of 55 PED!\r\n",
    f"2026-11-20 12:00:02 [#calypso] [{SECRET}] You received Animal Oil Residue x (500) Value: 5.00 PED\r\n",
    f"2026-11-20 12:00:03 [Team] [] {SECRET} received Animal Oil Residue x (7) Value: 0.07 PED\r\n",
    f"2026-11-20 12:00:04 [System] [{SECRET}] You received Animal Oil Residue x (9) Value: 0.09 PED\r\n",
    "2026-11-20 12:00:05 [System] [] You have gained 0.1234 experience in your Laser Weaponry Technology skill\r\n",
    "2026-11-20 12:00:06 [System] [] You received Animal Oil Residue x (0) Value: 0.00 PED\r\n",
    "2026-11-20 12:00:07 [System] [] You received Animal Oil Residue x (abc) Value: 0.00 PED\r\n",
    "2026-11-20 12:00:08 [System] [] You received Animal Oil Residue x (4)\r\n",
    "[System] [] You received Animal Oil Residue x (4) Value: 0.04 PED\r\n",
    f"2026-11-20 12:00:09 [System] [] You received {SECRET} Loot x (3) Value: 1.00 PED\r\n",   # untracked
    "2026-11-20 12:00:10 [System] [] You received Corria Veneer x (2000) Value: 2.00 PED\r\n",  # refining output
]


class ParserTests(unittest.TestCase):
    def test_exact_personal_receipts_only(self):
        self.assertEqual(parse_pickup(got("Paint Can (Yellow)", 3).rstrip()), ("Paint Can (Yellow)", 3))
        self.assertEqual(parse_pickup("﻿" + got("Soft Hide", 12).rstrip()), ("Soft Hide", 12))
        for line in NOT_RECEIPTS[:9]:
            self.assertIsNone(parse_pickup(line.rstrip()), line)

    def test_tracked_items_exclude_refining_outputs(self):
        items = tracked_items(load_catalog())
        self.assertTrue({"Corria Timber", "Soft Hide", "Animal Oil Residue", "Paint Can (Yellow)"} <= items)
        self.assertFalse({"Corria Veneer", "Soft Leather", "Wool Thread", "Wool Cloth"} & items)

    def test_find_chat_log_prefers_first_existing_candidate(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d, "known"), Path(d, "profile")
            (b / "Entropia Universe").mkdir(parents=True)
            (b / "Entropia Universe" / "chat.log").write_bytes(b"")
            self.assertEqual(find_chat_log([str(a), None, str(b)]), b / "Entropia Universe" / "chat.log")
            (a / "Entropia Universe").mkdir(parents=True)
            (a / "Entropia Universe" / "chat.log").write_bytes(b"")
            self.assertEqual(find_chat_log([str(a), str(b)]), a / "Entropia Universe" / "chat.log")
            self.assertIsNone(find_chat_log([str(Path(d, "nope"))]))


class LiveLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "chat.log"
        self.log.write_bytes(b"")
        self.db = Path(self.tmp.name) / "tracker.db"
        self.tracked = tracked_items(load_catalog())
        self.snap = InventorySnapshot([InventoryRow("1", "Animal Oil Residue", 100, "CARRIED", "0")])

    def new(self, path=None):
        return LiveLog(self.db, path or self.log, self.tracked)

    def append(self, text):
        with open(self.log, "ab") as fh:
            fh.write(text.encode("utf-8"))

    def test_no_backfill_no_double_count_across_polls_restart_and_partial_lines(self):
        self.append(got("Animal Oil Residue", 50))          # between export and import: never counted
        lv = self.new()
        lv.reset(self.snap)
        lv.poll()
        self.assertEqual(lv.pickups(), {})
        self.append("".join(NOT_RECEIPTS) + got("Animal Oil Residue", 12) + got("Corria Timber", 3)
                    + "2026-11-20 12:01:00 [System] [] You received Soft Hide x (")   # partial line
        lv.poll()
        lv.poll()                                          # same bytes twice: no double count
        self.assertEqual(lv.pickups(), {"Animal Oil Residue": 12, "Corria Timber": 3})
        self.assertEqual(lv.status, "live")
        restarted = self.new()                             # app restart: continues from the cursor
        restarted.poll()
        self.assertEqual(restarted.pickups(), {"Animal Oil Residue": 12, "Corria Timber": 3})
        self.append("7) Value: 0.07 PED\r\n")              # partial line completed
        restarted.poll()
        self.assertEqual(restarted.pickups()["Soft Hide"], 7)
        self.assertEqual(restarted.baseline().rows[0].quantity, 100)   # baseline untouched

    def test_crash_mid_transaction_rolls_back_then_counts_once(self):
        lv = self.new()
        lv.reset(self.snap)
        self.append(got("Animal Oil Residue", 5))
        with mock.patch.object(LiveLog, "_write_cursor", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                lv.poll()
        self.assertEqual(lv.pickups(), {})                 # pickups + cursor rolled back together
        lv.poll()
        lv.poll()
        self.assertEqual(lv.pickups(), {"Animal Oil Residue": 5})

    def test_cursor_inside_a_line_at_import_skips_that_ambiguous_line(self):
        self.append("2026-11-20 12:00:00 [System] [] You received Animal Oil Residue x (")
        lv = self.new()
        lv.reset(self.snap)
        self.append("8) Value: 0.08 PED\r\n" + got("Animal Oil Residue", 2))
        lv.poll()
        self.assertEqual(lv.pickups(), {"Animal Oil Residue": 2})

    def test_rotation_and_truncation_reanchor_without_credit_and_flag_gap(self):
        lv = self.new()
        lv.reset(self.snap)
        self.append(got("Animal Oil Residue", 4))
        lv.poll()
        os.remove(self.log)                                # replaced file with old-looking content
        self.append(got("Animal Oil Residue", 40) + got("Animal Oil Residue", 41))
        lv.poll()
        self.assertEqual((lv.status, lv.pickups()), ("gap", {"Animal Oil Residue": 4}))
        self.append(got("Animal Oil Residue", 1))
        lv.poll()
        self.assertEqual((lv.status, lv.pickups()), ("gap", {"Animal Oil Residue": 5}))
        with open(self.log, "r+b") as fh:                  # truncated then rewritten
            fh.truncate(0)
        self.append(got("Animal Oil Residue", 30))
        lv.poll()
        self.assertEqual(lv.pickups(), {"Animal Oil Residue": 5})
        lv.reset(self.snap)                                # reimport: clean slate, gap cleared
        self.assertEqual((lv.status, lv.pickups()), ("live", {}))
        self.append(got("Animal Oil Residue", 2))
        lv.poll()
        self.assertEqual((lv.status, lv.pickups()), ("live", {"Animal Oil Residue": 2}))

    def test_migration_anchor_flags_gap_and_missing_unsupported_logs_are_safe(self):
        self.append(got("Animal Oil Residue", 9))
        lv = self.new()
        lv.reset(self.snap, gap=True)                      # old snapshot: no replay, reimport advised
        lv.poll()
        self.assertEqual((lv.status, lv.pickups()), ("gap", {}))
        missing = self.new(Path(self.tmp.name) / "absent.log")
        missing.poll()
        self.assertEqual(missing.status, "unreadable")
        self.log.write_bytes("﻿x".encode("utf-16"))
        lv.poll()
        self.assertEqual(lv.status, "unsupported")
        off = LiveLog(self.db, None, self.tracked)
        off.poll()
        self.assertEqual(off.status, "off")

    def test_quiet_flag_after_silence(self):
        lv = self.new()
        lv.reset(self.snap)
        with mock.patch.object(live.time, "time", return_value=live.time.time() + live.QUIET_SECONDS + 5):
            lv.poll()
        self.assertTrue(lv.quiet)

    def test_database_and_status_hold_no_chat_text(self):
        lv = self.new()
        lv.reset(self.snap)
        self.append("".join(NOT_RECEIPTS) + got("Animal Oil Residue", 1))
        lv.poll()
        with closing(sqlite3.connect(self.db)) as db:
            dump = "\n".join(db.iterdump())
        self.assertNotIn(SECRET, dump)
        self.assertNotIn("Globals", dump)
        self.assertNotIn(SECRET, lv.status)

    def test_receipts_count_only_when_carried_in_scope_and_add_no_location(self):
        snap = InventorySnapshot([InventoryRow("1", "Soft Hide", 10, "STORAGE (Arkadia)", "0")])
        snap.carried_pickups = {"Soft Hide": 6}
        self.assertEqual(snap.quantity("Soft Hide", {"STORAGE (Arkadia)"}), 10)
        self.assertEqual(snap.quantity("Soft Hide", {"STORAGE (Arkadia)", "CARRIED"}), 16)
        self.assertEqual(list(snap.locations()), ["STORAGE (Arkadia)"])
        self.assertNotIn("carried_pickups", str(snap.to_json()))


if __name__ == "__main__":
    unittest.main()
