# Entropia Quest Item Tracker

A standalone Windows app for answering “what am I still missing for this quest?” Start by importing a full inventory export, then choose a quest, stage, and the storage locations to count. A changes-only export is not a full picture of your holdings.

The app has two windows:

- **Main window**: import or reimport the inventory export, choose which locations count, pick a quest and stage, and set the HUD (show/hide, opacity, always on top). The top of the window shows the shortfall left after refining and one action line, for example "Refine 100 Corria Timber, 75 Soft Hide, then gather the rest". A collapsed **Details** section holds the ready-now Have / Short / +Refinable columns, the AU labels box, the import file information and the source.
- **Quest HUD**: a small separate borderless window, with no title bar and no minimize, maximize or close buttons (about 210×155 px at 100% DPI). Drag it by its body to move it. The small × hides the HUD and does not close the app. It shows the quest, the stage and each item's held have/need. It never shows values, locations, the NPC position or refinable amounts. It starts hidden every time the app opens.

It uses only Python's standard library (tkinter), so there is no build step and nothing to install.

> **Unofficial fan tool.** Not affiliated with or endorsed by MindArk or Planet Arkadia. The quest data is independently transcribed factual recipe data with cited sources. No game artwork, client files or binaries are included. See [Data sources and trademarks](#data-sources-and-trademarks).

## Run

Requires Windows and Python with tkinter (included in the python.org installer). Tested with Python 3.15 only.

```
cd entropia-quest-item-tracker
pythonw -m eqit          # or double-click run_tracker.pyw
pythonw run_tracker.pyw --show-hud   # optional: open the HUD at start (default: hidden)
```

## Test (no windows shown)

```
python -m unittest discover -s tests -t .
```

The GUI wiring tests create Tk windows but keep them withdrawn, so nothing is drawn on screen. Set `EQIT_SKIP_GUI_TESTS=1` to skip them.

## Inventory import

**Paste inventory** reads the clipboard only when you click the button, and only once per click. Pasted text goes through the same parser and the same all-or-nothing rules as a file. An empty or unavailable clipboard shows a short message and leaves the current inventory unchanged. Reimport only re-reads files; after a paste, paste again.

Input is the game's inventory export: a quoted, tab-separated file whose header is exactly
`Id, Name, Quantity, Value(PED), Container, ContainerRefId`. A comma-separated file with the same header also works. UTF-8 with or without a BOM, UTF-16 with a BOM, and Windows-1252 are accepted.

- **All or nothing.** A wrong header, a non-whole-number quantity, an empty name, a wrong column count or an unreadable file rejects the whole file. The error message lists the line numbers (up to 20), and the previous import stays in use. Files over 20 MB are refused.
- **Warnings only.** A repeated Id (both rows are kept) or a file with no items.
- `Value(PED)` is ignored and never stored.
- **Locations.** Each row's location is its `Container` label. If a row's `ContainerRefId` matches another row's `Id`, the row inherits that parent row's location. Labels are never merged: `STORAGE (Arkadia)`, `STORAGE (Calypso)` and bare `STORAGE` stay separate.
- **Default scope.** `CARRIED` and `STORAGE (Arkadia)` are counted. Arkadia Underground counts only if its exact label is entered under "AU labels" (separate several with `;`). Bare `STORAGE` is never treated as AU. On reimport, your existing location choices are kept, and new locations are ticked only if they are local.

## Progress rules

- The stage is whatever you select. Owning enough items never moves you to the next stage.
- Only the current stage's items are counted. Other loot is ignored.
- **Have** counts only the refined or finished item actually held in the ticked locations. **+Refinable** shows what raw materials in those locations could make, counting only whole batches (for example, 7 Soft Hide makes 2 Soft Leather). The two numbers are never added together.

## Data

`eqit/data/catalog.json` holds Viceroy Armor (NPC Bill, Celeste Quarry): 7 ordered stages and the refining ratios, transcribed from https://www.arkadiaarchive.com/viceroyarmor. Every quest in the catalog must cite a source; the loader rejects any quest without one.

The upgrade goals are **Viceroy Adjusted**, **Viceroy Improved** and **Viceroy Modified**. Each has 7 pieces handed in to Bill and keeps its own progress, separate from the base quest and from the other upgrades. Each piece needs:

| Goal | Prior piece | Donor piece | Materials |
|---|---|---|---|
| Adjusted | full-TT base Viceroy | full-TT Musca (L) | 2 Adjusted Nanites, 100 Bismuth Plating, 30 Ospra Ingot |
| Improved | full-TT Adjusted Viceroy | full-TT Canis (L) | 2 Improved Nanites, 100 Bismuth Plating, 10 Banite Ingot |
| Modified | full-TT Improved Viceroy | full-TT Lupus (L) | 2 Modified Nanites, 100 Bismuth Plating, 4 Zulax Ingot |

- The prior and donor pieces must match the body part and gender, and normal and Skullcandy pieces must not be mixed.
- Those two gear pieces are **manual checks** you tick yourself. They are never inferred from the export: gear names and per-item full-TT condition aren't verified, and the export's aggregate value is not per-item condition.
- Pieces are listed in base order for display only. Upgrade order is not enforced.
- Sources: the upgrade blog post (exact per-piece quantities), the Arkadia 2016.5 release notes (the mechanic and Skullcandy) and Arkadia Archive (resource totals).

State, including the last good import, is saved in `%LOCALAPPDATA%\EntropiaQuestItemTracker\state.json`. Set `EQIT_STATE_DIR` to change the folder. Writes are atomic. A corrupt state file is renamed to `state.corrupt-<time>.json` rather than deleted.

## Live receipts (estimate)

After an import, the tracker reads new lines from `<Documents>\Entropia Universe\chat.log`. It uses the Windows Documents folder (including when OneDrive redirects it), then `%OneDrive%\Documents`, then `%USERPROFILE%\Documents`. It only reads: game and log settings are never changed. It counts only complete `[System] [] You received <Item> x (<n>) Value: <v> PED` lines for items the catalog tracks, and adds them to CARRIED. They count only while CARRIED is ticked.

- That line means an item entered your inventory, but not why. Loot, trades, mission rewards or crafting may produce it. So the status reads "received … (estimate, any source)".
- Refining outputs (veneers, leather, thread, cloth) are never credited. Crafting, refining, hand-ins and sales are not inferred, so reimport after any of them.
- Each import replaces the snapshot, clears receipts and starts reading from the log's current end, all in one step. Nothing logged before the import is counted. A failed import changes nothing.
- If the log is replaced, truncated or unreadable, the reader skips to the current end and shows "receipts may be missing: reimport to resync". It never replays old lines.
- Data lives in `tracker.db` next to `state.json`. It holds the snapshot, tracked item totals and the read position, plus a hash used to recognise the file. No chat text is stored.
- **Upgrade:** a snapshot saved by an older version moves into `tracker.db` with no replay, and you're asked to reimport.

## Data sources and trademarks

- **What the catalog is.** `eqit/data/catalog.json` holds independently transcribed factual game data: quest hand-in quantities and refining ratios. Each quest cites its sources in the file, using the `source` and `more_sources` fields. Those sources are community and vendor pages that this project doesn't control. No text, images or other content from them is reproduced.
- **What is not included.** No game artwork, client files or binaries, chat logs, or player inventory data. The tests use synthetic inventory data only.
- **Trademarks and affiliation.** Entropia Universe is a trademark of MindArk. Planet Arkadia, and the item and quest names, belong to their respective owners. This project is not affiliated with or endorsed by MindArk, Planet Arkadia or the cited sources.

## License

The project's own code is released under the MIT License (see [LICENSE](LICENSE)). The license grants no rights in third-party trademarks, names, game content or the cited sources.
