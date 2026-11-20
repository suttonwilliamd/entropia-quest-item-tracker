"""Two windows: the settings/main window (Tk root) and a tiny HUD (separate Toplevel).

The HUD is a borderless window (no title bar or native buttons). It starts hidden
(private by default) and only appears when the user clicks "Show HUD" or launches
with --show-hud.
"""
from __future__ import annotations

import os
import sqlite3
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .catalog import Catalog, load_catalog
from .inventory import (MAX_FILE_BYTES, InventoryImportError, InventorySnapshot, load_inventory_file,
                        parse_inventory_text)
from .live import LiveLog, find_chat_log, tracked_items
from .state import clamp_opacity, load_state, save_state
from .tracker import (action_text, clamp_stage, hud_layout_texts, hud_lines, progress_text,
                      reconcile_scope, shortfall_summary, stage_status)

APP_TITLE = "Entropia Quest Item Tracker"
HUD_TITLE = "Quest HUD"
HUD_FONT = ("Segoe UI", 9)
HUD_BG = "#14181c"
HUD_COLORS = {"title": "#ffffff", "stage": "#9ecbff", "ok": "#7bd88f", "short": "#ffb454",
              "unknown": "#9aa0a6", "info": "#c8c8c8", "refine": "#c3a6ff"}


class HudWindow:
    """Chromeless HUD: no title bar or native buttons. Drag the body to move; the
    small x hides the HUD (never exits the app)."""

    def __init__(self, master: tk.Misc, opacity: float, topmost: bool, position: str | None,
                 on_hide=None, on_moved=None, on_nav=None):
        self.top = tk.Toplevel(master)
        self.top.withdraw()
        self.top.title(HUD_TITLE)
        self.top.overrideredirect(True)  # HUD only: removes title bar, min/max/close
        self.top.configure(bg=HUD_BG)
        self.top.protocol("WM_DELETE_WINDOW", self.hide)
        self.position = position if isinstance(position, str) and position.startswith("+") else None
        self.on_hide = on_hide or (lambda: None)
        self.on_moved = on_moved or (lambda: None)
        self.on_nav = on_nav            # called with -1 / +1 from the stage-row arrows
        self.can_prev = self.can_next = False
        self._drag: tuple[int, int] | None = None
        self.hide_button: tk.Label | None = None
        self.prev_button: tk.Label | None = None
        self.next_button: tk.Label | None = None
        self.frame = tk.Frame(self.top, bg=HUD_BG, padx=6, pady=4)
        self.frame.pack(fill="both", expand=True)
        self._make_draggable(self.frame)
        self.set_opacity(opacity)
        self.set_topmost(topmost)

    # Drag anywhere except the hide button.
    def _make_draggable(self, widget: tk.Misc) -> None:
        widget.bind("<ButtonPress-1>", self._drag_start)
        widget.bind("<B1-Motion>", self._drag_move)
        widget.bind("<ButtonRelease-1>", self._drag_end)

    def _xy(self) -> tuple[int, int]:
        if self.position:
            x, _, y = self.position[1:].partition("+")
            return int(x), int(y)
        return self.top.winfo_x(), self.top.winfo_y()

    def _drag_start(self, event) -> None:
        x, y = self._xy()
        self._drag = (event.x_root - x, event.y_root - y)

    def _drag_move(self, event) -> None:
        if self._drag is not None:
            self.position = f"+{event.x_root - self._drag[0]}+{event.y_root - self._drag[1]}"
            self.top.geometry(self.position)

    def _drag_end(self, event) -> None:
        if self._drag is not None:
            self._drag = None
            self.on_moved()

    @staticmethod
    def _released_inside(event) -> bool:
        w = event.widget
        width, height = max(w.winfo_width(), w.winfo_reqwidth()), max(w.winfo_height(), w.winfo_reqheight())
        return 0 <= event.x < width and 0 <= event.y < height

    def _hide_release(self, event) -> None:
        if self._released_inside(event):  # released on the x itself
            self.hide()

    def _nav_release(self, event, delta: int) -> None:
        enabled = self.can_prev if delta < 0 else self.can_next
        if enabled and self.on_nav and self._released_inside(event):
            self.on_nav(delta)

    def set_layout(self, col0: list[str], col1: list[str]) -> None:
        """Fix column widths to the widest text any stage can show (measured with real
        labels, so DPI scaling is included). Width, header and arrow slots then never move."""
        def width(text: str, **kw) -> int:
            probe = tk.Label(self.frame, text=text, font=HUD_FONT, **kw)
            w = probe.winfo_reqwidth()
            probe.destroy()
            return w
        self.frame.grid_columnconfigure(0, minsize=max(map(width, col0 or [""])))
        self.frame.grid_columnconfigure(1, minsize=max(map(width, col1 or [""])) + 8)
        self.frame.grid_columnconfigure(2, minsize=max(width("refine") + 4, width("×", padx=2) + 6))

    def _nav_button(self, parent: tk.Misc, text: str, delta: int, enabled: bool) -> tk.Label:
        b = tk.Label(parent, text=text, bg=HUD_BG, font=(HUD_FONT[0], HUD_FONT[1], "bold"), padx=3, width=1,
                     fg=HUD_COLORS["stage"] if enabled else "#4a5058", cursor="hand2" if enabled else "")
        b.pack(side="left")
        b.bind("<ButtonRelease-1>", lambda e: self._nav_release(e, delta))
        return b

    @property
    def visible(self) -> bool:
        return self.top.state() != "withdrawn"

    def set_opacity(self, value: float) -> None:
        self.top.attributes("-alpha", clamp_opacity(value))

    def set_topmost(self, on: bool) -> None:
        self.top.attributes("-topmost", bool(on))

    def render(self, lines, can_prev: bool = False, can_next: bool = False) -> None:
        for child in self.frame.winfo_children():
            child.destroy()
        self.can_prev, self.can_next = can_prev, can_next
        self.prev_button = self.next_button = None
        for row, line in enumerate(lines):
            color = HUD_COLORS.get(line.kind, HUD_COLORS["info"])
            weight = "bold" if line.kind == "title" else "normal"
            font = (HUD_FONT[0], HUD_FONT[1], weight)
            if not line.value:
                lbl = tk.Label(self.frame, text=line.text, fg=color, bg=HUD_BG, font=font, anchor="w")
                self._make_draggable(lbl)
                if line.kind == "stage" and self.on_nav:  # stage text left, arrows right
                    lbl.grid(row=row, column=0, sticky="w")
                    nav = tk.Frame(self.frame, bg=HUD_BG)
                    nav.grid(row=row, column=1, columnspan=2, sticky="e")
                    self.prev_button = self._nav_button(nav, "‹", -1, can_prev)
                    self.next_button = self._nav_button(nav, "›", +1, can_next)
                else:
                    lbl.grid(row=row, column=0, columnspan=2, sticky="w")
                continue
            for col, text, fg, anchor, padx in ((0, line.text, HUD_COLORS["info"], "w", 0),
                                                (1, line.value, color, "e", (8, 0))):
                lbl = tk.Label(self.frame, text=text, fg=fg, bg=HUD_BG, font=font, anchor=anchor)
                lbl.grid(row=row, column=col, sticky=anchor, padx=padx)
                self._make_draggable(lbl)
            if line.mark:  # e.g. "refine": progress not yet physically held
                lbl = tk.Label(self.frame, text=line.mark, fg=HUD_COLORS["refine"], bg=HUD_BG, font=font)
                lbl.grid(row=row, column=2, sticky="w", padx=(4, 0))
                self._make_draggable(lbl)
        self.hide_button = tk.Label(self.frame, text="×", fg=HUD_COLORS["unknown"], bg=HUD_BG,
                                    font=HUD_FONT, cursor="hand2", padx=2)
        self.hide_button.grid(row=0, column=2, sticky="ne", padx=(6, 0))
        self.hide_button.bind("<ButtonRelease-1>", self._hide_release)
        self.top.update_idletasks()
        size = f"{self.frame.winfo_reqwidth()}x{self.frame.winfo_reqheight()}"
        self.top.geometry(size + (self.position or ""))

    def requested_size(self) -> tuple[int, int]:
        self.top.update_idletasks()
        return self.frame.winfo_reqwidth(), self.frame.winfo_reqheight()

    def show(self) -> None:
        self.top.deiconify()

    def hide(self) -> None:
        if self.visible:
            self.position = f"+{self.top.winfo_x()}+{self.top.winfo_y()}"
        self.top.withdraw()
        self.on_hide()


