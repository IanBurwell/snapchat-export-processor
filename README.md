# Snapchat Export Tools

A lightweight, non-destructive toolset to prepare Snapchat data exports for self-hosted photo managers (Immich, Google Photos, PhotoPrism, Apple Photos). Bakes Memories overlay graphics into composites, restores evidence-backed EXIF and QuickTime metadata, and provides a unified web-based GUI to review overlay pairs and purge unwanted chat media.

---

## 1. Environment Setup

### System Prerequisites
- **Python:** 3.11 or newer
- **ExifTool:** Required for reading and writing embedded EXIF/QuickTime metadata tags.
  - **Ubuntu / Debian:** `sudo apt update && sudo apt install -y exiftool`
  - **macOS:** `brew install exiftool`
  - **Windows:** Install via `winget install OliverBetz.ExifTool` or download from [exiftool.org](https://exiftool.org/).

### Python Environment
```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## 2. Recommended Workflow

```
[snapchat-data/]  ──►  python3 snapchat_processor.py  ──►  [snapchat-processed/]  ──►  python3 review_gui.py
(Raw Snapchat Export)   (Composite overlays, fix EXIF)    (Ready for manager)           (Filter & delete)
```

---

## 3. Tools & Usage

### A. Snapchat Processor (`snapchat_processor.py`)
Extracts Memories and Chat Media, composites overlay pairs, and writes verified date/time and GPS coordinates into file metadata. Original source exports remain completely untouched. Output destinations are strictly guarded: the processor refuses paths inside source directories or paths containing raw export markers (`index.html`, `html/`, `json/`).

```bash
# Run with defaults (source: snapchat-data/, output: snapchat-processed/)
python3 snapchat_processor.py

# Or specify custom source and output paths
python3 snapchat_processor.py -s /path/to/snapchat-export -o /path/to/output
```

If the target output directory already exists, the script prompts for user confirmation (`[y/N]`) before proceeding.

#### CLI Options
| Flag | Description |
| :--- | :--- |
| `-s, --source PATH` | Path to extracted Snapchat data export root folder (default: `snapchat-data`). |
| `-o, --output PATH` | Path to output destination folder (default: `snapchat-processed`; cannot be inside source or a source folder). |
| `-n, --dry-run` | Analyze data and validate all rules without writing files or metadata tags. |
| `--debug` | Export diagnostic copies of unsupported, conflict, and ambiguous files with metadata notes to `<output>/debug`. |
| `--temp-staging {auto,always,never}` | Fast Linux `/tmp` staging (auto-enabled for slow 9p / WSL Windows mounts). |
| `--exiftool-bin PATH` | Path or binary name for ExifTool (default: `exiftool`). |
| `--no-align-memories-sequence` | Disable sequence alignment fallback for Memories with expired download links. |
| `--no-progress` | Disable live progress bars and timing summary breakdown. |
| `-v, --verbose` | Enable detailed debug log output. |

---

### B. Unified Interactive Review GUI (`review_gui.py`)
A fast, zero-dependency browser interface powered by Python's standard library (`http.server` + `webbrowser`) that unifies Memories overlay pair review and Chat Media filtering into a single dashboard.

```bash
# Open unified review GUI with default path (snapchat-processed/)
python3 review_gui.py

# Specify custom output path
python3 review_gui.py /path/to/output

# Filter by initial source or category
python3 review_gui.py -s memories       # View Memories only
python3 review_gui.py -s chat_media      # View Chat Media only
python3 review_gui.py --mode overlays    # Focus on Memories overlay pairs
```

#### Review Features
- **Unified Exploration:** Browse both Memories and Chat Media with folder tabs (`All Sources`, `Memories`, `Chat Media`) and category filters (`Overlay Pairs`, `Photos`, `Videos`, `Thumbnails`, `Overlays`, `Stickers`).
- **Overlay Pair Review (One, Both, or Neither for Deletion):**
  - **Neither Deleted:** `Keep Both` (`1`) retains both original photo and composited overlay.
  - **One Deleted:** `Keep Without Overlay` (`2`) deletes composite, or `Keep With Overlay` (`3`) deletes original un-overlayed photo.
  - **Both Deleted:** `Delete Both` (`4` / `D`) deletes both original and composite files.
  - Interactive click-to-prefer on either side-by-side thumbnail with auto-advance.
- **Bulk Selection & Purging:**
  - Checkboxes on every card with multi-select and `Toggle Select Page`.
  - Batch actions: `Mark Selected Delete`, `Mark Selected Keep`, `Mark Page to Delete`, `Mark Page to Keep`, and `Invert Page`.
  - One-click category purges (e.g., delete all thumbnails or all stickers) with instant confirmation dialogs.
- **Grid & Focused Comparison Views:**
  - Paginated responsive grid (36 items/page with hover video autoplay).
  - Single/Compare Focus Mode (`V`) with instant keyboard shortcuts and auto-advancing.
- **Live Disk Reclaim Metrics:** Real-time counter of files marked for deletion and exact MB to be freed, applied on confirmation.

#### Keyboard Shortcuts
| Key | Context | Action |
| :--- | :--- | :--- |
| `1` | Overlay Pair | Keep Both (neither deleted) |
| `2` | Overlay Pair | Keep Without Overlay (delete composite) |
| `3` | Overlay Pair | Keep With Overlay (delete original) |
| `4` / `D` / `↓` | Overlay Pair | Delete Both |
| `K` / `Enter` / `Space` | Regular Media | Keep media |
| `D` / `↓` | Regular Media | Mark media for deletion |
| `P` / `←` | Single View | Previous item |
| `N` / `→` | Single View | Next item |
| `[` / `]` | Grid View | Previous / Next page |
| `V` / `Esc` | Any | Toggle between Grid and Single/Compare view |

#### CLI Options
| Flag | Description |
| :--- | :--- |
| `output_dir` / `-o, --output` | Path to the processed output directory (default: `snapchat-processed`). |
| `-s, --source {all,memories,chat_media}` | Initial media source folder (default: `all`). |
| `-m, --mode {auto,overlays,chat_media,both}` | Initial review filter mode (default: `auto`). |
| `-r, --report PATH` | Optional path to `report.json` to load pre-calculated pairs from. |
| `-p, --port INT` | Local port to bind server (default: random free port). |
| `--no-browser` | Prevent automatic opening of default web browser. |

---

## 4. Running Tests

Run the automated test suite with `pytest`:

```bash
pytest tests/
```
