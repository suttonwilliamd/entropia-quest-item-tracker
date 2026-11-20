import json
import tempfile
import unittest
from pathlib import Path

from eqit.catalog import CatalogError, load_catalog, parse_catalog
from eqit.inventory import InventoryImportError, decode_bytes, parse_inventory_text
from eqit.live import tracked_items
from eqit.state import clamp_opacity, load_state, save_state
from eqit.tracker import (action_text, clamp_stage, hud_lines, progress_text, reconcile_scope,
                          shortfall_summary, stage_status)

from tests.helpers import BASE_ROWS, HEADER, make_tsv


def parse(data: bytes):
    return parse_inventory_text(decode_bytes(data))


class InventoryImportTests(unittest.TestCase):
    def test_quoted_tsv_with_bom_preserves_names_quantities_locations(self):
        rows = BASE_ROWS + [["1010", 'Odd, "Quoted"\tName', "3", "", "CARRIED", "0"]]
        inv = parse(make_tsv(rows))
        self.assertEqual(len(inv.rows), 10)
        self.assertEqual(inv.rows[-1].name, 'Odd, "Quoted"\tName')
        self.assertEqual(inv.quantity("Corria Veneer"), 1500 + 300 + 9999)
        self.assertEqual(list(inv.locations())[:3], ["CARRIED", "STORAGE (Arkadia)", "STORAGE (Calypso)"])
        self.assertIn("STORAGE", inv.locations())  # bare STORAGE kept distinct
        self.assertFalse(hasattr(inv.rows[0], "value"))

    def test_utf16_and_csv_variants(self):
        text = make_tsv(BASE_ROWS, bom=False).decode("utf-8")
        self.assertEqual(len(parse(b"\xff\xfe" + text.encode("utf-16-le")).rows), len(BASE_ROWS))
        self.assertEqual(len(parse(make_tsv(BASE_ROWS, delimiter=",")).rows), len(BASE_ROWS))

    def test_csv_with_bom_crlf_and_quoted_names(self):
        rows = [["1", 'Wool, "Fine"', "3", "", "CARRIED", "0"], ["2", "Wool", "4", "", "CARRIED", "0"]]
        inv = parse(make_tsv(rows, delimiter=","))  # BOM + CRLF + QUOTE_ALL
        self.assertEqual([(r.name, r.quantity) for r in inv.rows], [('Wool, "Fine"', 3), ("Wool", 4)])

    def test_errors_are_actionable_and_never_echo_contents(self):
        secret = "ZZSECRETZZ"
        cases = {
            "header": make_tsv([], header=["Id", secret, "Quantity", "Value(PED)", "Container", "ContainerRefId"]),
            "order": make_tsv([], header=["Name", "Id", "Quantity", "Value(PED)", "Container", "ContainerRefId"]),
            "utf8 bom + bad bytes": b"\xef\xbb\xbf" + make_tsv([[secret]], bom=False) + b"\xff\xfe\x81",
            "odd utf16": b"\xff\xfe" + "Id\tName".encode("utf-16-le") + b"\x00",
            "rows": make_tsv([[secret, secret, secret + "1", "1", secret, "0"],     # quantity type
                              ["2", secret, "-3", "", "CARRIED", "0"],              # negative
                              ["3", secret, "", "", "CARRIED", "0"],                # missing qty
                              ["4", "", "1", "", secret, "0"],                      # empty name
                              ["5", secret, "1", "", secret, "0", secret]]),         # row shape
        }
        expected = {"header": "missing column(s): Name", "order": "out of order",
                    "utf8 bom + bad bytes": "encoding", "odd utf16": "encoding", "rows": "5 problem(s)"}
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(InventoryImportError) as ctx:
                parse(data)
            text = " ".join([str(ctx.exception), *ctx.exception.errors])
            self.assertIn(expected[label], text)
            self.assertNotIn(secret, text)
        self.assertEqual([e.split(":")[0] for e in ctx.exception.errors], [f"Line {i}" for i in range(2, 7)])

    def test_duplicate_id_warning_does_not_echo_id(self):
        inv = parse(make_tsv([["ZZID99", "Wool", "1", "", "CARRIED", "0"], ["ZZID99", "Wool", "1", "", "CARRIED", "0"]]))
        self.assertNotIn("ZZID99", " ".join(inv.warnings))

    def test_wrong_header_rejected(self):
        with self.assertRaises(InventoryImportError) as ctx:
            parse(make_tsv(BASE_ROWS, header=["Id", "Name", "Qty", "Value(PED)", "Container", "ContainerRefId"]))
        self.assertIn("missing column(s): Quantity", str(ctx.exception))
        self.assertNotIn("Qty", str(ctx.exception))
        with self.assertRaises(InventoryImportError):
            parse(b"")

    def test_bad_rows_reject_whole_file_with_line_numbers(self):
        rows = BASE_ROWS + [["2001", "Wool", "12.5", "", "CARRIED", "0"],
                            ["2002", "", "1", "", "CARRIED", "0"],
                            ["2003", "Wool"]]
        with self.assertRaises(InventoryImportError) as ctx:
            parse(make_tsv(rows))
        self.assertEqual(len(ctx.exception.errors), 3)
        self.assertTrue(ctx.exception.errors[0].startswith(f"Line {len(BASE_ROWS) + 2}:"))
        self.assertIn("nothing was imported", str(ctx.exception))

    def test_missing_trailing_ref_and_duplicate_id_are_tolerated(self):
        data = b'"Id"\t"Name"\t"Quantity"\t"Value(PED)"\t"Container"\t"ContainerRefId"\r\n' \
               b'"1"\t"Wool"\t"3"\t"0.1"\t"CARRIED"\r\n"1"\t"Wool"\t"2"\t"0.1"\t"CARRIED"\t"0"\r\n'
        inv = parse(data)
        self.assertEqual(inv.quantity("Wool"), 5)
        self.assertEqual(len(inv.warnings), 1)

    def test_nested_container_inherits_parent_location(self):
        rows = [["50", "Some Box", "1", "", "STORAGE (Arkadia)", "0"],
                ["51", "Wool", "9", "", "Some Box", "50"],
                ["52", "Wool", "4", "", "Other Box", "999"]]  # ref matches nothing: label kept
        inv = parse(make_tsv(rows))
        self.assertEqual(inv.quantity("Wool", {"STORAGE (Arkadia)"}), 9)
        self.assertIn("Other Box", inv.locations())


class ScopeAndProgressTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog()
        self.viceroy = self.catalog.get("viceroy")
        self.inv = parse(make_tsv(BASE_ROWS))

    def test_viceroy_catalog_order_and_source(self):
        self.assertEqual([s.name for s in self.viceroy.stages],
                         ["Arm Guards", "Foot Guards", "Gloves", "Harness", "Shin Guards", "Thigh Guards", "Helmet"])
        self.assertEqual(self.viceroy.source, "https://www.arkadiaarchive.com/viceroyarmor")
        self.assertEqual((self.viceroy.npc, self.viceroy.npc_location), ("Bill", "Celeste Quarry"))
        first = {r.item: r.quantity for r in self.viceroy.stages[0].hand_in}
        self.assertEqual(first, {"Corria Veneer": 2000, "Soft Leather": 100, "Paint Can (Yellow)": 400,
                                 "Electronic Stabilizing Component": 100, "Animal Oil Residue": 800})
        self.assertEqual(self.viceroy.stages[4].hand_in[-1].quantity, 70000)
        self.assertEqual(self.catalog.refining["Miluca Veneer"].output_qty, 5)

    def test_first_import_scopes_local_only_au_by_alias_only(self):
        scope, known, new = reconcile_scope(None, [], list(self.inv.locations()), [])
        self.assertEqual(set(scope), {"CARRIED", "STORAGE (Arkadia)"})
        scope, _, _ = reconcile_scope(None, [], list(self.inv.locations()), ["STORAGE (Arkadia Underground)"])
        self.assertIn("STORAGE (Arkadia Underground)", scope)
        self.assertNotIn("STORAGE", scope)
        self.assertNotIn("STORAGE (Calypso)", scope)

    def test_reimport_keeps_user_choices(self):
        scope, known, _ = reconcile_scope(None, [], ["CARRIED", "STORAGE (Arkadia)"], [])
        scope.remove("CARRIED")  # user unticks CARRIED
        scope, known, new = reconcile_scope(scope, known, ["CARRIED", "STORAGE (Arkadia)", "STORAGE (Calypso)"], [])
        self.assertEqual(scope, ["STORAGE (Arkadia)"])
        self.assertEqual(new, ["STORAGE (Calypso)"])

    def test_have_need_uses_scope_and_keeps_refinable_separate(self):
        scope = {"CARRIED", "STORAGE (Arkadia)"}
        st = {s.item: s for s in stage_status(self.catalog, self.viceroy.stages[0], self.inv, scope)}
        self.assertEqual(st["Corria Veneer"].have, 1800)        # Calypso 9999 excluded
        self.assertEqual(st["Corria Veneer"].refinable, 80)     # 4 Corria x 20
        self.assertEqual(st["Corria Veneer"].short, 200)
        self.assertEqual(st["Corria Veneer"].short_after_refining, 120)
        self.assertEqual(st["Soft Leather"].have, 0)            # 40 in bare STORAGE not counted
        self.assertEqual(st["Soft Leather"].refinable, 2)       # 7 Soft Hide -> 2 whole batches
        self.assertEqual(st["Animal Oil Residue"].short, 0)
        self.assertEqual(st["Paint Can (Yellow)"].have, 0)      # AU not aliased
        self.assertIsNone(stage_status(self.catalog, self.viceroy.stages[0], None, scope)[0].have)

    def test_clamp_stage(self):
        self.assertEqual(clamp_stage(self.viceroy, 3), 3)
        self.assertEqual(clamp_stage(self.viceroy, 99), 7)  # complete
        self.assertEqual(clamp_stage(self.viceroy, "x"), 0)

    def test_hud_lines_are_concise_and_private(self):
        lines = hud_lines(self.catalog, self.viceroy, 0, self.inv, {"CARRIED", "STORAGE (Arkadia)"})
        self.assertEqual(lines[1].text, "1/7 Arm Guards")
        self.assertEqual(len(lines), 7)
        blob = " ".join(f"{l.text} {l.value}" for l in lines)
        for secret in ("PED", "STORAGE", "CARRIED", "Celeste", "Bill", "Shiny Unrelated", "+"):
            self.assertNotIn(secret, blob)  # no values, locations, NPC or refinable
        self.assertTrue(all(len(l.text) <= 32 for l in lines))
        corria = lines[2]
        self.assertEqual((corria.value, corria.mark, corria.kind), ("1,880/2,000", "refine", "short"))
        self.assertEqual((lines[6].value, lines[6].mark, lines[6].kind), ("800/800", "", "ok"))

    def test_exact_export_names_arm_guards_regression(self):
        """Synthetic Arm Guards regression using exact catalog item names."""
        ark = "STORAGE (Arkadia)"
        inv = parse(make_tsv([
            ["1", "Corria Timber", "130", "1.0", ark, "0"],
            ["2", "Soft Hide", "77", "1.0", ark, "0"],           # 77 // 3 = 25 whole batches
            ["3", "Paint Can (Yellow)", "50", "1.0", "CARRIED", "0"],
            ["4", "Electronic Stabilizing Component", "10", "1.0", "CARRIED", "0"],
            ["5", "Arkace Timber", "21", "1.0", ark, "0"],
        ]))
        scope = {"CARRIED", ark}
        sts = stage_status(self.catalog, self.viceroy.stages[0], inv, scope)
        self.assertEqual([s.short for s in sts], [2000, 100, 350, 90, 800])
        self.assertEqual([s.short_after_refining for s in sts], [0, 75, 350, 90, 800])
        self.assertEqual((sts[0].refinable, sts[1].refinable), (2600, 25))
        self.assertEqual(action_text(sts[0]), "Refine 100 Corria Timber → 2000")
        self.assertEqual(action_text(sts[1]), "Refine 75 Soft Hide → 25; gather 75")
        self.assertEqual(shortfall_summary(sts), (
            "Short after refining: Soft Leather 75 · Paint Can (Yellow) 350 · "
            "Electronic Stabilizing Component 90 · Animal Oil Residue 800",
            "Refine 100 Corria Timber, 75 Soft Hide, then gather the rest"))
        # Progress credits refinable (capped to need) but is always marked as refine, never physical.
        self.assertEqual(progress_text(sts[0]), "2,000/2,000 refine (0 physical)")
        self.assertEqual(progress_text(sts[1]), "25/100 refine (0 physical)")
        self.assertEqual(progress_text(sts[2]), "50/400")
        self.assertEqual((sts[0].covered, sts[0].refine_credit, sts[0].short), (2000, 2000, 2000))
        hud = hud_lines(self.catalog, self.viceroy, 0, inv, scope)[2:]
        self.assertEqual([(l.value, l.mark, l.kind) for l in hud], [
            ("2,000/2,000", "refine", "refine"), ("25/100", "refine", "short"), ("50/400", "", "short"),
            ("10/100", "", "short"), ("0/800", "", "short")])
        shin = stage_status(self.catalog, self.viceroy.stages[4], inv, scope)
        self.assertEqual(shin[0].refinable, 210)  # 21 Arkace Timber x 10
        self.assertEqual({r.input for o, r in self.catalog.refining.items() if o.endswith("Veneer")},
                         {"Corria Timber", "Miluca Timber", "Hekia Timber", "Arkace Timber"})

    def test_viceroy_upgrade_goals(self):
        parts = [s.name for s in self.viceroy.stages]
        self.assertTrue(self.viceroy.ordered)
        expect = {  # id: (nanites, ingot, ingot qty per piece, prior variant, donor, ingot total)
            "viceroy_adjusted": ("Adjusted Nanites", "Ospra Ingot", 30, "base", "Musca", 210),
            "viceroy_improved": ("Improved Nanites", "Banite Ingot", 10, "Adjusted", "Canis", 70),
            "viceroy_modified": ("Modified Nanites", "Zulax Ingot", 4, "Improved", "Lupus", 28),
        }
        for qid, (nan, ingot, iq, prior, donor, ingot_total) in expect.items():
            q = self.catalog.get(qid)
            self.assertFalse(q.ordered)
            self.assertEqual((q.npc, q.npc_location), ("Bill", "Celeste Quarry"))
            self.assertEqual([s.name for s in q.stages], parts)            # base order, display only
            self.assertEqual({q.source, *q.more_sources}, {
                "https://entropia-universe-mmorpg.blogspot.com/2017/04/upgrading-viceroy-armor.html",
                "https://arkadiaforum.com/threads/planet-arkadia-update-2016-5-release-notes-7th-december-2016.13882/",
                "https://www.arkadiaarchive.com/viceroyarmor"})
            totals = {}
            for s in q.stages:
                self.assertEqual([(r.item, r.quantity) for r in s.hand_in],
                                 [(nan, 2), ("Bismuth Plating", 100), (ingot, iq)])
                prior_c, donor_c = s.manual
                self.assertTrue(prior_c.text.startswith(f"Full-TT {prior} Viceroy {s.name} ("))
                self.assertTrue(donor_c.text.startswith(f"Full-TT {donor} (L) {s.name.lower()} piece ("))
                for c in s.manual:
                    self.assertIn("gender", c.text)
                    self.assertIn("do not mix normal and Skullcandy", c.text)
                    self.assertIn("full TT", c.hud)
                for r in s.hand_in:
                    totals[r.item] = totals.get(r.item, 0) + r.quantity
            self.assertEqual(totals, {nan: 14, "Bismuth Plating": 700, ingot: ingot_total})
        items = tracked_items(self.catalog)
        self.assertTrue({"Adjusted Nanites", "Bismuth Plating", "Zulax Ingot"} <= items)
        self.assertFalse(any("Viceroy" in i or "(L)" in i for i in items))   # gear is manual only

    def test_upgrade_hud_label_and_manual_lines(self):
        adj = self.catalog.get("viceroy_adjusted")
        lines = hud_lines(self.catalog, adj, 0, self.inv, {"CARRIED"}, {"prior"})
        self.assertEqual(lines[1].text, "Arm Guards · 1 of 7")       # no '1/7' sequence claim
        self.assertEqual([(l.text, l.value) for l in lines[2:4]],
                         [("Base Viceroy, full TT", "✓"), ("Musca (L), full TT", "check")])
        self.assertEqual([l.text for l in lines[4:]], ["Adjusted Nanites", "Bismuth Plating", "Ospra Ingot"])
        self.assertEqual(hud_lines(self.catalog, self.viceroy, 0, self.inv, set())[1].text, "1/7 Arm Guards")

    def test_catalog_requires_source_and_unique_items(self):
        good = {"schema": 1, "quests": [{"id": "q", "name": "Q", "source": "s",
                                          "stages": [{"name": "A", "hand_in": [{"item": "X", "quantity": 1}]}]}]}
        parse_catalog(good)
        no_source = json.loads(json.dumps(good)); del no_source["quests"][0]["source"]
        dup = json.loads(json.dumps(good)); dup["quests"][0]["stages"][0]["hand_in"].append({"item": "X", "quantity": 2})
        for bad in (no_source, dup):
            with self.assertRaises(CatalogError):
                parse_catalog(bad)


class StateTests(unittest.TestCase):
    def test_roundtrip_clamp_and_corrupt_file_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            s = load_state(p)
            s["hud_opacity"] = 0.4
            save_state(p, s)
            self.assertEqual(load_state(p)["hud_opacity"], 0.4)
            self.assertEqual(clamp_opacity(5), 1.0)
            self.assertEqual(clamp_opacity(0.01), 0.25)
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(load_state(p)["hud_opacity"], 0.85)
            self.assertEqual(len(list(Path(d).glob("state.corrupt-*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
