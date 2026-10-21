#!/usr/bin/env python3
"""
Interactive review GUI for Snapchat exports:
- Overlay pairs review (original main vs composite)
- Chat media filter & categorization mode (seperated by type: thumbnails, overlays, videos, photos, etc.)
  with group deletion support.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import http.server
import json
import logging
import mimetypes
import os
import pathlib
import re
import sys
import threading
from typing import Any
import urllib.parse
import webbrowser


SUPPORTED_IMAGE_EXTENSIONS: set[str] = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif"}
SUPPORTED_VIDEO_EXTENSIONS: set[str] = {".mp4", ".mov", ".m4v"}
ALL_MEDIA_EXTENSIONS: set[str] = SUPPORTED_IMAGE_EXTENSIONS | SUPPORTED_VIDEO_EXTENSIONS


def classify_chat_media_type(rel_path: str, filename: str, is_video: bool) -> str:
    lower_name = filename.lower()
    lower_path = rel_path.lower()

    if is_video:
        return "videos"

    if any(tok in lower_name for tok in ("thumbnail", "thumb_", "-thumb", "_thumb", ".thumb")):
        return "thumbnails"
    if "/thumbnails/" in lower_path or "/thumbs/" in lower_path or lower_path.startswith("thumbnails/"):
        return "thumbnails"

    if any(tok in lower_name for tok in ("overlay", "-overlay", "_overlay", "overlay_")):
        return "overlays"
    if "/overlays/" in lower_path or lower_path.startswith("overlays/"):
        return "overlays"

    if any(tok in lower_name for tok in ("sticker", "bitmoji", "emoji", "-sticker", "_sticker")) or lower_name.endswith(".gif"):
        return "stickers"

    ext = pathlib.PurePosixPath(lower_name).suffix
    if ext in SUPPORTED_IMAGE_EXTENSIONS or ext in (".heic", ".heif", ".bmp"):
        return "photos"

    return "other"


@dataclass
class PairReviewItem:
    index: int
    main_rel_path: str
    composite_rel_path: str
    main_abs_path: pathlib.Path
    composite_abs_path: pathlib.Path
    choice: str = "both"


class PairReviewManager:
    def __init__(self, output_root: pathlib.Path, pairs: list[dict[str, str]]) -> None:
        self.output_root = output_root
        self.items: list[PairReviewItem] = [
            PairReviewItem(
                index=idx,
                main_rel_path=p["main_rel_path"],
                composite_rel_path=p["composite_rel_path"],
                main_abs_path=output_root / p["main_rel_path"],
                composite_abs_path=output_root / p["composite_rel_path"],
                choice=p.get("choice", "both"),
            )
            for idx, p in enumerate(pairs)
        ]

    def set_choice(self, index: int, choice: str) -> None:
        if 0 <= index < len(self.items) and choice in ("both", "without_overlay", "with_overlay"):
            self.items[index].choice = choice

    def set_all_choices(self, choice: str) -> None:
        if choice in ("both", "without_overlay", "with_overlay"):
            for item in self.items:
                item.choice = choice

    def apply_deletions(self) -> dict[str, Any]:
        deleted_files: list[str] = []
        errors: list[dict[str, str]] = []
        kept_both = kept_with = kept_without = 0

        for item in self.items:
            if item.choice == "with_overlay":
                kept_with += 1
                if item.main_abs_path.is_file():
                    try:
                        item.main_abs_path.unlink()
                        deleted_files.append(item.main_rel_path)
                    except OSError as exc:
                        errors.append({"file": item.main_rel_path, "error": str(exc)})
            elif item.choice == "without_overlay":
                kept_without += 1
                if item.composite_abs_path.is_file():
                    try:
                        item.composite_abs_path.unlink()
                        deleted_files.append(item.composite_rel_path)
                    except OSError as exc:
                        errors.append({"file": item.composite_rel_path, "error": str(exc)})
            else:
                kept_both += 1

        return {
            "total_pairs": len(self.items),
            "kept_both": kept_both,
            "kept_with_overlay": kept_with,
            "kept_without_overlay": kept_without,
            "deleted_files": deleted_files,
            "errors": errors,
        }


@dataclass
class ChatMediaItem:
    index: int
    rel_path: str
    abs_path: pathlib.Path
    filename: str
    is_video: bool
    size_bytes: int
    media_type: str = "photos"
    choice: str = "keep"  # "keep", "delete", or "deleted"


class ChatMediaReviewManager:
    def __init__(self, output_root: pathlib.Path, items: list[dict[str, Any]]) -> None:
        self.output_root = output_root
        self.items: list[ChatMediaItem] = []
        for idx, it in enumerate(items):
            rel = it["rel_path"]
            fname = it.get("filename") or pathlib.PurePosixPath(rel).name
            is_vid = it.get("is_video", False)
            mtype = it.get("media_type") or classify_chat_media_type(rel, fname, is_vid)
            self.items.append(
                ChatMediaItem(
                    index=idx,
                    rel_path=rel,
                    abs_path=output_root / rel,
                    filename=fname,
                    is_video=is_vid,
                    size_bytes=it.get("size_bytes", 0),
                    media_type=mtype,
                    choice=it.get("choice", "keep"),
                )
            )

    def set_choice(self, index: int, choice: str) -> None:
        if 0 <= index < len(self.items) and choice in ("keep", "delete"):
            self.items[index].choice = choice

    def set_choices_bulk(self, indices: list[int], choice: str) -> None:
        if choice in ("keep", "delete"):
            for idx in indices:
                if 0 <= idx < len(self.items):
                    self.items[idx].choice = choice

    def set_group_choice(self, group: str, choice: str) -> int:
        count = 0
        if choice in ("keep", "delete"):
            for item in self.items:
                if (group == "all" or item.media_type == group) and item.choice != "deleted":
                    item.choice = choice
                    count += 1
        return count

    def set_all_choices(self, choice: str) -> None:
        if choice in ("keep", "delete"):
            for item in self.items:
                if item.choice != "deleted":
                    item.choice = choice

    def delete_group(self, group: str) -> dict[str, Any]:
        deleted_files: list[str] = []
        errors: list[dict[str, str]] = []
        for item in self.items:
            if (group == "all" or item.media_type == group) and item.choice != "deleted":
                item.choice = "deleted"
                if item.abs_path.is_file():
                    try:
                        item.abs_path.unlink()
                        deleted_files.append(item.rel_path)
                    except OSError as exc:
                        errors.append({"file": item.rel_path, "error": str(exc)})
        return {
            "group": group,
            "deleted": len(deleted_files),
            "deleted_files": deleted_files,
            "errors": errors,
        }

    def apply_deletions(self) -> dict[str, Any]:
        deleted_files: list[str] = []
        errors: list[dict[str, str]] = []
        kept = deleted = 0

        for item in self.items:
            if item.choice in ("delete", "deleted"):
                deleted += 1
                if item.abs_path.is_file():
                    try:
                        item.abs_path.unlink()
                        deleted_files.append(item.rel_path)
                    except OSError as exc:
                        errors.append({"file": item.rel_path, "error": str(exc)})
            else:
                kept += 1

        return {
            "total_items": len(self.items),
            "kept": kept,
            "deleted": deleted,
            "deleted_files": deleted_files,
            "errors": errors,
        }


def _render_html(
    pair_manager: PairReviewManager | None = None,
    chat_manager: ChatMediaReviewManager | None = None,
    initial_mode: str = "auto",
) -> str:
    pairs_list = [
        {
            "index": item.index,
            "main_rel": item.main_rel_path,
            "comp_rel": item.composite_rel_path,
            "choice": item.choice,
        }
        for item in (pair_manager.items if pair_manager else [])
    ]
    pairs_json = json.dumps(pairs_list)

    chat_list = [
        {
            "index": item.index,
            "rel_path": item.rel_path,
            "filename": item.filename,
            "is_video": item.is_video,
            "size_bytes": item.size_bytes,
            "media_type": item.media_type,
            "choice": item.choice,
        }
        for item in (chat_manager.items if chat_manager else [])
    ]
    chat_json = json.dumps(chat_list)

    has_pairs = bool(pairs_list)
    has_chat = bool(chat_list)

    if initial_mode == "chat_media" and has_chat:
        default_tab = "chat"
    elif initial_mode == "overlays" and has_pairs:
        default_tab = "overlays"
    else:
        default_tab = "overlays" if has_pairs else ("chat" if has_chat else "overlays")

    show_tabs_style = "display: flex;" if (has_pairs and has_chat) else "display: none;"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Snapchat Export — Review Overlay Pairs & Chat Media</title>
<style>
  :root {{
    --bg: #18181b;
    --card: #27272a;
    --text: #fafafa;
    --border: #3f3f46;
    --accent: #2563eb;
    --del: #dc2626;
    --keep: #16a34a;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 16px;
    line-height: 1.4;
  }}
  header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid var(--border);
    padding-bottom: 10px;
    margin-bottom: 12px;
  }}
  .nav-tabs {{
    display: flex;
    gap: 8px;
    margin-bottom: 14px;
  }}
  .tab-btn {{
    background: #27272a;
    color: #a1a1aa;
    border: 1px solid var(--border);
    padding: 8px 16px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.9rem;
    font-weight: 600;
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }}
  .tab-btn:hover {{ background: #3f3f46; color: #fff; }}
  .tab-btn.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
  .tab-badge {{
    background: rgba(0,0,0,0.35);
    padding: 2px 7px;
    border-radius: 10px;
    font-size: 0.75rem;
  }}
  .toolbar {{
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 14px;
    align-items: center;
  }}
  button, select {{
    background: #3f3f46;
    color: #fff;
    border: 1px solid var(--border);
    padding: 6px 12px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.85rem;
  }}
  button:hover {{ background: #52525b; }}
  button.active {{ background: var(--accent); border-color: var(--accent); }}
  .btn-primary {{
    background: #16a34a;
    font-weight: 600;
    border: none;
    padding: 8px 16px;
  }}
  .btn-primary:hover {{ background: #15803d; }}

  /* Overlay Pair Review Styles */
  .pair-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px;
  }}
  .pair-header {{
    display: flex;
    justify-content: space-between;
    margin-bottom: 10px;
    font-size: 0.9rem;
  }}
  .grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-bottom: 12px;
  }}
  @media (max-width: 768px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  .img-box {{
    background: #09090b;
    border: 2px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    cursor: pointer;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
    user-select: none;
  }}
  .img-box:hover {{
    border-color: #60a5fa;
  }}
  .img-box.selected {{
    border-color: #22c55e;
    box-shadow: 0 0 0 2px rgba(34, 197, 94, 0.4);
  }}
  .img-box.unselected {{
    border-color: #ef4444;
    opacity: 0.6;
  }}
  .img-tag {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 12px;
    font-size: 0.85rem;
    font-weight: 600;
    background: #27272a;
    border-bottom: 1px solid var(--border);
  }}
  .img-badge {{
    font-size: 0.75rem;
    padding: 2px 8px;
    border-radius: 4px;
    font-weight: 700;
    letter-spacing: 0.3px;
  }}
  .badge-keep {{ background: #15803d; color: #f0fdf4; }}
  .badge-del {{ background: #991b1b; color: #fef2f2; }}
  .badge-both {{ background: #1d4ed8; color: #eff6ff; }}
  .img-wrap {{
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 350px;
    max-height: 520px;
    padding: 6px;
  }}
  img {{
    max-width: 100%;
    max-height: 500px;
    object-fit: contain;
    pointer-events: none;
  }}
  .choices {{
    display: flex;
    gap: 8px;
    justify-content: center;
    background: #18181b;
    padding: 10px;
    border-radius: 6px;
    margin-bottom: 10px;
  }}
  .status-text {{
    text-align: center;
    font-size: 0.85rem;
    margin-bottom: 10px;
    min-height: 1.2em;
  }}
  .footer {{
    display: flex;
    justify-content: space-between;
    margin-top: 14px;
  }}

  /* Type navigation bar */
  .type-nav-panel {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
    margin-bottom: 14px;
  }}
  .type-nav-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
  }}
  .type-pills-container {{
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }}
  .type-pill {{
    background: #18181b;
    border: 2px solid var(--border);
    border-radius: 6px;
    padding: 8px 12px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 10px;
    transition: all 0.15s ease;
    user-select: none;
  }}
  .type-pill:hover {{
    border-color: #60a5fa;
    background: #27272a;
  }}
  .type-pill.active {{
    border-color: #3b82f6;
    background: #1e3a8a;
  }}
  .type-pill-info {{
    display: flex;
    flex-direction: column;
  }}
  .type-pill-name {{
    font-weight: 700;
    font-size: 0.85rem;
    color: #fff;
  }}
  .type-pill-count {{
    font-size: 0.72rem;
    color: #a1a1aa;
  }}
  .btn-pill-del {{
    background: #991b1b;
    color: #fff;
    border: none;
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 0.72rem;
    font-weight: 700;
    cursor: pointer;
  }}
  .btn-pill-del:hover {{
    background: #dc2626;
  }}

  /* Chat Media Grid Styles */
  .chat-grid-container {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 12px;
    margin-bottom: 16px;
    min-height: 250px;
  }}
  .chat-card {{
    background: #09090b;
    border: 2px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    cursor: pointer;
    position: relative;
    transition: transform 0.12s ease, border-color 0.12s ease, box-shadow 0.12s ease;
    display: flex;
    flex-direction: column;
    user-select: none;
  }}
  .chat-card:hover {{
    transform: translateY(-2px);
    border-color: #60a5fa;
  }}
  .chat-card.is-keep {{
    border-color: #22c55e;
  }}
  .chat-card.is-delete {{
    border-color: #ef4444;
    background: #2a1215;
  }}
  .chat-card.is-delete .chat-thumb-wrap::after {{
    content: "✕ DELETE";
    position: absolute;
    inset: 0;
    background: rgba(220, 38, 38, 0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    color: #fff;
    font-weight: 800;
    font-size: 1.05rem;
    letter-spacing: 0.5px;
    text-shadow: 0 1px 4px rgba(0,0,0,0.9);
  }}
  .chat-thumb-wrap {{
    position: relative;
    width: 100%;
    aspect-ratio: 1 / 1;
    background: #000;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
  }}
  .chat-thumb-wrap img, .chat-thumb-wrap video {{
    width: 100%;
    height: 100%;
    object-fit: cover;
    pointer-events: none;
  }}
  .chat-pill {{
    position: absolute;
    top: 6px;
    left: 6px;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.68rem;
    font-weight: 700;
    text-transform: uppercase;
    z-index: 2;
  }}
  .pill-keep {{ background: #15803d; color: #f0fdf4; }}
  .pill-del {{ background: #991b1b; color: #fef2f2; }}
  .pill-type-tag {{
    position: absolute;
    bottom: 6px;
    left: 6px;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    background: rgba(0,0,0,0.8);
    color: #f4f4f5;
    z-index: 2;
  }}
  .pill-vid {{ background: rgba(0,0,0,0.8); color: #38bdf8; right: 6px; left: auto; }}
  .chat-card-meta {{
    padding: 6px 8px;
    font-size: 0.72rem;
    background: #18181b;
    border-top: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
    color: #a1a1aa;
  }}
  .chat-card-title {{
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 110px;
    font-family: monospace;
  }}

  /* Chat Single View Styles */
  .chat-single-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
  }}
  .chat-single-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
  }}
  .chat-single-media-box {{
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 420px;
    max-height: 580px;
    background: #09090b;
    border: 2px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    margin-bottom: 14px;
    cursor: pointer;
    position: relative;
    transition: border-color 0.15s ease;
  }}
  .chat-single-media-box.is-keep {{ border-color: #22c55e; }}
  .chat-single-media-box.is-delete {{ border-color: #ef4444; }}
  .chat-single-media-box img, .chat-single-media-box video {{
    max-width: 100%;
    max-height: 560px;
    object-fit: contain;
  }}

  /* Modals */
  .modal {{
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.8);
    justify-content: center;
    align-items: center;
    z-index: 50;
  }}
  .modal-inner {{
    background: var(--card);
    border: 1px solid var(--border);
    padding: 24px;
    border-radius: 8px;
    text-align: center;
    max-width: 440px;
  }}
</style>
</head>
<body>
  <header>
    <h3>Snapchat Export — Review & Filtering</h3>
    <span id="summary-badge" style="font-size: 0.85rem; color: #a1a1aa;"></span>
  </header>

  <div class="nav-tabs" id="main-nav-tabs" style="{show_tabs_style}">
    <button class="tab-btn" id="tab-btn-overlays" onclick="switchMainTab('overlays')">
      🖼️ Overlay Pairs <span class="tab-badge" id="badge-count-overlays">{len(pairs_list)}</span>
    </button>
    <button class="tab-btn" id="tab-btn-chat" onclick="switchMainTab('chat')">
      💬 Chat Media Filter <span class="tab-badge" id="badge-count-chat">{len(chat_list)}</span>
    </button>
  </div>

  <!-- VIEW 1: OVERLAY PAIRS -->
  <div id="view-overlays" style="display: none;">
    <div class="toolbar">
      <span>Batch:</span>
      <button onclick="setAll('both')">All Keep Both</button>
      <button onclick="setAll('without_overlay')">All Without Overlay</button>
      <button onclick="setAll('with_overlay')">All With Overlay</button>
      <div style="flex: 1;"></div>
      <select id="jump-select" onchange="renderPair(+this.value)"></select>
    </div>

    <div class="pair-card">
      <div class="pair-header">
        <strong>Pair <span id="idx-cur">1</span> of <span id="idx-tot">0</span></strong>
        <span id="file-title" style="font-family: monospace; color: #93c5fd;"></span>
      </div>

      <div class="grid">
        <div class="img-box" id="box-orig" onclick="setChoice('without_overlay')" title="Click image to prefer Without Overlay">
          <div class="img-tag">
            <span style="color: #60a5fa;">Without Overlay (Original Main)</span>
            <span id="badge-orig" class="img-badge"></span>
          </div>
          <div class="img-wrap"><img id="img-orig" alt="Original"></div>
        </div>
        <div class="img-box" id="box-comp" onclick="setChoice('with_overlay')" title="Click image to prefer With Overlay">
          <div class="img-tag">
            <span style="color: #4ade80;">With Overlay (Composite)</span>
            <span id="badge-comp" class="img-badge"></span>
          </div>
          <div class="img-wrap"><img id="img-comp" alt="Composite"></div>
        </div>
      </div>

      <div class="choices">
        <button id="btn-both" onclick="setChoice('both')">Keep Both (1)</button>
        <button id="btn-without" onclick="setChoice('without_overlay')">Keep Without Overlay (2)</button>
        <button id="btn-with" onclick="setChoice('with_overlay')">Keep With Overlay (3)</button>
      </div>

      <div class="status-text" id="status-text"></div>

      <div class="footer">
        <div>
          <button id="btn-prev" onclick="move(-1)">◀ Prev (P)</button>
          <button id="btn-next" onclick="move(1)">Next (N) ▶</button>
        </div>
        <button class="btn-primary" onclick="applyDeletions()">Apply Choices & Delete Unchosen</button>
      </div>
    </div>
  </div>

  <!-- VIEW 2: CHAT MEDIA FILTER -->
  <div id="view-chat" style="display: none;">
    <!-- Type / Category Bar -->
    <div class="type-nav-panel">
      <div class="type-nav-header">
        <span style="font-weight: 700; font-size: 0.9rem;">Categories / Media Types:</span>
        <span id="group-action-bar"></span>
      </div>
      <div class="type-pills-container" id="type-pills-container"></div>
    </div>

    <!-- Filter & View Controls Toolbar -->
    <div class="toolbar" style="justify-content: space-between;">
      <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
        <span style="font-weight: 600; font-size: 0.85rem; color: #93c5fd;">View:</span>
        <button id="btn-chat-view-grid" class="active" onclick="setChatViewMode('grid')">⊞ Grid</button>
        <button id="btn-chat-view-single" onclick="setChatViewMode('single')">🗖 Single (V)</button>
        <span style="color: var(--border);">|</span>
        <span style="font-size: 0.85rem; color: #a1a1aa;">Status:</span>
        <select id="chat-filter" onchange="changeChatFilter(this.value)">
          <option value="all">All Statuses</option>
          <option value="keep">Marked Keep</option>
          <option value="delete">Marked for Deletion</option>
        </select>
        <span style="color: var(--border);">|</span>
        <button onclick="batchMarkVisible('delete')">Mark Page to Delete</button>
        <button onclick="batchMarkVisible('keep')">Mark Page to Keep</button>
        <button onclick="batchInvertVisible()">Invert Page</button>
      </div>
      <div style="display: flex; gap: 8px; align-items: center;">
        <button id="btn-chat-prev-page" onclick="changeChatPage(-1)">◀ Prev ([)</button>
        <span id="chat-page-indicator" style="font-size: 0.85rem; font-family: monospace;">Page 1</span>
        <button id="btn-chat-next-page" onclick="changeChatPage(1)">Next (]) ▶</button>
      </div>
    </div>

    <!-- Grid View Mode -->
    <div id="chat-grid-view">
      <div class="chat-grid-container" id="chat-grid"></div>
    </div>

    <!-- Single Item View Mode -->
    <div class="chat-single-card" id="chat-single-view" style="display: none;">
      <div class="chat-single-header">
        <strong>Item <span id="chat-single-cur">1</span> of <span id="chat-single-total">0</span>: <span id="chat-single-name" style="font-family: monospace; color: #93c5fd;"></span> (<span id="chat-single-size"></span>) <span id="chat-single-type" style="color: #a1a1aa; font-size: 0.8rem; margin-left: 6px;"></span></strong>
        <span id="chat-single-badge" class="img-badge badge-keep">KEEP</span>
      </div>
      <div class="chat-single-media-box" id="chat-single-box" onclick="toggleChatChoiceAndAdvance()" title="Click to toggle choice & advance"></div>
      <div class="choices">
        <button id="btn-chat-single-del" style="background: #991b1b; color: #fff; font-weight: 600;" onclick="setChatSingleChoice('delete')">✕ Mark to Delete (D / ↓)</button>
        <button id="btn-chat-single-keep" style="background: #15803d; color: #fff; font-weight: 600;" onclick="setChatSingleChoice('keep')">✓ Keep Media (K / Enter / Space / →)</button>
      </div>
      <div class="footer">
        <div>
          <button onclick="moveChatSingle(-1)">◀ Prev (P / ←)</button>
          <button onclick="moveChatSingle(1)">Next (N / →) ▶</button>
        </div>
        <button onclick="setChatViewMode('grid')">Back to Grid (V)</button>
      </div>
    </div>

    <!-- Chat Media Bottom Summary & Action Bar -->
    <div style="background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; display: flex; justify-content: space-between; align-items: center; margin-top: 14px;">
      <div style="font-size: 0.9rem;">
        <strong>Chat Media Summary:</strong>
        Total: <span id="chat-stat-total">0</span> |
        <span style="color: #4ade80;">Keep: <span id="chat-stat-keep">0</span></span> |
        <span style="color: #f87171; font-weight: 600;">Delete: <span id="chat-stat-del">0</span> (<span id="chat-stat-size">0 B</span>)</span>
      </div>
      <button class="btn-primary" style="background: #dc2626;" onclick="applyChatDeletions()">Apply Chat Deletions</button>
    </div>
  </div>

  <div class="modal" id="done-modal">
    <div class="modal-inner">
      <h3 style="color: #4ade80; margin-bottom: 10px;">Review Applied</h3>
      <p id="done-msg" style="margin-bottom: 16px; font-size: 0.9rem;"></p>
      <p style="color: #a1a1aa; font-size: 0.8rem;">You may now close this browser tab.</p>
    </div>
  </div>

<script>
  const pairs = {pairs_json};
  const chatItems = {chat_json};
  let activeMainTab = "{default_tab}";

  let cur = 0;
  let advanceTimer = null;

  // Chat Media State
  let chatViewMode = "grid";
  let chatPage = 0;
  const chatPageSize = 36;
  let chatFilter = "all";
  let activeChatType = "all";
  let chatCurIndex = 0;
  let chatAdvanceTimer = null;

  const TYPE_DEFS = {{
    all: {{ label: "All Media", icon: "📁" }},
    photos: {{ label: "Photos", icon: "📷" }},
    thumbnails: {{ label: "Thumbnails", icon: "🖼️" }},
    overlays: {{ label: "Overlays", icon: "🎨" }},
    videos: {{ label: "Videos", icon: "🎬" }},
    stickers: {{ label: "Stickers", icon: "👾" }},
    other: {{ label: "Other", icon: "📄" }},
  }};

  function formatBytes(bytes) {{
    if (!bytes || bytes <= 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
  }}

  function switchMainTab(tab) {{
    activeMainTab = tab;
    document.getElementById("view-overlays").style.display = tab === "overlays" ? "block" : "none";
    document.getElementById("view-chat").style.display = tab === "chat" ? "block" : "none";

    const btnOverlays = document.getElementById("tab-btn-overlays");
    const btnChat = document.getElementById("tab-btn-chat");
    if (btnOverlays) btnOverlays.classList.toggle("active", tab === "overlays");
    if (btnChat) btnChat.classList.toggle("active", tab === "chat");

    if (tab === "overlays" && pairs.length > 0) renderPair(cur);
    else if (tab === "chat" && chatItems.length > 0) renderChatView();
  }}

  // ------------------ OVERLAY PAIR FUNCTIONS ------------------
  function initOverlays() {{
    document.getElementById("idx-tot").textContent = pairs.length;
    const sel = document.getElementById("jump-select");
    pairs.forEach((p, i) => {{
      const opt = document.createElement("option");
      opt.value = i;
      opt.textContent = `${{i + 1}}: ${{p.main_rel.split('/').pop()}}`;
      sel.appendChild(opt);
    }});
    if (pairs.length > 0) renderPair(0);
  }}

  function renderPair(idx) {{
    if (idx < 0 || idx >= pairs.length) return;
    cur = idx;
    const p = pairs[idx];
    document.getElementById("idx-cur").textContent = idx + 1;
    document.getElementById("file-title").textContent = p.main_rel;
    document.getElementById("jump-select").value = idx;
    document.getElementById("img-orig").src = `/image?path=${{encodeURIComponent(p.main_rel)}}`;
    document.getElementById("img-comp").src = `/image?path=${{encodeURIComponent(p.comp_rel)}}`;

    document.getElementById("btn-both").classList.toggle("active", p.choice === "both");
    document.getElementById("btn-without").classList.toggle("active", p.choice === "without_overlay");
    document.getElementById("btn-with").classList.toggle("active", p.choice === "with_overlay");

    const boxOrig = document.getElementById("box-orig");
    const boxComp = document.getElementById("box-comp");
    const badgeOrig = document.getElementById("badge-orig");
    const badgeComp = document.getElementById("badge-comp");
    const st = document.getElementById("status-text");

    boxOrig.classList.remove("selected", "unselected");
    boxComp.classList.remove("selected", "unselected");

    if (p.choice === "without_overlay") {{
      boxOrig.classList.add("selected");
      boxComp.classList.add("unselected");
      badgeOrig.className = "img-badge badge-keep";
      badgeOrig.textContent = "✓ Preferred (Keeping)";
      badgeComp.className = "img-badge badge-del";
      badgeComp.textContent = "✕ Will Delete";
      st.innerHTML = '<span style="color: #60a5fa; font-weight: 600;">Preferred: Without Overlay</span> &bull; <strong style="color: #f87171;">Will delete composite</strong>';
    }} else if (p.choice === "with_overlay") {{
      boxOrig.classList.add("unselected");
      boxComp.classList.add("selected");
      badgeOrig.className = "img-badge badge-del";
      badgeOrig.textContent = "✕ Will Delete";
      badgeComp.className = "img-badge badge-keep";
      badgeComp.textContent = "✓ Preferred (Keeping)";
      st.innerHTML = '<span style="color: #4ade80; font-weight: 600;">Preferred: With Overlay</span> &bull; <strong style="color: #f87171;">Will delete original</strong>';
    }} else {{
      badgeOrig.className = "img-badge badge-both";
      badgeOrig.textContent = "Keeping";
      badgeComp.className = "img-badge badge-both";
      badgeComp.textContent = "Keeping";
      st.innerHTML = '<span style="color: #60a5fa;">Keeping both files</span>';
    }}

    document.getElementById("btn-prev").disabled = idx === 0;
    document.getElementById("btn-next").disabled = idx === pairs.length - 1;

    const both = pairs.filter(p => p.choice === "both").length;
    const ov = pairs.filter(p => p.choice === "with_overlay").length;
    const noOv = pairs.filter(p => p.choice === "without_overlay").length;
    document.getElementById("summary-badge").textContent = `Overlays: Both ${{both}} | Overlay ${{ov}} | Without ${{noOv}}`;
  }}

  function setChoice(c, autoAdvance = true) {{
    if (advanceTimer) clearTimeout(advanceTimer);
    pairs[cur].choice = c;
    fetch("/api/set_choice", {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify({{index: cur, choice: c}})}});
    renderPair(cur);
    if (autoAdvance && cur < pairs.length - 1) {{
      advanceTimer = setTimeout(() => move(1), 200);
    }}
  }}

  function setAll(c) {{
    if (advanceTimer) clearTimeout(advanceTimer);
    pairs.forEach(p => p.choice = c);
    fetch("/api/set_all", {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify({{choice: c}})}});
    renderPair(cur);
  }}

  function move(d) {{
    if (advanceTimer) clearTimeout(advanceTimer);
    renderPair(cur + d);
  }}

  function applyDeletions() {{
    if (advanceTimer) clearTimeout(advanceTimer);
    const count = pairs.filter(p => p.choice !== "both").length;
    if (!confirm(`Delete ${{count}} unchosen overlay file(s)?`)) return;
    fetch("/api/apply", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{choices: Object.fromEntries(pairs.map(p => [p.index, p.choice]))}})
    }})
    .then(r => r.json())
    .then(data => {{
      const res = data.result || {{}};
      document.getElementById("done-msg").textContent = `Overlay Review: Deleted ${{res.deleted_files ? res.deleted_files.length : 0}} file(s). Kept: ${{res.kept_both}} both, ${{res.kept_with_overlay}} with overlay, ${{res.kept_without_overlay}} without overlay.`;
      document.getElementById("done-modal").style.display = "flex";
    }});
  }}

  // ------------------ CHAT MEDIA FUNCTIONS ------------------
  function getFilteredChatItems() {{
    return chatItems.filter(it => {{
      if (it.choice === "deleted") return false;
      if (activeChatType !== "all" && it.media_type !== activeChatType) return false;
      if (chatFilter === "keep") return it.choice === "keep";
      if (chatFilter === "delete") return it.choice === "delete";
      return true;
    }});
  }}

  function setChatViewMode(mode) {{
    chatViewMode = mode;
    document.getElementById("btn-chat-view-grid").classList.toggle("active", mode === "grid");
    document.getElementById("btn-chat-view-single").classList.toggle("active", mode === "single");
    document.getElementById("chat-grid-view").style.display = mode === "grid" ? "block" : "none";
    document.getElementById("chat-single-view").style.display = mode === "single" ? "block" : "none";
    renderChatView();
  }}

  function changeChatFilter(val) {{
    chatFilter = val;
    chatPage = 0;
    renderChatView();
  }}

  function changeChatPage(d) {{
    const filtered = getFilteredChatItems();
    const maxPage = Math.max(0, Math.ceil(filtered.length / chatPageSize) - 1);
    chatPage = Math.min(maxPage, Math.max(0, chatPage + d));
    renderChatView();
  }}

  function renderTypePills() {{
    const container = document.getElementById("type-pills-container");
    container.innerHTML = "";

    const counts = {{ all: 0 }};
    const bytes = {{ all: 0 }};
    chatItems.forEach(it => {{
      if (it.choice === "deleted") return;
      const t = it.media_type || "other";
      counts[t] = (counts[t] || 0) + 1;
      bytes[t] = (bytes[t] || 0) + (it.size_bytes || 0);
      counts.all += 1;
      bytes.all += (it.size_bytes || 0);
    }});

    const typesToShow = ["all", "photos", "thumbnails", "overlays", "videos", "stickers", "other"].filter(
      t => t === "all" || (counts[t] || 0) > 0
    );

    typesToShow.forEach(t => {{
      const def = TYPE_DEFS[t] || {{ label: t, icon: "📁" }};
      const pill = document.createElement("div");
      pill.className = `type-pill ${{activeChatType === t ? "active" : ""}}`;
      pill.onclick = () => selectChatType(t);

      const info = document.createElement("div");
      info.className = "type-pill-info";
      info.innerHTML = `<span class="type-pill-name">${{def.icon}} ${{def.label}}</span><span class="type-pill-count">${{counts[t] || 0}} files (${{formatBytes(bytes[t] || 0)}})</span>`;
      pill.appendChild(info);

      if (t !== "all" && (counts[t] || 0) > 0) {{
        const delBtn = document.createElement("button");
        delBtn.className = "btn-pill-del";
        delBtn.title = `Delete all ${{counts[t]}} ${{def.label}}`;
        delBtn.textContent = "🗑️ Delete Group";
        delBtn.onclick = (e) => {{
          e.stopPropagation();
          deleteGroupNow(t);
        }};
        pill.appendChild(delBtn);
      }}

      container.appendChild(pill);
    }});

    const actionBar = document.getElementById("group-action-bar");
    if (activeChatType !== "all") {{
      const def = TYPE_DEFS[activeChatType] || {{ label: activeChatType }};
      const c = counts[activeChatType] || 0;
      const b = bytes[activeChatType] || 0;
      actionBar.innerHTML = `
        <span style="font-size: 0.82rem; margin-right: 8px; color: #93c5fd;">Active: <strong>${{def.label}}</strong> (${{c}} files)</span>
        <button style="padding: 4px 8px; font-size: 0.75rem;" onclick="markGroupChoice('${{activeChatType}}', 'delete')">Mark All as Delete</button>
        <button style="padding: 4px 8px; font-size: 0.75rem;" onclick="markGroupChoice('${{activeChatType}}', 'keep')">Mark All as Keep</button>
        <button class="btn-primary" style="padding: 4px 10px; font-size: 0.75rem; background: #dc2626;" onclick="deleteGroupNow('${{activeChatType}}')">🗑️ Delete All ${{def.label}} Now (${{formatBytes(b)}})</button>
      `;
    }} else {{
      actionBar.innerHTML = '<span style="font-size: 0.8rem; color: #a1a1aa;">Viewing all categories</span>';
    }}
  }}

  function selectChatType(t) {{
    activeChatType = t;
    chatPage = 0;
    renderChatView();
  }}

  function markGroupChoice(group, choice) {{
    chatItems.forEach(it => {{
      if (it.choice !== "deleted" && (group === "all" || it.media_type === group)) {{
        it.choice = choice;
      }}
    }});
    fetch("/api/chat/set_group_choice", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{group: group, choice: choice}})
    }});
    renderChatView();
  }}

  function deleteGroupNow(group) {{
    const def = TYPE_DEFS[group] || {{ label: group }};
    const targetItems = chatItems.filter(it => it.choice !== "deleted" && (group === "all" || it.media_type === group));
    const totalBytes = targetItems.reduce((acc, it) => acc + (it.size_bytes || 0), 0);
    if (!targetItems.length) return;

    if (!confirm(`Permanently delete all ${{targetItems.length}} ${{def.label}} files (${{formatBytes(totalBytes)}}) right now? This cannot be undone.`)) return;

    fetch("/api/chat/delete_group", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{group: group}})
    }})
    .then(r => r.json())
    .then(data => {{
      const res = data.result || {{}};
      targetItems.forEach(it => {{ it.choice = "deleted"; }});
      renderChatView();
      alert(`Deleted ${{res.deleted || 0}} ${{def.label}} files (${{formatBytes(totalBytes)}} freed).`);
    }});
  }}

  function renderChatStats() {{
    const nonDeleted = chatItems.filter(it => it.choice !== "deleted");
    const total = nonDeleted.length;
    const keep = nonDeleted.filter(it => it.choice === "keep").length;
    const delItems = nonDeleted.filter(it => it.choice === "delete");
    const delCount = delItems.length;
    const delBytes = delItems.reduce((acc, it) => acc + (it.size_bytes || 0), 0);

    document.getElementById("chat-stat-total").textContent = total;
    document.getElementById("chat-stat-keep").textContent = keep;
    document.getElementById("chat-stat-del").textContent = delCount;
    document.getElementById("chat-stat-size").textContent = formatBytes(delBytes);

    if (activeMainTab === "chat") {{
      document.getElementById("summary-badge").textContent = `Chat Media: Keep ${{keep}} | Delete ${{delCount}} (${{formatBytes(delBytes)}})`;
    }}

    renderTypePills();
  }}

  function renderChatView() {{
    renderChatStats();
    if (chatViewMode === "grid") {{
      renderChatGrid();
    }} else {{
      renderChatSingle();
    }}
  }}

  function renderChatGrid() {{
    const filtered = getFilteredChatItems();
    const totalPages = Math.max(1, Math.ceil(filtered.length / chatPageSize));
    if (chatPage >= totalPages) chatPage = totalPages - 1;

    document.getElementById("chat-page-indicator").textContent = `Page ${{chatPage + 1}} of ${{totalPages}} (${{filtered.length}} items)`;
    document.getElementById("btn-chat-prev-page").disabled = chatPage === 0;
    document.getElementById("btn-chat-next-page").disabled = chatPage >= totalPages - 1;

    const grid = document.getElementById("chat-grid");
    grid.innerHTML = "";

    const start = chatPage * chatPageSize;
    const pageItems = filtered.slice(start, start + chatPageSize);

    if (pageItems.length === 0) {{
      grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: #a1a1aa; padding: 40px;">No media files match this filter or category.</div>';
      return;
    }}

    pageItems.forEach(it => {{
      const card = document.createElement("div");
      card.className = `chat-card ${{it.choice === "delete" ? "is-delete" : "is-keep"}}`;
      card.id = `chat-card-${{it.index}}`;
      card.onclick = () => toggleChatChoice(it.index);

      const wrap = document.createElement("div");
      wrap.className = "chat-thumb-wrap";

      if (it.is_video) {{
        const vid = document.createElement("video");
        vid.src = `/image?path=${{encodeURIComponent(it.rel_path)}}`;
        vid.muted = true;
        vid.playsInline = true;
        vid.preload = "metadata";
        card.onmouseenter = () => {{ vid.play().catch(() => {{}}); }};
        card.onmouseleave = () => {{ vid.pause(); vid.currentTime = 0; }};
        wrap.appendChild(vid);

        const vPill = document.createElement("span");
        vPill.className = "chat-pill pill-vid";
        vPill.textContent = "▶ VIDEO";
        wrap.appendChild(vPill);
      }} else {{
        const img = document.createElement("img");
        img.src = `/image?path=${{encodeURIComponent(it.rel_path)}}`;
        img.loading = "lazy";
        img.alt = it.filename;
        wrap.appendChild(img);
      }}

      const statusPill = document.createElement("span");
      statusPill.className = `chat-pill ${{it.choice === "delete" ? "pill-del" : "pill-keep"}}`;
      statusPill.id = `chat-pill-${{it.index}}`;
      statusPill.textContent = it.choice === "delete" ? "✕ DEL" : "✓ KEEP";
      wrap.appendChild(statusPill);

      const typeTag = document.createElement("span");
      typeTag.className = "pill-type-tag";
      typeTag.textContent = (TYPE_DEFS[it.media_type] ? TYPE_DEFS[it.media_type].label : it.media_type);
      wrap.appendChild(typeTag);

      const meta = document.createElement("div");
      meta.className = "chat-card-meta";
      meta.innerHTML = `<span class="chat-card-title" title="${{it.filename}}">${{it.filename}}</span><span>${{formatBytes(it.size_bytes)}}</span>`;

      card.appendChild(wrap);
      card.appendChild(meta);
      grid.appendChild(card);
    }});
  }}

  function toggleChatChoice(idx) {{
    const it = chatItems[idx];
    if (!it || it.choice === "deleted") return;
    it.choice = it.choice === "keep" ? "delete" : "keep";
    fetch("/api/chat/set_choice", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{index: idx, choice: it.choice}})
    }});

    const card = document.getElementById(`chat-card-${{idx}}`);
    if (card) {{
      card.className = `chat-card ${{it.choice === "delete" ? "is-delete" : "is-keep"}}`;
      const pill = document.getElementById(`chat-pill-${{idx}}`);
      if (pill) {{
        pill.className = `chat-pill ${{it.choice === "delete" ? "pill-del" : "pill-keep"}}`;
        pill.textContent = it.choice === "delete" ? "✕ DEL" : "✓ KEEP";
      }}
    }}
    renderChatStats();
  }}

  function batchMarkVisible(choice) {{
    const filtered = getFilteredChatItems();
    const start = chatPage * chatPageSize;
    const visible = filtered.slice(start, start + chatPageSize);
    const indices = visible.map(it => it.index);
    if (!indices.length) return;

    indices.forEach(i => {{ chatItems[i].choice = choice; }});
    fetch("/api/chat/set_bulk", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{indices: indices, choice: choice}})
    }});
    renderChatView();
  }}

  function batchInvertVisible() {{
    const filtered = getFilteredChatItems();
    const start = chatPage * chatPageSize;
    const visible = filtered.slice(start, start + chatPageSize);
    if (!visible.length) return;

    visible.forEach(it => {{
      it.choice = it.choice === "keep" ? "delete" : "keep";
    }});
    const toDelete = visible.filter(it => it.choice === "delete").map(it => it.index);
    const toKeep = visible.filter(it => it.choice === "keep").map(it => it.index);

    if (toDelete.length) {{
      fetch("/api/chat/set_bulk", {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify({{indices: toDelete, choice: "delete"}})}});
    }}
    if (toKeep.length) {{
      fetch("/api/chat/set_bulk", {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify({{indices: toKeep, choice: "keep"}})}});
    }}
    renderChatView();
  }}

  // ------------------ CHAT SINGLE ITEM FUNCTIONS ------------------
  function renderChatSingle() {{
    const filtered = getFilteredChatItems();
    if (filtered.length === 0) {{
      document.getElementById("chat-single-view").style.display = "none";
      document.getElementById("chat-grid-view").style.display = "block";
      return;
    }}
    if (chatCurIndex < 0) chatCurIndex = 0;
    if (chatCurIndex >= filtered.length) chatCurIndex = filtered.length - 1;

    const it = filtered[chatCurIndex];
    document.getElementById("chat-single-cur").textContent = chatCurIndex + 1;
    document.getElementById("chat-single-total").textContent = filtered.length;
    document.getElementById("chat-single-name").textContent = it.filename;
    document.getElementById("chat-single-size").textContent = formatBytes(it.size_bytes);
    document.getElementById("chat-single-type").textContent = `[${{(TYPE_DEFS[it.media_type] ? TYPE_DEFS[it.media_type].label : it.media_type).toUpperCase()}}]`;

    const badge = document.getElementById("chat-single-badge");
    badge.className = `img-badge ${{it.choice === "delete" ? "badge-del" : "badge-keep"}}`;
    badge.textContent = it.choice === "delete" ? "✕ WILL DELETE" : "✓ KEEPING";

    const box = document.getElementById("chat-single-box");
    box.className = `chat-single-media-box ${{it.choice === "delete" ? "is-delete" : "is-keep"}}`;
    box.innerHTML = "";

    if (it.is_video) {{
      const vid = document.createElement("video");
      vid.src = `/image?path=${{encodeURIComponent(it.rel_path)}}`;
      vid.controls = true;
      vid.autoplay = true;
      vid.loop = true;
      vid.muted = true;
      box.appendChild(vid);
    }} else {{
      const img = document.createElement("img");
      img.src = `/image?path=${{encodeURIComponent(it.rel_path)}}`;
      img.alt = it.filename;
      box.appendChild(img);
    }}
  }}

  function setChatSingleChoice(c, autoAdvance = true) {{
    if (chatAdvanceTimer) clearTimeout(chatAdvanceTimer);
    const filtered = getFilteredChatItems();
    const it = filtered[chatCurIndex];
    if (!it) return;
    it.choice = c;
    fetch("/api/chat/set_choice", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{index: it.index, choice: c}})
    }});
    renderChatSingle();
    renderChatStats();

    if (autoAdvance && chatCurIndex < filtered.length - 1) {{
      chatAdvanceTimer = setTimeout(() => moveChatSingle(1), 160);
    }}
  }}

  function toggleChatChoiceAndAdvance() {{
    const filtered = getFilteredChatItems();
    const it = filtered[chatCurIndex];
    if (!it) return;
    setChatSingleChoice(it.choice === "keep" ? "delete" : "keep", true);
  }}

  function moveChatSingle(d) {{
    if (chatAdvanceTimer) clearTimeout(chatAdvanceTimer);
    const filtered = getFilteredChatItems();
    chatCurIndex = Math.min(filtered.length - 1, Math.max(0, chatCurIndex + d));
    renderChatSingle();
  }}

  function applyChatDeletions() {{
    const toDel = chatItems.filter(it => it.choice === "delete");
    const totalBytes = toDel.reduce((acc, it) => acc + (it.size_bytes || 0), 0);
    if (!toDel.length) {{
      alert("No chat media files are currently marked for deletion.");
      return;
    }}
    if (!confirm(`Permanently delete ${{toDel.length}} marked file(s) (${{formatBytes(totalBytes)}})? This cannot be undone.`)) return;

    fetch("/api/chat/apply", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{choices: Object.fromEntries(chatItems.map(it => [it.index, it.choice]))}})
    }})
    .then(r => r.json())
    .then(data => {{
      const res = data.result || {{}};
      document.getElementById("done-msg").textContent = `Chat Media Review: Deleted ${{res.deleted || 0}} file(s) (${{formatBytes(totalBytes)}}). Kept ${{res.kept || 0}} file(s).`;
      document.getElementById("done-modal").style.display = "flex";
    }});
  }}

  // ------------------ GLOBAL KEYBOARD SHORTCUTS ------------------
  window.addEventListener("keydown", (e) => {{
    if (activeMainTab === "overlays") {{
      if (e.key === "ArrowLeft" || e.key === "p") move(-1);
      else if (e.key === "ArrowRight" || e.key === "n") move(1);
      else if (e.key === "1") setChoice("both");
      else if (e.key === "2") setChoice("without_overlay");
      else if (e.key === "3") setChoice("with_overlay");
    }} else if (activeMainTab === "chat") {{
      if (chatViewMode === "single") {{
        if (e.key === "ArrowLeft" || e.key === "p") moveChatSingle(-1);
        else if (e.key === "ArrowRight" || e.key === "n" || e.key === "k" || e.key === " " || e.key === "Enter") setChatSingleChoice("keep");
        else if (e.key === "ArrowDown" || e.key === "d" || e.key === "x" || e.key === "Delete") setChatSingleChoice("delete");
        else if (e.key === "v") setChatViewMode("grid");
      }} else {{
        if (e.key === "[" || e.key === "PageUp") changeChatPage(-1);
        else if (e.key === "]" || e.key === "PageDown") changeChatPage(1);
        else if (e.key === "v") setChatViewMode("single");
      }}
    }}
  }});

  // Initial Boot
  if (pairs.length > 0) initOverlays();
  if (chatItems.length > 0) renderChatStats();
  switchMainTab(activeMainTab);
</script>
</body>
</html>
"""


