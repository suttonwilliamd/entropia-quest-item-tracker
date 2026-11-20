"""Two-window wiring checks with withdrawn (never mapped) Tk windows.

No window is shown, focused or raised. Set EQIT_SKIP_GUI_TESTS=1 to skip.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import BASE_ROWS, make_tsv

try:
    import tkinter as tk
    from eqit.app import HUD_TITLE, TrackerApp
    from eqit.state import load_state
except ImportError:  # pragma: no cover
    tk = None


@unittest.skipIf(tk is None or os.environ.get("EQIT_SKIP_GUI_TESTS"), "GUI wiring tests skipped")
class GuiWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_file = self.dir / "state.json"
        self.export = self.dir / "inventory.tsv"
        self.export.write_bytes(make_tsv(BASE_ROWS))
        self.roots = []
        # The real system clipboard must never be touched by tests.
        guard = mock.patch.object(tk.Misc, "clipboard_get", side_effect=AssertionError("real clipboard read"))
        guard.start()
        self.addCleanup(guard.stop)

    def tearDown(self):
        for r in self.roots:
            r.destroy()
        self.tmp.cleanup()

    def new_app(self) -> TrackerApp:
        root = tk.Tk()
        root.withdraw()  # never mapped
        self.roots.append(root)
        return TrackerApp(root, self.state_file)

    def test_two_separate_windows_hud_private_by_default(self):
        app = self.new_app()
        self.assertIsInstance(app.hud.top, tk.Toplevel)
        self.assertIsNot(app.hud.top, app.root)
        self.assertEqual(app.hud.top.title(), HUD_TITLE)
        self.assertEqual(app.hud.top.state(), "withdrawn")
        self.assertFalse(app.hud.visible)

    @unittest.skipUnless(os.name == "nt", "Win32 style check")
    def test_hud_has_no_native_chrome_main_window_does(self):
        import ctypes
        app = self.new_app()
        app.root.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetWindowLongW.restype = ctypes.c_long
        CAPTION, SYSMENU, MINBOX, MAXBOX = 0x00C00000, 0x00080000, 0x00020000, 0x00010000

        def style(win):
            hwnd = int(win.wm_frame(), 16)
            self.assertFalse(user32.IsWindowVisible(hwnd))       # never shown
            return user32.GetWindowLongW(hwnd, -16) & 0xFFFFFFFF

        hud, main = style(app.hud.top), style(app.root)
        for bit in (CAPTION, SYSMENU, MINBOX, MAXBOX):
            self.assertEqual(hud & bit, 0, hex(bit))
            self.assertEqual(main & bit, bit, hex(bit))

    def test_drag_moves_and_saves_without_hiding_x_hides_only(self):
        from types import SimpleNamespace as Ev
        app = self.new_app()
        hud, hidden = app.hud, []
        hud.on_hide = lambda: hidden.append(1)
        hud.position = "+100+200"
        hud._drag_start(Ev(x_root=110, y_root=205))
        hud._drag_move(Ev(x_root=160, y_root=245))
        hud._drag_end(Ev(x_root=160, y_root=245))
        self.assertEqual(hud.position, "+150+240")
        self.assertEqual(hidden, [])                             # dragging never hides
        app.save_now()
        self.assertEqual(load_state(self.state_file)["hud_position"], "+150+240")
        x = hud.hide_button
        self.assertNotIn("<ButtonPress-1>", x.bind())            # x is not a drag handle
        hud._hide_release(Ev(widget=x, x=-40, y=3))              # press on x, release elsewhere
        self.assertEqual(hidden, [])
        hud._hide_release(Ev(widget=x, x=2, y=2))                # click on x
        self.assertEqual(hidden, [1])
        self.assertFalse(hud.visible)
        self.assertTrue(app.root.winfo_exists())                 # hides HUD, not the app

    def test_hud_footprint_is_small(self):
        app = self.new_app()
        app.import_path(str(self.export))
        w, h = app.hud.requested_size()
        scale = 96 / (72 * float(app.root.tk.call("tk", "scaling")))  # normalise to 100% DPI
        self.assertLessEqual(w * scale, 320, (w, h))
        self.assertLessEqual(h * scale, 200, (w, h))
        labels = [c.cget("text") for c in app.hud.frame.winfo_children() if isinstance(c, tk.Label)]
        self.assertIn("Viceroy Armor", labels)
        self.assertIn("1/7 Arm Guards", labels)
        self.assertFalse(any(str(t).startswith("+") for t in labels))  # no refinable on HUD

    def test_refine_credit_marked_in_hidden_hud_and_main(self):
        ark = "STORAGE (Arkadia)"
        self.export.write_bytes(make_tsv([["1", "Corria Timber", "130", "1", ark, "0"],
                                          ["2", "Soft Hide", "77", "1", ark, "0"]]))
        app = self.new_app()
        app.import_path(str(self.export))
        rows = {app.tree.item(i, "text"): app.tree.item(i, "values") for i in app.tree.get_children()}
        self.assertEqual(rows["Corria Veneer"][:2], ("2,000/2,000 refine (0 physical)", "0"))
        self.assertEqual(rows["Soft Leather"][:2], ("25/100 refine (0 physical)", "75"))
        self.assertEqual(rows["Corria Veneer"][3:7], ("2000", "0", "2000", "2600"))  # need/have/short/+refinable
        grid = {(int(w.grid_info()["row"]), int(w.grid_info()["column"])): w.cget("text")
                for w in app.hud.frame.winfo_children() if isinstance(w, tk.Label)}
        self.assertEqual((grid[(2, 1)], grid[(2, 2)]), ("2,000/2,000", "refine"))
        self.assertEqual((grid[(3, 1)], grid[(3, 2)]), ("25/100", "refine"))
        self.assertNotIn((4, 2), grid)                           # Paint Can: no refine mark
        self.assertFalse(app.hud.visible)
        w, h = app.hud.requested_size()
        scale = 96 / (72 * float(app.root.tk.call("tk", "scaling")))
        self.assertLessEqual((w * scale, h * scale), (320, 200))

    def test_hud_stage_arrows_browse_without_changing_committed_stage(self):
        from types import SimpleNamespace as Ev
        app = self.new_app()
        app.import_path(str(self.export))
        app.save_now()
        saved = self.state_file.read_bytes()
        main_rows = [app.tree.item(i, "values") for i in app.tree.get_children()]
        hud = app.hud

        def stage_row():
            return {int(w.grid_info()["column"]): w for w in hud.frame.grid_slaves(row=1)}

        def click(button):
            hud._nav_release(Ev(widget=button, x=1, y=1), -1 if button is hud.prev_button else 1)

        def item_texts():
            return [w.cget("text") for w in hud.frame.grid_slaves(column=0) if int(w.grid_info()["row"]) >= 2][::-1]

        row = stage_row()
        self.assertEqual((row[0].cget("text"), row[0].grid_info()["sticky"]), ("1/7 Arm Guards", "w"))
        self.assertEqual(row[1].grid_info()["sticky"], "e")      # arrows right-aligned
        self.assertEqual((hud.can_prev, hud.can_next), (False, True))
        click(hud.prev_button)                                   # disabled at first stage: no-op
        self.assertEqual(stage_row()[0].cget("text"), "1/7 Arm Guards")

        click(hud.next_button)
        self.assertEqual(stage_row()[0].cget("text"), "2/7 Foot Guards")
        self.assertEqual(item_texts()[0], "Miluca Veneer")
        self.assertIn("0/300", [w.cget("text") for w in hud.frame.grid_slaves(column=1)
                                if isinstance(w, tk.Label)])                        # Socket II count
        for _ in range(10):
            if hud.can_next:
                click(hud.next_button)
        self.assertEqual(stage_row()[0].cget("text"), "7/7 Helmet")
        self.assertEqual((hud.can_prev, hud.can_next), (True, False))
        click(hud.next_button)                                   # disabled at last stage: no-op
        self.assertEqual(stage_row()[0].cget("text"), "7/7 Helmet")

        # Committed stage, main table and saved state untouched by browsing.
        self.assertEqual(app.stage_index, 0)
        self.assertEqual(app.state["stage_by_quest"], {})
        self.assertEqual([app.tree.item(i, "values") for i in app.tree.get_children()], main_rows)
        app.save_now()
        self.assertEqual(self.state_file.read_bytes(), saved)

        app.set_stage(2)                                         # committing a stage resets the browse
        self.assertEqual(stage_row()[0].cget("text"), "3/7 Gloves")
        self.assertFalse(hud.visible)
        w, h = hud.requested_size()
        scale = 96 / (72 * float(app.root.tk.call("tk", "scaling")))
        self.assertLessEqual((w * scale, h * scale), (320, 200))

    @unittest.skipUnless(os.name == "nt", "Win32 rect check")
    def test_arrow_hitboxes_fixed_for_repeated_same_point_clicks(self):
        """Measures real (hidden) Win32 rects; clicks are simulated by calling the release
        handler with coordinates derived from one fixed screen point. No pointer events."""
        import ctypes
        import ctypes.wintypes as wt
        from types import SimpleNamespace as Ev
        user32 = ctypes.windll.user32

        def rect(widget):
            r = wt.RECT()
            hwnd = int(widget.wm_frame(), 16) if isinstance(widget, tk.Toplevel) else widget.winfo_id()
            self.assertTrue(user32.GetWindowRect(hwnd, ctypes.byref(r)))
            return r.left, r.top, r.right, r.bottom

        for scaling in (96 / 72, 144 / 72):                     # 100% and 150% Windows DPI
            for corner in ("normal", "bottom-right edge"):
                with self.subTest(scaling=scaling, corner=corner):
                    root = tk.Tk()
                    root.withdraw()
                    self.roots.append(root)
                    root.tk.call("tk", "scaling", scaling)
                    app = TrackerApp(root, self.state_file)
                    app.import_path(str(self.export))
                    app.set_stage(3)                             # committed mid-quest stage
                    hud = app.hud
                    w = rect(hud.top)[2] - rect(hud.top)[0]
                    hud.position = ("+200+150" if corner == "normal" else
                                    f"+{root.winfo_screenwidth() - w}+{root.winfo_screenheight() - 40}")
                    app.render_hud()
                    root.update_idletasks()
                    app.save_now()
                    saved = self.state_file.read_bytes()
                    top0 = rect(hud.top)
                    slots = (rect(hud.prev_button), rect(hud.next_button))

                    def press(button_attr, delta, point):
                        root.update_idletasks()
                        self.assertEqual(rect(hud.top)[:3], top0[:3])          # origin + width fixed
                        self.assertEqual((rect(hud.prev_button), rect(hud.next_button)), slots)
                        for lbl in hud.frame.winfo_children():
                            if isinstance(lbl, tk.Label):              # nothing clipped at this DPI
                                l, t, r, b = rect(lbl)
                                self.assertGreaterEqual(r - l, lbl.winfo_reqwidth(), lbl.cget("text"))
                        button = getattr(hud, button_attr)
                        l, t, r, b = rect(button)
                        self.assertTrue(l <= point[0] < r and t <= point[1] < b)  # same point, same button
                        hud._nav_release(Ev(widget=button, x=point[0] - l, y=point[1] - t), delta)
                        return app.hud_stage

                    nxt = ((slots[1][0] + slots[1][2]) // 2, (slots[1][1] + slots[1][3]) // 2)
                    prv = ((slots[0][0] + slots[0][2]) // 2, (slots[0][1] + slots[0][3]) // 2)
                    self.assertEqual([press("next_button", 1, nxt) for _ in range(5)], [4, 5, 6, 6, 6])
                    self.assertEqual([press("prev_button", -1, prv) for _ in range(9)], [5, 4, 3, 2, 1, 0, 0, 0, 0])
                    self.assertEqual([press("next_button", 1, nxt) for _ in range(8)], [1, 2, 3, 4, 5, 6, 6, 6])
                    self.assertEqual(app.stage_index, 3)                    # browsing never commits
                    app.save_now()
                    self.assertEqual(self.state_file.read_bytes(), saved)
                    self.assertFalse(hud.visible)

    def test_main_leads_with_after_refining_and_details_collapsed(self):
        app = self.new_app()
        app.import_path(str(self.export))
        self.assertTrue(app.headline.get().startswith("Short after refining: Corria Veneer 120"))
        self.assertTrue(app.action.get().startswith("Refine 4 Corria Timber"))
        self.assertEqual(app.details.winfo_manager(), "")        # collapsed
        self.assertEqual(tuple(app.tree["displaycolumns"]), app.MAIN_COLUMNS)
        app.toggle_details()
        self.assertEqual(app.details.winfo_manager(), "grid")
        self.assertNotEqual(tuple(app.tree["displaycolumns"]), app.MAIN_COLUMNS)  # detail columns shown

    def test_opacity_applies_and_persists(self):
        app = self.new_app()
        app.set_opacity(0.55)
        self.assertAlmostEqual(float(app.hud.top.attributes("-alpha")), 0.55, places=2)
        app.save_now()
        self.assertEqual(load_state(self.state_file)["hud_opacity"], 0.55)
        app2 = self.new_app()
        self.assertAlmostEqual(float(app2.hud.top.attributes("-alpha")), 0.55, places=2)
        self.assertFalse(app2.hud.visible)

    def test_import_reimport_malformed_and_stage_progress(self):
        app = self.new_app()
        ok, _ = app.import_path(str(self.export))
        self.assertTrue(ok)
        app.set_stage(3)                      # mid-quest: Harness
        app.loc_list.selection_clear(0)       # untick CARRIED
        app.set_scope_from_list()
        self.assertNotIn("CARRIED", app.scope)

        rows = [r[:] for r in BASE_ROWS]
        rows[0][2] = "999999"                 # plenty of everything: must not advance stage
        self.export.write_bytes(make_tsv(rows))
        ok, _ = app.reimport()
        self.assertTrue(ok)
        self.assertEqual(app.stage_index, 3)
        self.assertNotIn("CARRIED", app.scope)

        for bad in (b"ZZSECRETZZ garbage\n", b"\xef\xbb\xbf\xff\xfe ZZSECRETZZ", b""):
            self.export.write_bytes(bad)
            ok, msg = app.reimport()
            self.assertFalse(ok)
            self.assertNotIn("ZZSECRETZZ", msg)
            self.assertEqual(app.inventory.rows[0].quantity, 999999)  # previous import kept

        app.save_now()
        app2 = self.new_app()
        self.assertEqual(app2.stage_index, 3)
        self.assertEqual(app2.inventory.rows[0].quantity, 999999)
        self.assertEqual(app2.tree.item(app2.tree.get_children()[0], "text"), "Hekia Veneer")

    def test_paste_inventory_injected_clipboard(self):
        app = self.new_app()
        app.import_path(str(self.export))
        app.set_stage(2)
        app.set_opacity(0.4)
        app.loc_list.selection_clear(0)
        app.set_scope_from_list()                   # user unticks CARRIED
        scope_before = app.scope

        clip = {"value": ""}
        app.read_clipboard = lambda: clip["value"]
        website_tsv = ("Id\tName\tQuantity\tValue(PED)\tContainer\tContainerRefId\r\n"
                       "1\tMiluca Veneer\t1500\t15.00\tSTORAGE (Arkadia)\t0\r\n")
        clip["value"] = website_tsv
        ok, msg = app.paste_inventory()
        self.assertTrue(ok, msg)
        self.assertEqual(app.inventory.quantity("Miluca Veneer"), 1500)
        self.assertEqual(app.inventory.source_path, "")
        self.assertEqual((app.stage_index, app.state["hud_opacity"]), (2, 0.4))
        self.assertEqual(app.scope, scope_before)
        self.assertIn("pasted", app.reimport()[1])

        good = app.inventory
        secret = "ZZSECRETZZ"
        bad_inputs = [f"{secret}\t{secret}\r\n", "Id\tName\tQuantity\tValue(PED)\tContainer\tContainerRefId\r\n"
                      f"1\t{secret}\tlots\t1\tCARRIED\t0\r\n", "", "   \r\n"]
        for value in bad_inputs:
            clip["value"] = value
            ok, msg = app.paste_inventory()
            self.assertFalse(ok)
            self.assertNotIn(secret, msg)
            self.assertIs(app.inventory, good)       # last good snapshot kept

        def unavailable():
            raise tk.TclError(f"CLIPBOARD selection doesn't exist {secret}")
        app.read_clipboard = unavailable
        ok, msg = app.paste_inventory()
        self.assertFalse(ok)
        self.assertIn("unavailable", msg)
        self.assertNotIn(secret, msg)
        self.assertIs(app.inventory, good)

        ok, _ = app.import_path(str(self.export))   # file import still works afterwards
        self.assertTrue(ok)
        self.assertEqual(app.stage_index, 2)

    def test_live_receipts_failed_import_reimport_and_hud(self):
        log = self.dir / "chat.log"
        line = "2026-11-20 12:00:00 [System] [] You received {} x ({}) Value: 0.10 PED\r\n"
        log.write_bytes(line.format("Animal Oil Residue", 999).encode())      # before import: ignored
        root = tk.Tk()
        root.withdraw()
        self.roots.append(root)
        app = TrackerApp(root, self.state_file, chat_log=log)
        app.import_path(str(self.export))
        app.poll_live()
        aor = lambda: next(app.tree.item(i, "values") for i in app.tree.get_children()
                           if app.tree.item(i, "text") == "Animal Oil Residue")
        self.assertEqual(aor()[4], "800")                                       # have now
        with open(log, "ab") as fh:
            fh.write(line.format("Animal Oil Residue", 25).encode())
        app.poll_live()
        self.assertEqual(aor()[4], "825")
        self.assertIn("+25 received since import (estimate, any source)", app.live_status.get())
        hud_text = " ".join(w.cget("text") for w in app.hud.frame.winfo_children() if isinstance(w, tk.Label))
        self.assertNotIn("Live", hud_text)
        self.assertNotIn("received", hud_text)

        self.export.write_bytes(b"garbage\n")                                   # failed import keeps all
        self.assertFalse(app.reimport()[0])
        self.assertEqual(app.live.pickups(), {"Animal Oil Residue": 25})
        app.poll_live()
        self.assertEqual(aor()[4], "825")

        app.loc_list.selection_clear(0)                                         # untick CARRIED
        app.set_scope_from_list()
        self.assertIn("tick CARRIED", app.live_status.get())

        self.export.write_bytes(make_tsv(BASE_ROWS))                            # fresh import: cleared
        self.assertTrue(app.reimport()[0])
        self.assertEqual(app.live.pickups(), {})
        self.assertIn("+0 received", app.live_status.get())

    def test_upgrade_migrates_old_snapshot_without_replay(self):
        from eqit.inventory import parse_inventory_text, decode_bytes
        from eqit.state import default_state, save_state
        old = default_state()
        old.update(hud_opacity=0.42, stage_by_quest={"viceroy": 2},
                   inventory=parse_inventory_text(decode_bytes(make_tsv(BASE_ROWS))).to_json())
        save_state(self.state_file, old)
        log = self.dir / "chat.log"
        log.write_bytes(b"2026-11-20 12:00:00 [System] [] You received Animal Oil Residue x (77) Value: 0.77 PED\r\n")
        root = tk.Tk()
        root.withdraw()
        self.roots.append(root)
        app = TrackerApp(root, self.state_file, chat_log=log)
        app.poll_live()
        self.assertEqual(len(app.inventory.rows), len(BASE_ROWS))
        self.assertEqual((app.stage_index, app.state["hud_opacity"]), (2, 0.42))
        self.assertEqual(app.live.pickups(), {})                                # no replay
        self.assertIn("reimport to resync", app.live_status.get())
        app.save_now()
        self.assertIsNone(load_state(self.state_file)["inventory"])             # now lives in tracker.db
        self.assertEqual(len(app.live.baseline().rows), len(BASE_ROWS))

    def test_upgrade_goals_progress_independent_and_manual_never_inferred(self):
        rows = BASE_ROWS + [["3001", "Adjusted Nanites", "5", "1", "CARRIED", "0"],
                            ["3002", "Viceroy Arm Guards (M)", "1", "50", "CARRIED", "0"],   # gear in export
                            ["3003", "Bismuth Plating", "999", "1", "STORAGE (Calypso)", "0"]]  # off-world
        self.export.write_bytes(make_tsv(rows))
        app = self.new_app()
        app.import_path(str(self.export))
        app.set_stage(3)                                           # base Viceroy: Harness
        app.set_quest("viceroy_adjusted")
        self.assertEqual(app.stage_index, 0)                       # own progress
        self.assertEqual(app.checked(app.quest, 0), set())         # gear row never ticks a check
        self.assertEqual(len(app._manual_vars), 2)
        self.assertIn("2 gear check(s) open", app.headline.get())
        rows_now = {app.tree.item(i, "text"): app.tree.item(i, "values") for i in app.tree.get_children()}
        self.assertEqual(rows_now["Adjusted Nanites"][0], "2/2")
        self.assertEqual(rows_now["Bismuth Plating"][4], "0")      # Calypso storage not counted

        app.set_stage(2)                                           # Gloves
        app.set_manual("prior", True)
        hud = {w.cget("text") for w in app.hud.frame.winfo_children() if isinstance(w, tk.Label)}
        self.assertTrue({"Gloves · 3 of 7", "Base Viceroy, full TT", "✓", "check"} <= hud)
        self.assertIn("1 gear check(s) open", app.headline.get())

        app.set_quest("viceroy_improved")
        self.assertEqual((app.stage_index, app.checked(app.quest, 2)), (0, set()))
        app.set_quest("viceroy")
        self.assertEqual(app.stage_index, 3)
        app.save_now()

        app2 = self.new_app()                                      # restart: all preserved
        self.assertEqual(app2.state["stage_by_quest"], {"viceroy": 3, "viceroy_adjusted": 2})  # improved untouched
        self.assertEqual(app2.state["manual_checks"], {"viceroy_adjusted": {"Gloves": ["prior"]}})
        app2.set_quest("viceroy_adjusted")
        widths = set()
        for i in range(7):                                         # fixed HUD width across pieces
            app2.browse_hud(1) if i else None
            widths.add(app2.hud.requested_size()[0])
        self.assertEqual(len(widths), 1)
        self.assertFalse(app2.hud.visible)

    def test_au_alias_must_be_explicit(self):
        app = self.new_app()
        app.set_au_aliases("")
        app.import_path(str(self.export))
        self.assertNotIn("STORAGE (Arkadia Underground)", app.scope)
        app.set_au_aliases("STORAGE (Arkadia Underground)")
        self.assertIn("STORAGE (Arkadia Underground)", app.scope)
        self.assertNotIn("STORAGE", app.scope)


if __name__ == "__main__":
    unittest.main()
