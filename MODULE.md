# AyTree module (Artifact Scanner)

**What:** Legacy green file browser — expandable tree, per-item status, notes, git branches.  
**Spoken name:** iTree / AyTree. Palette stays **green-gold** (not board cyan).

## Routes (win_serve :8765)

| Path | Role |
|------|------|
| `/aytree` · `/itree` · `/legacy-tree` | Tree UI only (`index_tree.html` · pane=tree) |
| `/aytree/info` · `/aytree-info` | Info/metadata pane (same HTML · pane=info) |
| `GET /api/aytree/tree` | Filesystem + git branch tree |
| `GET`/`POST /api/aytree/notes` | Notes + status DB |
| `POST /api/aytree/extend` | create_folder · create_branch · delete_virtual |
| `GET`/`POST /api/aytree/pins` | Quick folders / pins (add · remove · reorder · replace · clear) |
| `GET /api/aytree` | Module info (scan root, notes path) |
| `GET /api/aytree/branch-preview` | Branch/worktree sneak-peek: commits · stat · patch vs HEAD |
| `GET /api/aytree/github-url` | GitHub compare/tree URL for a ref (origin only) |
| `GET /api/aytree/open-chrome?url=` | Open https URL in Google Chrome |

Selection sync: BroadcastChannel `aytree-selection-v1` + `localStorage` key `aytree_selected_node`.
Pin changes broadcast `{type:'pins'}` on the same channel (`aytree_pins_refresh` in storage).

## Quick access (tree window)

**The quick-folder tab strip and the rail are one list.** A pinned folder *is* a
root — `quickFolders` in the UI, `pins` in the notes DB. Do not add a second
store for "favourites"; extend this one.

Rail under the header: **Pinned folders** (drag to reorder, `Alt+1…9` to jump,
current root chipped `root`) + **Recent folders** (`GET /api/recent-paths`, dirs
only, one click to pin).

- Click an entry → inside the active root, Miller columns drill to it and the row
  flashes; outside it, the entry becomes the root (`switchRoot`). That is the
  "add a dir" path.
- Add: **＋ Add folder** / the `+` tab (accepts `~`, `~/dev`, quoted paths —
  server resolves via `GET /api/aytree/tree?root=`), hover ☆ on a column row, `p`
  on a selection, header **📌 Pin selected**, or right-click → Pin.
- Row right-click: pin · **open as root** · Open in Explorer · Copy path · New
  folder here.
- Storage: notes DB `pins` (any path — bookkeeping, not a mutation, so the
  scan-root guard does not apply). `localStorage aytree_quick_folders_v1` is a
  fast-boot mirror and the fallback for an older `win_serve`. Rail visibility:
  `aytree_qa_hidden`. A pin equal to the default scan root gets a rail entry but
  no duplicate tab.

## Config

- `AYTREE_SCAN_ROOT` — directory to map (default: `%USERPROFILE%\dev` if present)
- `AYTREE_NOTES` — notes JSON path (default: `%USERPROFILE%\.artifact-scanner\aytree_notes.json`)

## Integration (Artifact Scanner)

| Surface | Behavior |
|---------|----------|
| `window_host.py` launch | Opens `/aytree` **+** `/aytree/info` as Live-rail companions (same column, **half-height stack**: tree above, info below). `--no-aytree` / `--solo` opt out. |
| Board header **⌇** | Reopens tree+info pair via `pywebview.api.open_window` → `/api/open-window` → `window.open`. |
| Board **Menu · AyTree** | Same handler (text path for discoverability). |
| Title-only until hover | Shared companion-title hover + board **stay** toggle. |

## Feature work (yours)

Land new UI/features in:

1. `index_tree.html` — green shell (do not retheme to board cyan)
2. `api.py` — power layer; keep mutations under scan root

### Advanced (2026-08-10) — branch hover + GitHub click

- **Hover** a `branch_local` / `branch_remote` row → floating **infotip** (stat + truncated diff vs HEAD). Debounced ~280ms; cached per repo|ref.
- **Click** that row → still selects for info flip, and **opens Chrome** to GitHub **compare** URL (`default...branch`). Falls back to `window.open` if Chrome path missing.
- Needs a GitHub `origin` (or first remote). Non-GitHub remotes: tip shows error; click no-ops with console warn.

### Origin paint (2026-08-12) — you/ vs agent/

Path segments `you` · `user` · `user-requested` vs `agent` · `agent-suggested` · `suggested` (nearest wins).  
API field `origin`; rows get green **you** badge / muted italic **agent** badge; sort you first, agent last.  
Law: local you/ vs agent/ folders.

Standalone AyTree is a separate product tree.  
This module is the scanner-hosted copy for day-to-day + feature hooks.