class TrackerApp:
    def __init__(self, root: tk.Tk, state_file: Path, catalog: Catalog | None = None,
                 chat_log: Path | None = None):
        self.root = root
        self.state_file = Path(state_file)
        self.state = load_state(self.state_file)
        self.catalog = catalog or load_catalog()
        self.inventory: InventorySnapshot | None = None
        if isinstance(self.state.get("inventory"), dict):
            try:
                self.inventory = InventorySnapshot.from_json(self.state["inventory"])
            except (KeyError, TypeError, ValueError):
                self.state["inventory"] = None
        # Baseline + live pickups live in SQLite. Upgrade: an older state.json snapshot is moved
        # in once (anchored at the log's current end, so nothing earlier is backfilled).
        self.live: LiveLog | None = None
        try:
            self.live = LiveLog(self.state_file.with_name("tracker.db"), chat_log, tracked_items(self.catalog))
            base = self.live.baseline()
            if base is None and self.inventory is not None:
                self.live.reset(self.inventory, gap=True)   # no trustworthy cursor: no replay, reimport advised
                base = self.inventory
            self.inventory = base
            if base is not None:
                base.carried_pickups = self.live.pickups()
            self.state["inventory"] = None
        except (sqlite3.Error, OSError, KeyError, TypeError, ValueError):
            self.live = None   # fall back to state.json-only behaviour, no live pickups
        self._save_job = None
        self._locations: list[str] = []
        self.read_clipboard = root.clipboard_get  # replaced in tests; called only from paste_inventory
        self.hud_view: int | None = None  # HUD-only stage browse; None = follow committed stage; never saved

        root.title(APP_TITLE)
        self._build()
        self.hud = HudWindow(root, self.state["hud_opacity"], self.state["hud_topmost"],
                             self.state["hud_position"], on_hide=self._hud_hidden, on_moved=self.schedule_save,
                             on_nav=self.browse_hud)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    # ---------- UI ----------
    def _build(self) -> None:
        f = ttk.Frame(self.root, padding=8)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        bar = ttk.Frame(f)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Button(bar, text="Import inventory…", command=self.import_dialog).pack(side="left")
        ttk.Button(bar, text="Paste inventory", command=lambda: self._report(*self.paste_inventory())
                   ).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="Reimport", command=self._reimport_clicked).pack(side="left", padx=4)
        self.status = tk.StringVar()
        ttk.Label(bar, textvariable=self.status).pack(side="left", padx=6)
        self.live_status = tk.StringVar()
        ttk.Label(bar, textvariable=self.live_status, foreground="#666").pack(side="left")

        ttk.Label(f, text="Count from").grid(row=1, column=0, sticky="nw", pady=(6, 0))
        self.loc_list = tk.Listbox(f, selectmode="multiple", height=5, exportselection=False)
        self.loc_list.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        self.loc_list.bind("<<ListboxSelect>>", lambda e: self.set_scope_from_list())
        qs = ttk.Frame(f)
        qs.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 2))
        ttk.Label(qs, text="Quest").pack(side="left")
        self.quest_box = ttk.Combobox(qs, state="readonly", width=22)
        self.quest_box.pack(side="left", padx=4)
        self.quest_box.bind("<<ComboboxSelected>>", lambda e: self._quest_chosen())
        ttk.Label(qs, text="Stage").pack(side="left", padx=(8, 0))
        self.stage_box = ttk.Combobox(qs, state="readonly", width=22)
        self.stage_box.pack(side="left", padx=4)
        self.stage_box.bind("<<ComboboxSelected>>", lambda e: self.set_stage(self.stage_box.current()))

        self.headline = tk.StringVar()
        self.action = tk.StringVar()
        ttk.Label(f, textvariable=self.headline, font=("Segoe UI", 10, "bold"), wraplength=560
                  ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(f, textvariable=self.action, wraplength=560).grid(row=5, column=0, columnspan=2, sticky="w")

        cols = ("progress", "after", "action", "need", "have", "short", "refine")
        self.tree = ttk.Treeview(f, columns=cols, height=6, displaycolumns=self.MAIN_COLUMNS)
        self.tree.heading("#0", text="Item")
        self.tree.column("#0", width=210)
        for c, t, w in zip(cols, ("Progress", "Still missing", "Action", "Need", "Have now", "Short now",
                                  "+Refinable"), (180, 90, 230, 60, 70, 70, 75)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c in ("action", "progress") else "e")
        self.tree.grid(row=6, column=0, columnspan=2, sticky="nsew", pady=(4, 0))
        f.rowconfigure(6, weight=1)

        hud = ttk.LabelFrame(f, text="HUD", padding=4)
        hud.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.manual_frame = ttk.Frame(f)   # per-piece manual gear checks (upgrade goals)
        self.manual_frame.grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._manual_vars: list[tk.BooleanVar] = []
        self.hud_btn = ttk.Button(hud, text="Show HUD", command=self.toggle_hud)
        self.hud_btn.pack(side="left")
        ttk.Label(hud, text="Opacity").pack(side="left", padx=(10, 2))
        self.opacity_var = tk.DoubleVar(value=self.state["hud_opacity"] * 100)
        ttk.Scale(hud, from_=25, to=100, variable=self.opacity_var, length=140,
                  command=lambda v: self.set_opacity(float(v) / 100)).pack(side="left")
        self.opacity_label = ttk.Label(hud, width=5)
        self.opacity_label.pack(side="left")
        self.topmost_var = tk.BooleanVar(value=self.state["hud_topmost"])
        ttk.Checkbutton(hud, text="On top", variable=self.topmost_var,
                        command=lambda: self.set_topmost(self.topmost_var.get())).pack(side="left", padx=6)

        self.details_btn = ttk.Button(f, text="Details ▸", command=self.toggle_details)
        self.details_btn.grid(row=9, column=0, sticky="w", pady=(6, 0))
        self.details = ttk.Frame(f)
        self.details.grid(row=10, column=0, columnspan=2, sticky="ew")
        self.details.columnconfigure(1, weight=1)
        ttk.Label(self.details, text="AU labels").grid(row=0, column=0, sticky="w")
        self.au_var = tk.StringVar(value="; ".join(self.state["au_aliases"]))
        au = ttk.Entry(self.details, textvariable=self.au_var)
        au.grid(row=0, column=1, sticky="ew", pady=2)
        au.bind("<Return>", lambda e: self.set_au_aliases(self.au_var.get()))
        au.bind("<FocusOut>", lambda e: self.set_au_aliases(self.au_var.get()))
        self.file_info = tk.StringVar()
        self.source = tk.StringVar()
        for r, var in ((1, self.file_info), (2, self.source)):
            ttk.Label(self.details, textvariable=var, foreground="#666").grid(row=r, column=0, columnspan=2, sticky="w")
        self.details.grid_remove()  # collapsed by default

    MAIN_COLUMNS = ("progress", "after", "action")

    def toggle_details(self) -> None:
        if self.details.winfo_manager():
            self.details.grid_remove()
            self.tree.configure(displaycolumns=self.MAIN_COLUMNS)
            self.details_btn.configure(text="Details ▸")
        else:
            self.details.grid()
            self.tree.configure(displaycolumns="#all")
            self.details_btn.configure(text="Details ▾")

    # ---------- state ----------
    @property
    def quest(self):
        return self.catalog.get(self.state["selected_quest"]) or (self.catalog.quests[0] if self.catalog.quests else None)

    @property
    def stage_index(self) -> int:
        q = self.quest
        return clamp_stage(q, self.state["stage_by_quest"].get(q.id, 0)) if q else 0

    @property
    def scope(self) -> set[str]:
        return set(self.state["scope"] or [])

    def schedule_save(self) -> None:
        if self._save_job is not None:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(400, self.save_now)

    def save_now(self) -> None:
        if self._save_job is not None:
            self.root.after_cancel(self._save_job)
            self._save_job = None
        self.state["hud_position"] = self.hud.position
        # With SQLite the snapshot lives in tracker.db; state.json keeps settings only.
        self.state["inventory"] = self.inventory.to_json() if self.inventory and not self.live else None
        save_state(self.state_file, self.state)

    # ---------- actions ----------
    def import_path(self, path: str) -> tuple[bool, str]:
        """Import an export file. On any error the previous inventory is kept."""
        return self._import(lambda: load_inventory_file(path))

    def paste_inventory(self) -> tuple[bool, str]:
        """Read the clipboard (only when the user clicks) and import it like a file."""
        try:
            text = self.read_clipboard()
        except Exception:
            return False, "Clipboard is unavailable or does not hold text. Copy the inventory table and try again."
        if not isinstance(text, str) or not text.strip():
            return False, "Clipboard is empty. Copy the inventory table (with its header row) and try again."
        if len(text) > MAX_FILE_BYTES:
            return False, "Clipboard text is too large to be an inventory export."
        return self._import(lambda: parse_inventory_text(text))

    def _import(self, load) -> tuple[bool, str]:
        try:
            snap = load()
        except InventoryImportError as exc:
            return False, "\n".join([str(exc), *exc.errors])
        except Exception:  # never crash or echo file contents; keep the last good import
            return False, "Could not import this file; the previous inventory is unchanged."
        if self.live:
            try:   # one transaction: new baseline, pickups cleared, cursor at the log's current end
                self.live.reset(snap)
            except sqlite3.Error:
                return False, "Could not save the import; the previous inventory is unchanged."
        self.inventory = snap
        scope, known, new = reconcile_scope(self.state["scope"], self.state["known_locations"],
                                            list(snap.locations()), self.state["au_aliases"])
        self.state.update(scope=scope, known_locations=known)
        self.save_now()
        self.refresh()
        msg = f"Imported {len(snap.rows)} rows"
        if new:
            msg += f"; new locations: {', '.join(new)}"
        return True, "\n".join([msg, *snap.warnings])

    def import_dialog(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, title="Inventory export",
                                          filetypes=[("Inventory export", "*.tsv *.txt *.csv"), ("All files", "*.*")])
        if path:
            self._report(*self.import_path(path))

    def reimport(self) -> tuple[bool, str]:
        if not self.inventory:
            return False, "Nothing imported yet."
        if not self.inventory.source_path:
            return False, "The last inventory was pasted; use Paste inventory again."
        return self.import_path(self.inventory.source_path)

    def _reimport_clicked(self) -> None:
        self._report(*self.reimport())

    def _report(self, ok: bool, msg: str) -> None:
        if not ok:
            messagebox.showerror(APP_TITLE, msg, parent=self.root)
        elif "\n" in msg:
            messagebox.showinfo(APP_TITLE, msg, parent=self.root)

    def set_scope_from_list(self) -> None:
        chosen = {self._locations[i] for i in self.loc_list.curselection()}
        kept = [s for s in (self.state["scope"] or []) if s not in self._locations]  # absent from this import
        self.state["scope"] = kept + [loc for loc in self._locations if loc in chosen]
        self.schedule_save()
        self.refresh()

    def set_au_aliases(self, text: str) -> None:
        aliases = [a.strip() for a in text.split(";") if a.strip()]
        if aliases == self.state["au_aliases"]:
            return
        added = [a for a in aliases if a not in self.state["au_aliases"]]
        self.state["au_aliases"] = aliases
        scope = list(self.state["scope"] or [])
        scope += [a for a in added if a in self._locations and a not in scope]  # explicit alias -> count it
        self.state["scope"] = scope
        self.schedule_save()
        self.refresh()

    @property
    def hud_stage(self) -> int:
        return self.stage_index if self.hud_view is None else self.hud_view

    def browse_hud(self, delta: int) -> None:
        """HUD arrows: preview another stage. Never changes the committed stage or saved state."""
        q = self.quest
        if q is None:
            return
        new = max(0, min(len(q.stages) - 1, self.hud_stage + delta))
        if new != self.hud_stage:
            self.hud_view = None if new == self.stage_index else new
            self.render_hud()

    def render_hud(self) -> None:
        q, i = self.quest, self.hud_stage
        if q is not None and getattr(self, "_hud_layout_quest", None) != q.id:
            self.hud.set_layout(*hud_layout_texts(q))
            self._hud_layout_quest = q.id
        checked = self.checked(q, i) if q is not None else set()
        self.hud.render(hud_lines(self.catalog, q, i, self.inventory, self.scope, checked),
                        can_prev=bool(q) and i > 0, can_next=bool(q) and i < len(q.stages) - 1)

    def checked(self, quest, index: int) -> set[str]:
        """Manual checks the user ticked for one goal's piece (independent per goal/piece)."""
        if index >= len(quest.stages):
            return set()
        ticks = self.state["manual_checks"].get(quest.id, {})
        ids = ticks.get(quest.stages[index].name, []) if isinstance(ticks, dict) else []
        return {x for x in ids if isinstance(x, str)} if isinstance(ids, list) else set()

    def set_manual(self, check_id: str, on: bool) -> None:
        q, i = self.quest, self.stage_index
        if q is None or i >= len(q.stages):
            return
        ids = self.checked(q, i)
        ids = ids | {check_id} if on else ids - {check_id}
        valid = [c.id for c in q.stages[i].manual if c.id in ids]
        quest_ticks = self.state["manual_checks"].get(q.id)
        if not isinstance(quest_ticks, dict):
            quest_ticks = self.state["manual_checks"][q.id] = {}
        quest_ticks[q.stages[i].name] = valid
        self.schedule_save()
        self.refresh()

    def set_quest(self, quest_id: str) -> None:
        self.hud_view = None
        self.state["selected_quest"] = quest_id
        self.schedule_save()
        self.refresh()

    def _quest_chosen(self) -> None:
        i = self.quest_box.current()
        if 0 <= i < len(self.catalog.quests):
            self.set_quest(self.catalog.quests[i].id)

    def set_stage(self, index: int) -> None:
        q = self.quest
        if q is None or index < 0:
            return
        self.hud_view = None
        self.state["stage_by_quest"][q.id] = clamp_stage(q, index)
        self.schedule_save()
        self.refresh()

    def set_opacity(self, value: float) -> None:
        v = clamp_opacity(value)
        self.state["hud_opacity"] = v
        self.hud.set_opacity(v)
        self.opacity_label.configure(text=f"{round(v * 100)}%")
        self.schedule_save()

    def set_topmost(self, on: bool) -> None:
        self.state["hud_topmost"] = bool(on)
        self.hud.set_topmost(on)
        self.schedule_save()

    def toggle_hud(self) -> None:
        if self.hud.visible:
            self.hud.hide()
        else:
            self.hud.show()
        self.hud_btn.configure(text="Hide HUD" if self.hud.visible else "Show HUD")

    def _hud_hidden(self) -> None:
        self.hud_btn.configure(text="Show HUD")
        self.schedule_save()

    def close(self) -> None:
        self.hud.hide()
        self.save_now()
        self.root.destroy()

    # ---------- view ----------
    def refresh(self) -> None:
        inv, scope, q = self.inventory, self.scope, self.quest
        self._locations = list(inv.locations()) if inv else []
        self.loc_list.delete(0, "end")
        locs = inv.locations() if inv else {}
        for i, loc in enumerate(self._locations):
            self.loc_list.insert("end", f"{loc}  — {locs[loc]['rows']} stacks")
            if loc in scope:
                self.loc_list.selection_set(i)
        if inv:
            counted = sum(1 for loc in self._locations if loc in scope)
            name = os.path.basename(inv.source_path) or "pasted inventory"
            self.status.set(f"Counting {counted}/{len(self._locations)} locations")
            self.file_info.set(f"{name} · {len(inv.rows)} rows · imported {inv.imported_at}"
                               + (f" · {len(inv.warnings)} warning(s)" if inv.warnings else ""))
        else:
            self.status.set("No inventory imported")
            self.file_info.set("")
        self.headline.set("")
        self.action.set("")

        self.quest_box["values"] = [x.name for x in self.catalog.quests]
        self.tree.delete(*self.tree.get_children())
        for child in self.manual_frame.winfo_children():
            child.destroy()
        self._manual_vars = []
        if q is None:
            self.quest_box.set("")
            self.stage_box["values"] = []
            self.source.set("No quests in catalog")
        else:
            self.quest_box.current(self.catalog.quests.index(q))
            self.stage_box["values"] = [f"{i + 1}. {s.name}" if q.ordered else f"{s.name} ({i + 1} of {len(q.stages)})"
                                        for i, s in enumerate(q.stages)] + ["Complete"]
            self.stage_box.current(self.stage_index)
            where = f" · NPC {q.npc}, {q.npc_location}" if q.npc else ""
            order = "" if q.ordered else " · pieces listed in base order; order not enforced"
            self.source.set(" · ".join(["Source: " + q.source, *q.more_sources]) + where + order)
            if self.stage_index < len(q.stages):
                stage = q.stages[self.stage_index]
                statuses = stage_status(self.catalog, stage, inv, scope)
                headline, action = shortfall_summary(statuses)
                ticked = self.checked(q, self.stage_index)
                open_checks = sum(1 for c in stage.manual if c.id not in ticked)
                if open_checks:
                    headline += f" · {open_checks} gear check(s) open"
                for c in stage.manual:   # ticked by hand only; never inferred from inventory
                    var = tk.BooleanVar(value=c.id in ticked)
                    ttk.Checkbutton(self.manual_frame, text=c.text, variable=var,
                                    command=lambda cid=c.id, v=var: self.set_manual(cid, v.get())).pack(anchor="w")
                    self._manual_vars.append(var)
                self.headline.set(headline)
                self.action.set(action)
                for st in statuses:
                    unknown = st.have is None
                    self.tree.insert("", "end", text=st.item, values=(
                        progress_text(st), "?" if unknown else st.short_after_refining, action_text(st), st.need,
                        "?" if unknown else st.have, "?" if unknown else st.short, st.refinable or ""))
            else:
                self.headline.set("Quest complete")
        self.opacity_label.configure(text=f"{round(self.state['hud_opacity'] * 100)}%")
        self.live_status.set(self.live_text())
        self.render_hud()

    # ---------- live pickups ----------
    LIVE_TEXT = {
        "off": "Live receipts off: chat.log not found",
        "unreadable": "Live receipts paused: chat.log unreadable, retrying",
        "unsupported": "Live receipts off: chat.log format not supported",
        "error": "Live receipts paused: tracker database busy, retrying",
    }

    def live_text(self) -> str:
        """Short main-window status. Counts are estimates; the HUD shows none of this."""
        if self.inventory is None:
            return ""
        if self.live is None:
            return "Live receipts off"
        s = self.live.status
        if s in self.LIVE_TEXT:
            return self.LIVE_TEXT[s]
        if s not in ("live", "gap"):
            return ""
        n = sum(self.inventory.carried_pickups.values())
        text = f"Live: +{n:,} received since import (estimate, any source)"
        if "CARRIED" not in self.scope:
            text += " · not counted: tick CARRIED"
        if s == "gap":
            text += " · receipts may be missing: reimport to resync"
        elif self.live.quiet:
            text += " · log quiet"
        return text

    def poll_live(self) -> None:
        if self.live is None or self.inventory is None:
            return
        self.live.poll()
        try:
            pickups = self.live.pickups()
        except sqlite3.Error:
            self.live.status = "error"
            pickups = self.inventory.carried_pickups
        if pickups != self.inventory.carried_pickups:
            self.inventory.carried_pickups = pickups
            self.refresh()
        else:
            self.live_status.set(self.live_text())

    def start_live_polling(self, every_ms: int = 2000) -> None:
        self.poll_live()
        self.root.after(every_ms, self.start_live_polling, every_ms)


def main() -> None:
    from .state import state_path
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    import sys
    root = tk.Tk()
    app = TrackerApp(root, state_path(), chat_log=find_chat_log())
    if "--show-hud" in sys.argv[1:]:   # explicit opt-in (e.g. relaunch); default stays hidden
        app.toggle_hud()
    app.start_live_polling()
    root.mainloop()