def launch_overlay_review_gui(
    output_root: pathlib.Path,
    composites: list[dict[str, str]] | None = None,
    chat_media: list[dict[str, Any]] | None = None,
    mode: str = "auto",
    port: int = 0,
    open_browser: bool = True,
    logger: logging.Logger | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if composites is None and mode in ("auto", "overlays", "both"):
        composites = discover_overlay_pairs(output_root)
    if chat_media is None and mode in ("auto", "chat_media", "both"):
        chat_media = discover_chat_media(output_root)

    if mode == "overlays":
        chat_media = []
    elif mode == "chat_media":
        composites = []

    pair_manager = PairReviewManager(output_root, composites or [])
    chat_manager = ChatMediaReviewManager(output_root, chat_media or [])

    if not pair_manager.items and not chat_manager.items:
        return {
            "total_pairs": 0,
            "kept_both": 0,
            "kept_with_overlay": 0,
            "kept_without_overlay": 0,
            "deleted_files": [],
            "errors": [],
            "chat_media_total": 0,
            "chat_media_kept": 0,
            "chat_media_deleted": 0,
            "chat_deleted_files": [],
        }

    class RequestHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                content = _render_html(pair_manager, chat_manager, initial_mode=mode).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            elif parsed.path == "/image":
                query = urllib.parse.parse_qs(parsed.query)
                rel_val = query.get("path", [""])[0]
                target_p = (output_root / rel_val).resolve()
                if not target_p.is_relative_to(output_root.resolve()) or not target_p.is_file():
                    self.send_error(404, "File not found")
                    return
                mime_type, _ = mimetypes.guess_type(target_p.name)
                data = target_p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mime_type or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif parsed.path == "/api/pairs":
                data = json.dumps([
                    {
                        "index": item.index,
                        "main_rel": item.main_rel_path,
                        "comp_rel": item.composite_rel_path,
                        "choice": item.choice,
                    }
                    for item in pair_manager.items
                ]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif parsed.path == "/api/chat/items":
                data = json.dumps([
                    {
                        "index": item.index,
                        "rel_path": item.rel_path,
                        "filename": item.filename,
                        "is_video": item.is_video,
                        "size_bytes": item.size_bytes,
                        "media_type": item.media_type,
                        "choice": item.choice,
                    }
                    for item in chat_manager.items
                ]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
            try:
                payload = json.loads(body)
            except Exception:
                payload = {}

            if parsed.path == "/api/set_choice":
                idx = payload.get("index")
                choice = payload.get("choice")
                if isinstance(idx, int) and isinstance(choice, str):
                    pair_manager.set_choice(idx, choice)
                self._send_json({"ok": True, "index": idx, "choice": choice})
            elif parsed.path == "/api/set_all":
                choice = payload.get("choice")
                if isinstance(choice, str):
                    pair_manager.set_all_choices(choice)
                self._send_json({"ok": True, "choice": choice})
            elif parsed.path == "/api/apply":
                choices = payload.get("choices")
                if isinstance(choices, dict):
                    for k, v in choices.items():
                        try:
                            pair_manager.set_choice(int(k), str(v))
                        except ValueError:
                            pass
                result = pair_manager.apply_deletions()
                self.server.applied_overlay_result = result
                self._send_json({"ok": True, "result": result})
                if not chat_manager.items:
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif parsed.path == "/api/chat/set_choice":
                idx = payload.get("index")
                choice = payload.get("choice")
                if isinstance(idx, int) and isinstance(choice, str):
                    chat_manager.set_choice(idx, choice)
                self._send_json({"ok": True, "index": idx, "choice": choice})
            elif parsed.path == "/api/chat/set_bulk":
                indices = payload.get("indices", [])
                choice = payload.get("choice", "keep")
                if isinstance(indices, list) and isinstance(choice, str):
                    chat_manager.set_choices_bulk(indices, choice)
                self._send_json({"ok": True, "count": len(indices), "choice": choice})
            elif parsed.path == "/api/chat/set_group_choice":
                group = payload.get("group", "all")
                choice = payload.get("choice", "keep")
                count = chat_manager.set_group_choice(group, choice)
                self._send_json({"ok": True, "group": group, "choice": choice, "count": count})
            elif parsed.path == "/api/chat/delete_group":
                group = payload.get("group", "all")
                result = chat_manager.delete_group(group)
                if self.server.applied_chat_result is None:
                    self.server.applied_chat_result = {
                        "total_items": len(chat_manager.items),
                        "kept": 0,
                        "deleted": 0,
                        "deleted_files": [],
                        "errors": [],
                    }
                self.server.applied_chat_result["deleted"] += result["deleted"]
                self.server.applied_chat_result["deleted_files"].extend(result["deleted_files"])
                self.server.applied_chat_result["errors"].extend(result["errors"])
                self._send_json({"ok": True, "result": result})
            elif parsed.path == "/api/chat/set_all":
                choice = payload.get("choice", "keep")
                if isinstance(choice, str):
                    chat_manager.set_all_choices(choice)
                self._send_json({"ok": True, "choice": choice})
            elif parsed.path == "/api/chat/apply":
                choices = payload.get("choices")
                if isinstance(choices, dict):
                    for k, v in choices.items():
                        try:
                            chat_manager.set_choice(int(k), str(v))
                        except ValueError:
                            pass
                result = chat_manager.apply_deletions()
                self.server.applied_chat_result = result
                self._send_json({"ok": True, "result": result})
                if not pair_manager.items or self.server.applied_overlay_result is not None:
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif parsed.path == "/api/apply_all":
                res1 = pair_manager.apply_deletions() if pair_manager.items else {}
                res2 = chat_manager.apply_deletions() if chat_manager.items else {}
                self.server.applied_overlay_result = res1
                self.server.applied_chat_result = res2
                self._send_json({"ok": True, "overlay_result": res1, "chat_result": res2})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif parsed.path == "/api/cancel":
                self._send_json({"ok": True, "cancelled": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_error(404)

        def _send_json(self, obj: dict[str, Any]) -> None:
            data = json.dumps(obj).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = http.server.HTTPServer(("127.0.0.1", port), RequestHandler)
    server.applied_overlay_result = None
    server.applied_chat_result = None
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}"
    if logger:
        logger.info("Review GUI available at %s (overlays=%d, chat_media=%d)", url, len(pair_manager.items), len(chat_manager.items))
    print(f"\nReview GUI running at: {url}")
    print(f"Loaded: {len(pair_manager.items)} overlay pairs, {len(chat_manager.items)} chat media items.")
    print("Review media in your browser and click 'Apply Deletions' to finish.\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if logger:
            logger.info("GUI review interrupted by user.")
    finally:
        server.server_close()

    res: dict[str, Any] = {
        "total_pairs": len(pair_manager.items),
        "kept_both": 0,
        "kept_with_overlay": 0,
        "kept_without_overlay": 0,
        "deleted_files": [],
        "errors": [],
    }
    if pair_manager.items:
        ov_res = server.applied_overlay_result or pair_manager.apply_deletions()
        res.update(ov_res)
    if chat_manager.items:
        ch_res = server.applied_chat_result or chat_manager.apply_deletions()
        res["chat_media_total"] = ch_res["total_items"]
        res["chat_media_kept"] = ch_res["kept"]
        res["chat_media_deleted"] = ch_res["deleted"]
        res["chat_deleted_files"] = ch_res["deleted_files"]
        res["deleted_files"] = list(res.get("deleted_files", [])) + list(ch_res["deleted_files"])
        res["errors"] = list(res.get("errors", [])) + list(ch_res["errors"])

    return res


launch_review_gui = launch_overlay_review_gui


def discover_overlay_pairs(output_root: pathlib.Path) -> list[dict[str, str]]:
    if not output_root.is_dir():
        return []
    pairs: list[dict[str, str]] = []
    for root, _, files in os.walk(output_root):
        root_path = pathlib.Path(root)
        for fname in sorted(files):
            p = root_path / fname
            if "-composite" in p.name.lower():
                main_name = re.sub(r"-composite(\.[^.]+)$", r"-main\1", p.name, flags=re.IGNORECASE)
                if main_name == p.name:
                    main_name = p.name.replace("-composite", "")
                main_cand = p.parent / main_name
                if main_cand.is_file():
                    pairs.append({
                        "main_rel_path": main_cand.relative_to(output_root).as_posix(),
                        "composite_rel_path": p.relative_to(output_root).as_posix(),
                    })
    return pairs


def discover_chat_media(output_root: pathlib.Path) -> list[dict[str, Any]]:
    if not output_root.is_dir():
        return []

    if output_root.name.lower() == "chat_media":
        search_dir = output_root
        is_direct = True
    elif (output_root / "chat_media").is_dir():
        search_dir = output_root / "chat_media"
        is_direct = False
    else:
        search_dir = output_root
        is_direct = False

    items: list[dict[str, Any]] = []
    for root, _, files in os.walk(search_dir):
        root_p = pathlib.Path(root)
        if not is_direct and ("debug" in root_p.parts or "memories" in root_p.parts):
            continue
        for fname in sorted(files):
            p = root_p / fname
            ext = p.suffix.lower()
            if ext in ALL_MEDIA_EXTENSIONS:
                try:
                    rel = p.relative_to(output_root).as_posix()
                except ValueError:
                    rel = p.name
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                is_vid = ext in SUPPORTED_VIDEO_EXTENSIONS
                mtype = classify_chat_media_type(rel, p.name, is_vid)
                items.append({
                    "rel_path": rel,
                    "filename": p.name,
                    "is_video": is_vid,
                    "size_bytes": size,
                    "media_type": mtype,
                })
    return items


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Interactive post-processing GUI to review Snapchat image overlay pairs and filter/delete chat media."
    )
    parser.add_argument(
        "output_dir", type=pathlib.Path, nargs="?", default=None,
        help="Path to the processed export output directory.",
    )
    parser.add_argument(
        "--output", "-o", dest="output_opt", type=pathlib.Path, default=None,
        help="Alternative flag to specify the processed export output directory.",
    )
    parser.add_argument(
        "--mode", "-m", choices=["auto", "overlays", "chat_media", "both"], default="auto",
        help="Review mode: 'overlays' for memories main/composite pairs, 'chat_media' to filter/delete memes & screenshots, 'both' for tabs, or 'auto' (default).",
    )
    parser.add_argument(
        "--report", "-r", type=pathlib.Path, default=None,
        help="Optional path to processing report JSON to load overlay pairs from.",
    )
    parser.add_argument(
        "--port", "-p", type=int, default=0,
        help="Port to bind the local review server (default: random free port).",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Do not automatically open default web browser.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    target_dir = args.output_opt or args.output_dir
    if not target_dir:
        print("Error: output directory is required. Specify path as argument or with --output.", file=sys.stderr)
        return 1
    if not target_dir.is_dir():
        print(f"Error: directory '{target_dir}' does not exist.", file=sys.stderr)
        return 1

    mode = args.mode
    pairs: list[dict[str, str]] = []
    chat_media: list[dict[str, Any]] = []

    if mode in ("auto", "overlays", "both"):
        if args.report and args.report.is_file():
            try:
                report_data = json.loads(args.report.read_text(encoding="utf-8"))
                pairs = report_data.get("composites", [])
            except Exception as exc:
                print(f"Warning: failed to read report '{args.report}': {exc}. Falling back to directory scan.", file=sys.stderr)
        if not pairs and mode != "chat_media":
            pairs = discover_overlay_pairs(target_dir)

    if mode in ("auto", "chat_media", "both"):
        chat_media = discover_chat_media(target_dir)

    if not pairs and not chat_media:
        print(f"No overlay pairs or chat media found in '{target_dir}'.", file=sys.stderr)
        return 0

    print(f"Found {len(pairs)} overlay pairs and {len(chat_media)} chat media items in '{target_dir}'.")
    result = launch_overlay_review_gui(
        target_dir,
        composites=pairs,
        chat_media=chat_media,
        mode=mode,
        port=args.port,
        open_browser=not args.no_browser,
    )
    print("\nReview Complete:")
    if pairs or mode in ("overlays", "both"):
        print(f"  • Overlay Pairs: {result.get('total_pairs', 0)} total | {result.get('kept_both', 0)} kept both | {result.get('kept_with_overlay', 0)} with overlay | {result.get('kept_without_overlay', 0)} without overlay")
    if chat_media or mode in ("chat_media", "both"):
        print(f"  • Chat Media: {result.get('chat_media_total', 0)} total | {result.get('chat_media_kept', 0)} kept | {result.get('chat_media_deleted', 0)} deleted")
    print(f"  • Total Deleted Files: {len(result.get('deleted_files', []))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
