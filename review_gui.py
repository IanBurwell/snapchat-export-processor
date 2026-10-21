#!/usr/bin/env python3
"""
Unified interactive review GUI for Snapchat exports:
- Explore both Memories and Chat Media with folder and category filtering.
- Review overlay pairs with side-by-side comparisons (mark one, both, or neither for deletion).
- Bulk selection & deletion across pages, selections, or entire categories.
- Single-view focus mode with rapid keyboard shortcuts and auto-advancing.
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
    choice: str = "both"  # "both", "without_overlay", "with_overlay", "delete_both"


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
        if 0 <= index < len(self.items) and choice in ("both", "without_overlay", "with_overlay", "delete_both", "deleted"):
            self.items[index].choice = choice

    def set_all_choices(self, choice: str) -> None:
        if choice in ("both", "without_overlay", "with_overlay", "delete_both"):
            for item in self.items:
                item.choice = choice

    def apply_deletions(self) -> dict[str, Any]:
        deleted_files: list[str] = []
        errors: list[dict[str, str]] = []
        kept_both = kept_with = kept_without = deleted_both = 0

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
            elif item.choice in ("delete_both", "deleted"):
                deleted_both += 1
                for p, rel in ((item.main_abs_path, item.main_rel_path), (item.composite_abs_path, item.composite_rel_path)):
                    if p.is_file():
                        try:
                            p.unlink()
                            deleted_files.append(rel)
                        except OSError as exc:
                            errors.append({"file": rel, "error": str(exc)})
            else:
                kept_both += 1

        return {
            "total_pairs": len(self.items),
            "kept_both": kept_both,
            "kept_with_overlay": kept_with,
            "kept_without_overlay": kept_without,
            "deleted_both": deleted_both,
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


@dataclass
class ReviewItem:
    index: int
    item_type: str  # "single" or "pair"
    source: str     # "memories", "chat_media", "other"
    media_type: str # "pairs", "photos", "videos", "thumbnails", "overlays", "stickers", "other"
    rel_path: str
    filename: str
    is_video: bool
    size_bytes: int
    main_rel_path: str = ""
    composite_rel_path: str = ""
    main_size: int = 0
    composite_size: int = 0
    choice: str = "keep"  # single: "keep", "delete", "deleted"; pair: "both", "with_overlay", "without_overlay", "delete_both", "deleted"


class UnifiedReviewManager:
    def __init__(self, output_root: pathlib.Path, items: list[dict[str, Any]]) -> None:
        self.output_root = output_root
        self.items: list[ReviewItem] = [
            ReviewItem(
                index=idx,
                item_type=it.get("item_type", "single"),
                source=it.get("source", "other"),
                media_type=it.get("media_type", "photos"),
                rel_path=it["rel_path"],
                filename=it.get("filename") or pathlib.PurePosixPath(it["rel_path"]).name,
                is_video=it.get("is_video", False),
                size_bytes=it.get("size_bytes", 0),
                main_rel_path=it.get("main_rel_path", ""),
                composite_rel_path=it.get("composite_rel_path", ""),
                main_size=it.get("main_size", 0),
                composite_size=it.get("composite_size", 0),
                choice=it.get("choice", "both" if it.get("item_type") == "pair" else "keep"),
            )
            for idx, it in enumerate(items)
        ]

    def set_choice(self, index: int, choice: str) -> None:
        if 0 <= index < len(self.items):
            it = self.items[index]
            if it.item_type == "pair":
                if choice in ("both", "with_overlay", "without_overlay", "delete_both", "deleted"):
                    it.choice = choice
            else:
                if choice in ("keep", "delete", "deleted"):
                    it.choice = choice

    def set_choices_bulk(self, indices: list[int], choice: str) -> None:
        for idx in indices:
            self.set_choice(idx, choice)

    def set_group_choice(self, group: str, choice: str, source: str = "all") -> int:
        count = 0
        for item in self.items:
            if item.choice == "deleted":
                continue
            if source != "all" and item.source != source:
                continue
            if group == "all" or item.media_type == group:
                self.set_choice(item.index, choice)
                count += 1
        return count

    def delete_group(self, group: str, source: str = "all") -> dict[str, Any]:
        deleted_files: list[str] = []
        errors: list[dict[str, str]] = []
        for item in self.items:
            if item.choice == "deleted":
                continue
            if source != "all" and item.source != source:
                continue
            if group == "all" or item.media_type == group:
                item.choice = "deleted"
                if item.item_type == "pair":
                    for rel in (item.main_rel_path, item.composite_rel_path):
                        p = self.output_root / rel
                        if p.is_file():
                            try:
                                p.unlink()
                                deleted_files.append(rel)
                            except OSError as exc:
                                errors.append({"file": rel, "error": str(exc)})
                else:
                    p = self.output_root / item.rel_path
                    if p.is_file():
                        try:
                            p.unlink()
                            deleted_files.append(item.rel_path)
                        except OSError as exc:
                            errors.append({"file": item.rel_path, "error": str(exc)})
        return {
            "group": group,
            "source": source,
            "deleted": len(deleted_files),
            "deleted_files": deleted_files,
            "errors": errors,
        }

    def apply_deletions(self) -> dict[str, Any]:
        deleted_files: list[str] = []
        errors: list[dict[str, str]] = []
        kept = deleted = 0
        pairs_kept_both = pairs_kept_with = pairs_kept_without = pairs_deleted_both = 0

        for item in self.items:
            if item.item_type == "pair":
                m_abs = self.output_root / item.main_rel_path
                c_abs = self.output_root / item.composite_rel_path
                if item.choice == "with_overlay":
                    pairs_kept_with += 1
                    kept += 1
                    deleted += 1
                    if m_abs.is_file():
                        try:
                            m_abs.unlink()
                            deleted_files.append(item.main_rel_path)
                        except OSError as exc:
                            errors.append({"file": item.main_rel_path, "error": str(exc)})
                elif item.choice == "without_overlay":
                    pairs_kept_without += 1
                    kept += 1
                    deleted += 1
                    if c_abs.is_file():
                        try:
                            c_abs.unlink()
                            deleted_files.append(item.composite_rel_path)
                        except OSError as exc:
                            errors.append({"file": item.composite_rel_path, "error": str(exc)})
                elif item.choice in ("delete_both", "deleted"):
                    pairs_deleted_both += 1
                    deleted += 2
                    for p, rel in ((m_abs, item.main_rel_path), (c_abs, item.composite_rel_path)):
                        if p.is_file():
                            try:
                                p.unlink()
                                deleted_files.append(rel)
                            except OSError as exc:
                                errors.append({"file": rel, "error": str(exc)})
                else:  # "both" (neither deleted)
                    pairs_kept_both += 1
                    kept += 2
            else:
                p_abs = self.output_root / item.rel_path
                if item.choice in ("delete", "deleted"):
                    deleted += 1
                    if p_abs.is_file():
                        try:
                            p_abs.unlink()
                            deleted_files.append(item.rel_path)
                        except OSError as exc:
                            errors.append({"file": item.rel_path, "error": str(exc)})
                else:
                    kept += 1

        pairs_total = sum(1 for it in self.items if it.item_type == "pair")
        return {
            "total_items": len(self.items),
            "kept": kept,
            "deleted": deleted,
            "deleted_files": deleted_files,
            "pairs_total": pairs_total,
            "pairs_kept_both": pairs_kept_both,
            "pairs_kept_with_overlay": pairs_kept_with,
            "pairs_kept_without_overlay": pairs_kept_without,
            "pairs_deleted_both": pairs_deleted_both,
            "errors": errors,
        }


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


def discover_all_review_items(
    output_root: pathlib.Path,
    pairs_hint: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    if not output_root.is_dir():
        return []

    pairs = pairs_hint if pairs_hint is not None else discover_overlay_pairs(output_root)
    paired_paths: set[str] = set()
    items: list[dict[str, Any]] = []

    for p in pairs:
        main_rel = p["main_rel_path"]
        comp_rel = p["composite_rel_path"]
        paired_paths.add(main_rel)
        paired_paths.add(comp_rel)
        m_abs = output_root / main_rel
        c_abs = output_root / comp_rel
        m_size = m_abs.stat().st_size if m_abs.is_file() else 0
        c_size = c_abs.stat().st_size if c_abs.is_file() else 0
        items.append({
            "item_type": "pair",
            "source": "memories",
            "media_type": "pairs",
            "rel_path": main_rel,
            "filename": pathlib.PurePosixPath(main_rel).name,
            "is_video": False,
            "main_rel_path": main_rel,
            "composite_rel_path": comp_rel,
            "main_size": m_size,
            "composite_size": c_size,
            "size_bytes": m_size + c_size,
            "choice": p.get("choice", "both"),
        })

    for root, _, files in os.walk(output_root):
        root_p = pathlib.Path(root)
        if "debug" in root_p.parts:
            continue
        for fname in sorted(files):
            p = root_p / fname
            ext = p.suffix.lower()
            if ext not in ALL_MEDIA_EXTENSIONS:
                continue
            rel = p.relative_to(output_root).as_posix()
            if rel in paired_paths:
                continue

            if rel.startswith("chat_media/") or "chat_media" in root_p.parts:
                src = "chat_media"
            elif rel.startswith("memories/") or "memories" in root_p.parts:
                src = "memories"
            else:
                src = "other"

            is_vid = ext in SUPPORTED_VIDEO_EXTENSIONS
            mtype = classify_chat_media_type(rel, fname, is_vid)
            try:
                size = p.stat().st_size
            except OSError:
                size = 0

            items.append({
                "item_type": "single",
                "source": src,
                "media_type": mtype,
                "rel_path": rel,
                "filename": fname,
                "is_video": is_vid,
                "size_bytes": size,
                "choice": "keep",
            })

    items.sort(key=lambda x: x["rel_path"])
    return items


def _render_html(
    unified_items: list[dict[str, Any]],
    initial_source: str = "all",
    initial_category: str = "all",
) -> str:
    items_json = json.dumps(unified_items)
    pairs_list = [it for it in unified_items if it.get("item_type") == "pair"]
    chat_list = [it for it in unified_items if it.get("source") == "chat_media"]

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
    --card-hover: #323238;
    --text: #fafafa;
    --border: #3f3f46;
    --accent: #2563eb;
    --del: #dc2626;
    --keep: #16a34a;
    --amber: #d97706;
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
    padding-bottom: 12px;
    margin-bottom: 14px;
    flex-wrap: wrap;
    gap: 10px;
  }}
  .header-title {{
    font-size: 1.25rem;
    font-weight: 700;
    display: flex;
    align-items: center;
    gap: 8px;
  }}

  /* Filter Panels */
  .filter-panel {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
    margin-bottom: 14px;
  }}
  .source-tabs {{
    display: flex;
    gap: 8px;
    margin-bottom: 10px;
    flex-wrap: wrap;
  }}
  .source-tab-btn {{
    background: #18181b;
    border: 1px solid var(--border);
    color: #a1a1aa;
    padding: 6px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-weight: 600;
    font-size: 0.85rem;
    transition: all 0.15s ease;
  }}
  .source-tab-btn:hover {{ background: #27272a; color: #fff; }}
  .source-tab-btn.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}

  .type-pills-container {{
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    align-items: center;
  }}
  .type-pill {{
    background: #18181b;
    border: 2px solid var(--border);
    border-radius: 6px;
    padding: 6px 10px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 8px;
    transition: all 0.15s ease;
    user-select: none;
  }}
  .type-pill:hover {{ border-color: #60a5fa; background: #27272a; }}
  .type-pill.active {{ border-color: #3b82f6; background: #1e3a8a; }}
  .type-pill-info {{ display: flex; flex-direction: column; }}
  .type-pill-name {{ font-weight: 700; font-size: 0.82rem; color: #fff; }}
  .type-pill-count {{ font-size: 0.7rem; color: #a1a1aa; }}
  .btn-pill-del {{
    background: #991b1b;
    color: #fff;
    border: none;
    padding: 3px 6px;
    border-radius: 4px;
    font-size: 0.7rem;
    font-weight: 700;
    cursor: pointer;
  }}
  .btn-pill-del:hover {{ background: #dc2626; }}

  /* Toolbar */
  .toolbar {{
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 14px;
    align-items: center;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 12px;
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
  }}
  .btn-primary:hover {{ background: #15803d; }}

  /* Grid Layout */
  .grid-container {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 12px;
    margin-bottom: 16px;
    min-height: 250px;
  }}
  .media-card {{
    background: #09090b;
    border: 2px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    position: relative;
    display: flex;
    flex-direction: column;
    user-select: none;
    transition: transform 0.12s ease, border-color 0.12s ease;
  }}
  .media-card:hover {{ transform: translateY(-2px); border-color: #60a5fa; }}
  .media-card.is-keep {{ border-color: #22c55e; }}
  .media-card.is-delete {{ border-color: #ef4444; background: #2a1215; }}
  .media-card.is-selected-bulk {{ box-shadow: 0 0 0 2px #38bdf8; }}

  /* Checkbox & Top Tags */
  .card-top-bar {{
    position: absolute;
    top: 6px;
    left: 6px;
    right: 6px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    z-index: 5;
    pointer-events: none;
  }}
  .card-checkbox {{
    pointer-events: auto;
    width: 18px;
    height: 18px;
    cursor: pointer;
    accent-color: #0284c7;
  }}
  .badge-status {{
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.65rem;
    font-weight: 800;
    text-transform: uppercase;
    background: rgba(0,0,0,0.85);
    color: #fff;
  }}
  .status-keep {{ background: #15803d; color: #f0fdf4; }}
  .status-delete {{ background: #991b1b; color: #fef2f2; }}
  .status-both {{ background: #1d4ed8; color: #eff6ff; }}
  .status-ov {{ background: #d97706; color: #fffbeb; }}

  /* Previews */
  .thumb-wrap {{
    position: relative;
    width: 100%;
    aspect-ratio: 1 / 1;
    background: #000;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    cursor: pointer;
  }}
  .thumb-wrap img, .thumb-wrap video {{
    width: 100%;
    height: 100%;
    object-fit: cover;
    pointer-events: none;
  }}

  /* Pair Dual Thumbnails */
  .pair-dual-wrap {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 2px;
    width: 100%;
    aspect-ratio: 1 / 1;
    background: #000;
  }}
  .pair-half {{
    position: relative;
    overflow: hidden;
    cursor: pointer;
  }}
  .pair-half img {{
    width: 100%;
    height: 100%;
    object-fit: cover;
  }}
  .pair-half-label {{
    position: absolute;
    bottom: 4px;
    left: 4px;
    font-size: 0.62rem;
    font-weight: 700;
    background: rgba(0,0,0,0.8);
    padding: 2px 4px;
    border-radius: 3px;
    color: #fff;
  }}
  .pair-half.is-deleted::after {{
    content: "✕ DEL";
    position: absolute;
    inset: 0;
    background: rgba(220, 38, 38, 0.65);
    display: flex;
    align-items: center;
    justify-content: center;
    color: #fff;
    font-weight: 800;
    font-size: 0.9rem;
  }}

  /* Card Footer */
  .card-footer {{
    padding: 6px 8px;
    font-size: 0.72rem;
    background: #18181b;
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 4px;
    color: #a1a1aa;
  }}
  .card-footer-meta {{
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
  .card-name {{
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 160px;
    font-family: monospace;
    color: #cbd5e1;
  }}
  .pair-btn-bar {{
    display: flex;
    gap: 4px;
    margin-top: 2px;
  }}
  .pair-btn-bar button {{
    flex: 1;
    padding: 3px 0;
    font-size: 0.68rem;
    border-radius: 3px;
  }}

  /* Single / Comparison View */
  .single-container {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 16px;
  }}
  .single-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
    flex-wrap: wrap;
    gap: 8px;
  }}
  .grid-pair-compare {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-bottom: 14px;
  }}
  @media (max-width: 768px) {{ .grid-pair-compare {{ grid-template-columns: 1fr; }} }}
  .img-box {{
    background: #09090b;
    border: 2px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    cursor: pointer;
    transition: all 0.15s ease;
  }}
  .img-box:hover {{ border-color: #60a5fa; }}
  .img-box.selected {{ border-color: #22c55e; box-shadow: 0 0 0 2px rgba(34,197,94,0.4); }}
  .img-box.unselected {{ border-color: #ef4444; opacity: 0.65; }}
  .img-tag {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 10px;
    background: #18181b;
    border-bottom: 1px solid var(--border);
    font-size: 0.8rem;
  }}
  .img-wrap {{
    height: 480px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #000;
  }}
  .img-wrap img, .img-wrap video {{
    max-width: 100%;
    max-height: 480px;
    object-fit: contain;
  }}
  .choices-bar {{
    display: flex;
    gap: 8px;
    justify-content: center;
    margin-bottom: 14px;
    flex-wrap: wrap;
  }}
  .choices-bar button {{
    padding: 8px 16px;
    font-size: 0.85rem;
    font-weight: 600;
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
    max-width: 460px;
  }}
</style>
</head>
<body>
  <header>
    <div class="header-title">
      <span>📸 Snapchat Export Review & Filter</span>
      <span style="font-size: 0.8rem; font-weight: normal; color: #a1a1aa;">(Memories Overlay Pairs & Chat Media Filter)</span>
    </div>
    <div style="font-size: 0.85rem; color: #a1a1aa;" id="top-stats-badge">Loading...</div>
  </header>

  <!-- Filter & Source Navigation -->
  <div class="filter-panel">
    <div class="source-tabs">
      <span style="font-weight: 700; font-size: 0.85rem; align-self: center; margin-right: 4px;">Source:</span>
      <button class="source-tab-btn active" id="tab-src-all" onclick="setSource('all')">📁 All Sources</button>
      <button class="source-tab-btn" id="tab-src-memories" onclick="setSource('memories')">💾 Memories</button>
      <button class="source-tab-btn" id="tab-src-chat_media" onclick="setSource('chat_media')">💬 Chat Media</button>
    </div>
    <div class="type-nav-header" style="margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center;">
      <span style="font-weight: 700; font-size: 0.85rem;">Categories / Media Types:</span>
      <span id="group-action-container"></span>
    </div>
    <div class="type-pills-container" id="type-pills-container"></div>
  </div>

  <!-- Main Toolbar -->
  <div class="toolbar" style="justify-content: space-between;">
    <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
      <span style="font-weight: 600; font-size: 0.85rem; color: #93c5fd;">View:</span>
      <button id="btn-view-grid" class="active" onclick="setViewMode('grid')">⊞ Grid</button>
      <button id="btn-view-single" onclick="setViewMode('single')">🗖 Single / Compare (V)</button>
      <span style="color: var(--border);">|</span>
      <span style="font-size: 0.85rem; color: #a1a1aa;">Status:</span>
      <select id="status-filter" onchange="changeStatusFilter(this.value)">
        <option value="all">All Statuses</option>
        <option value="keep">Marked Keep</option>
        <option value="delete">Marked for Deletion</option>
      </select>
      <span style="color: var(--border);">|</span>
      <!-- Bulk Selection Actions -->
      <button onclick="toggleSelectPage()">Toggle Select Page</button>
      <span id="selection-pill" style="display: none; background: #0369a1; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 700;"></span>
      <button id="btn-mark-sel-del" style="display: none; background: #991b1b;" onclick="markSelected('delete')">Mark Selected Delete</button>
      <button id="btn-mark-sel-keep" style="display: none; background: #15803d;" onclick="markSelected('keep')">Mark Selected Keep</button>
      <button onclick="batchMarkVisible('delete')">Mark Page to Delete</button>
      <button onclick="batchMarkVisible('keep')">Mark Page to Keep</button>
      <button onclick="batchInvertVisible()">Invert Page</button>
    </div>
    <div style="display: flex; gap: 8px; align-items: center;">
      <button id="btn-prev-page" onclick="changePage(-1)">◀ Prev ([)</button>
      <span id="page-indicator" style="font-size: 0.85rem; font-family: monospace;">Page 1</span>
      <button id="btn-next-page" onclick="changePage(1)">Next (]) ▶</button>
    </div>
  </div>

  <!-- VIEW 1: GRID MODE -->
  <div id="grid-view-panel">
    <div class="grid-container" id="media-grid"></div>
  </div>

  <!-- VIEW 2: SINGLE / COMPARE MODE -->
  <div id="single-view-panel" style="display: none;">
    <div class="single-container">
      <div class="single-header">
        <strong>Item <span id="s-cur">1</span> of <span id="s-total">0</span>: <span id="s-filename" style="font-family: monospace; color: #93c5fd;"></span> (<span id="s-size"></span>) <span id="s-tag" style="color: #a1a1aa; font-size: 0.8rem; margin-left: 6px;"></span></strong>
        <span id="s-badge" class="badge-status"></span>
      </div>

      <!-- Single File Box -->
      <div id="single-box-regular" style="display: none;">
        <div class="img-wrap" id="single-regular-wrap" style="height: 520px; border: 2px solid var(--border); border-radius: 8px; cursor: pointer;" onclick="toggleSingleRegularChoiceAndAdvance()"></div>
        <div class="choices-bar" style="margin-top: 14px;">
          <button id="btn-s-keep" style="background: #15803d; color: #fff;" onclick="setSingleChoice('keep')">✓ Keep Media (K / Enter / Space / →)</button>
          <button id="btn-s-del" style="background: #991b1b; color: #fff;" onclick="setSingleChoice('delete')">✕ Mark to Delete (D / ↓)</button>
        </div>
      </div>

      <!-- Overlay Pair Compare Box -->
      <div id="single-box-pair" style="display: none;">
        <div class="grid-pair-compare">
          <div class="img-box" id="box-orig" onclick="setChoice('without_overlay')" title="Click image to prefer Without Overlay">
            <div class="img-tag">
              <span style="color: #60a5fa; font-weight: 600;">Without Overlay (Original Main)</span>
              <span id="badge-orig" class="badge-status"></span>
            </div>
            <div class="img-wrap"><img id="img-orig" alt="Original"></div>
          </div>
          <div class="img-box" id="box-comp" onclick="setChoice('with_overlay')" title="Click image to prefer With Overlay">
            <div class="img-tag">
              <span style="color: #4ade80; font-weight: 600;">With Overlay (Composite)</span>
              <span id="badge-comp" class="badge-status"></span>
            </div>
            <div class="img-wrap"><img id="img-comp" alt="Composite"></div>
          </div>
        </div>
        <div class="choices-bar">
          <button id="btn-both" onclick="setChoice('both')">Keep Both (1)</button>
          <button id="btn-without" onclick="setChoice('without_overlay')">Keep Without Overlay (2)</button>
          <button id="btn-with" onclick="setChoice('with_overlay')">Keep With Overlay (3)</button>
          <button id="btn-del-both" style="background: #991b1b;" onclick="setChoice('delete_both')">Delete Both (4 / D)</button>
        </div>
        <div id="status-text" style="text-align: center; margin-bottom: 12px; font-size: 0.9rem;"></div>
      </div>

      <div style="display: flex; justify-content: space-between; align-items: center; border-top: 1px solid var(--border); padding-top: 12px;">
        <div>
          <button onclick="moveSingle(-1)">◀ Prev (P / ←)</button>
          <button onclick="moveSingle(1)">Next (N / →) ▶</button>
        </div>
        <button onclick="setViewMode('grid')">Back to Grid (V / Esc)</button>
      </div>
    </div>
  </div>

  <!-- Bottom Summary & Action Bar -->
  <div style="background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; display: flex; justify-content: space-between; align-items: center; margin-top: 14px; flex-wrap: wrap; gap: 12px;">
    <div style="font-size: 0.9rem;" id="bottom-summary-text">
      Loading summary...
    </div>
    <button class="btn-primary" style="background: #dc2626; padding: 8px 18px;" onclick="applyAllDeletions()">Apply All Deletions</button>
  </div>

  <!-- Completion Modal -->
  <div class="modal" id="done-modal">
    <div class="modal-inner">
      <h3 style="color: #4ade80; margin-bottom: 10px;">Review Applied</h3>
      <p id="done-msg" style="margin-bottom: 16px; font-size: 0.9rem;"></p>
      <p style="color: #a1a1aa; font-size: 0.8rem;">You may now close this browser tab.</p>
    </div>
  </div>

<script>
  const allItems = {items_json};
  let activeSource = "{initial_source}";
  let activeCategory = "{initial_category}";
  let statusFilter = "all";
  let viewMode = "grid";
  let curPage = 0;
  const pageSize = 36;
  let curSingleIndex = 0;
  let advanceTimer = null;
  const selectedIndices = new Set();

  const TYPE_DEFS = {{
    all: {{ label: "All Media", icon: "📁" }},
    pairs: {{ label: "Overlay Pairs", icon: "🖼️" }},
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

  function getFilteredItems() {{
    return allItems.filter(it => {{
      if (it.choice === "deleted") return false;
      if (activeSource !== "all" && it.source !== activeSource) return false;
      if (activeCategory !== "all" && it.media_type !== activeCategory) return false;
      if (statusFilter === "keep") {{
        return it.item_type === "pair" ? it.choice === "both" : it.choice === "keep";
      }}
      if (statusFilter === "delete") {{
        return it.item_type === "pair" ? it.choice !== "both" : it.choice === "delete";
      }}
      return true;
    }});
  }}

  function setSource(src) {{
    activeSource = src;
    curPage = 0;
    document.querySelectorAll(".source-tab-btn").forEach(b => b.classList.remove("active"));
    const btn = document.getElementById("tab-src-" + src);
    if (btn) btn.classList.add("active");
    renderView();
  }}

  function selectCategory(cat) {{
    activeCategory = cat;
    curPage = 0;
    renderView();
  }}

  function setViewMode(mode) {{
    viewMode = mode;
    document.getElementById("btn-view-grid").classList.toggle("active", mode === "grid");
    document.getElementById("btn-view-single").classList.toggle("active", mode === "single");
    document.getElementById("grid-view-panel").style.display = mode === "grid" ? "block" : "none";
    document.getElementById("single-view-panel").style.display = mode === "single" ? "block" : "none";
    renderView();
  }}

  function changeStatusFilter(val) {{
    statusFilter = val;
    curPage = 0;
    renderView();
  }}

  function changePage(d) {{
    const filtered = getFilteredItems();
    const maxPage = Math.max(0, Math.ceil(filtered.length / pageSize) - 1);
    curPage = Math.min(maxPage, Math.max(0, curPage + d));
    renderView();
  }}

  function renderTypePills() {{
    const container = document.getElementById("type-pills-container");
    container.innerHTML = "";

    const counts = {{ all: 0 }};
    const bytes = {{ all: 0 }};

    allItems.forEach(it => {{
      if (it.choice === "deleted") return;
      if (activeSource !== "all" && it.source !== activeSource) return;
      const t = it.media_type || "other";
      counts[t] = (counts[t] || 0) + 1;
      bytes[t] = (bytes[t] || 0) + (it.size_bytes || 0);
      counts.all += 1;
      bytes.all += (it.size_bytes || 0);
    }});

    const typesOrder = ["all", "pairs", "photos", "videos", "thumbnails", "overlays", "stickers", "other"];
    const typesToShow = typesOrder.filter(t => t === "all" || (counts[t] || 0) > 0);

    typesToShow.forEach(t => {{
      const def = TYPE_DEFS[t] || {{ label: t, icon: "📁" }};
      const pill = document.createElement("div");
      pill.className = `type-pill ${{activeCategory === t ? "active" : ""}}`;
      pill.onclick = () => selectCategory(t);

      const info = document.createElement("div");
      info.className = "type-pill-info";
      info.innerHTML = `<span class="type-pill-name">${{def.icon}} ${{def.label}}</span><span class="type-pill-count">${{counts[t] || 0}} files (${{formatBytes(bytes[t] || 0)}})</span>`;
      pill.appendChild(info);

      if (t !== "all" && ["thumbnails", "overlays", "stickers"].includes(t) && (counts[t] || 0) > 0) {{
        const delBtn = document.createElement("button");
        delBtn.className = "btn-pill-del";
        delBtn.title = `Delete all ${{counts[t]}} ${{def.label}}`;
        delBtn.textContent = "✕ Del All";
        delBtn.onclick = (e) => {{
          e.stopPropagation();
          deleteGroupNow(t);
        }};
        pill.appendChild(delBtn);
      }}

      container.appendChild(pill);
    }});

    const actionBar = document.getElementById("group-action-container");
    if (activeCategory !== "all") {{
      const def = TYPE_DEFS[activeCategory] || {{ label: activeCategory }};
      const c = counts[activeCategory] || 0;
      const b = bytes[activeCategory] || 0;
      actionBar.innerHTML = `
        <span style="font-size: 0.82rem; margin-right: 8px; color: #93c5fd;">Active Category: <strong>${{def.label}}</strong> (${{c}} items)</span>
        <button style="padding: 3px 8px; font-size: 0.72rem;" onclick="markCategoryChoice('${{activeCategory}}', 'delete')">Mark Category Delete</button>
        <button style="padding: 3px 8px; font-size: 0.72rem;" onclick="markCategoryChoice('${{activeCategory}}', 'keep')">Mark Category Keep</button>
        <button class="btn-primary" style="padding: 3px 10px; font-size: 0.72rem; background: #dc2626;" onclick="deleteGroupNow('${{activeCategory}}')">🗑️ Delete All ${{def.label}} Now (${{formatBytes(b)}})</button>
      `;
    }} else {{
      actionBar.innerHTML = '<span style="font-size: 0.8rem; color: #a1a1aa;">Viewing all categories</span>';
    }}
  }}

  function markCategoryChoice(cat, choice) {{
    allItems.forEach(it => {{
      if (it.choice !== "deleted" && (activeSource === "all" || it.source === activeSource) && it.media_type === cat) {{
        if (it.item_type === "pair") {{
          it.choice = choice === "delete" ? "delete_both" : "both";
        }} else {{
          it.choice = choice;
        }}
      }}
    }});
    renderView();
  }}

  function deleteGroupNow(group) {{
    const def = TYPE_DEFS[group] || {{ label: group }};
    const targetItems = allItems.filter(it => it.choice !== "deleted" && (activeSource === "all" || it.source === activeSource) && (group === "all" || it.media_type === group));
    const totalBytes = targetItems.reduce((acc, it) => acc + (it.size_bytes || 0), 0);
    if (!targetItems.length) return;

    if (!confirm(`Permanently delete all ${{targetItems.length}} ${{def.label}} items (${{formatBytes(totalBytes)}})? This cannot be undone.`)) return;

    fetch("/api/delete_group", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{group: group, source: activeSource}})
    }})
    .then(r => r.json())
    .then(data => {{
      const res = data.result || {{}};
      targetItems.forEach(it => {{ it.choice = "deleted"; }});
      renderView();
      alert(`Deleted ${{res.deleted || 0}} file(s) (${{formatBytes(totalBytes)}} freed).`);
    }});
  }}

  function renderStats() {{
    const nonDeleted = allItems.filter(it => it.choice !== "deleted");
    let delFiles = 0;
    let delBytes = 0;
    let keepFiles = 0;
    let pairBoth = 0, pairWith = 0, pairWithout = 0, pairDelBoth = 0;

    nonDeleted.forEach(it => {{
      if (it.item_type === "pair") {{
        if (it.choice === "both") {{
          pairBoth += 1;
          keepFiles += 2;
        }} else if (it.choice === "with_overlay") {{
          pairWith += 1;
          keepFiles += 1;
          delFiles += 1;
          delBytes += it.main_size;
        }} else if (it.choice === "without_overlay") {{
          pairWithout += 1;
          keepFiles += 1;
          delFiles += 1;
          delBytes += it.composite_size;
        }} else if (it.choice === "delete_both") {{
          pairDelBoth += 1;
          delFiles += 2;
          delBytes += it.size_bytes;
        }}
      }} else {{
        if (it.choice === "delete") {{
          delFiles += 1;
          delBytes += it.size_bytes;
        }} else {{
          keepFiles += 1;
        }}
      }}
    }});

    const statText = `Total Items: ${{nonDeleted.length}} | Keep: ${{keepFiles}} files | Delete: <strong style="color: #f87171;">${{delFiles}} files (${{formatBytes(delBytes)}})</strong> | Pairs: ${{pairBoth}} both, ${{pairWith}} overlay, ${{pairWithout}} original, ${{pairDelBoth}} both del`;
    document.getElementById("top-stats-badge").innerHTML = `<strong style="color: #f87171;">${{delFiles}} files to delete</strong> (${{formatBytes(delBytes)}} freed)`;
    document.getElementById("bottom-summary-text").innerHTML = statText;
    renderTypePills();
  }}

  function renderView() {{
    renderStats();
    if (viewMode === "grid") renderGrid();
    else renderSingle();
  }}

  function renderGrid() {{
    const filtered = getFilteredItems();
    const container = document.getElementById("media-grid");
    container.innerHTML = "";

    const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
    if (curPage >= totalPages) curPage = totalPages - 1;
    document.getElementById("page-indicator").textContent = `Page ${{curPage + 1}} of ${{totalPages}} (${{filtered.length}} items)`;
    document.getElementById("btn-prev-page").disabled = curPage === 0;
    document.getElementById("btn-next-page").disabled = curPage >= totalPages - 1;

    const start = curPage * pageSize;
    const pageItems = filtered.slice(start, start + pageSize);

    // Update selection pill and buttons
    const selCount = selectedIndices.size;
    const selPill = document.getElementById("selection-pill");
    const btnMarkDel = document.getElementById("btn-mark-sel-del");
    const btnMarkKeep = document.getElementById("btn-mark-sel-keep");
    if (selCount > 0) {{
      selPill.style.display = "inline-block";
      selPill.textContent = `${{selCount}} Selected`;
      btnMarkDel.style.display = "inline-block";
      btnMarkKeep.style.display = "inline-block";
    }} else {{
      selPill.style.display = "none";
      btnMarkDel.style.display = "none";
      btnMarkKeep.style.display = "none";
    }}

    pageItems.forEach((it, idxInPage) => {{
      const globalFilteredIdx = start + idxInPage;
      const card = document.createElement("div");
      const isPair = it.item_type === "pair";
      const isDel = isPair ? it.choice === "delete_both" : it.choice === "delete";
      const isSel = selectedIndices.has(it.index);

      card.className = `media-card ${{isDel ? "is-delete" : "is-keep"}} ${{isSel ? "is-selected-bulk" : ""}}`;

      // Top bar with checkbox & status badge
      const topBar = document.createElement("div");
      topBar.className = "card-top-bar";

      const badge = document.createElement("span");
      badge.className = "badge-status";
      if (isPair) {{
        if (it.choice === "both") {{ badge.className += " status-both"; badge.textContent = "✓ KEEP BOTH"; }}
        else if (it.choice === "with_overlay") {{ badge.className += " status-ov"; badge.textContent = "KEEP OVERLAY (DEL ORIG)"; }}
        else if (it.choice === "without_overlay") {{ badge.className += " status-ov"; badge.textContent = "KEEP ORIG (DEL OVERLAY)"; }}
        else if (it.choice === "delete_both") {{ badge.className += " status-delete"; badge.textContent = "✕ DELETE BOTH"; }}
      }} else {{
        badge.className += it.choice === "delete" ? " status-delete" : " status-keep";
        badge.textContent = it.choice === "delete" ? "✕ DELETE" : "✓ KEEP";
      }}
      topBar.appendChild(badge);

      const chk = document.createElement("input");
      chk.type = "checkbox";
      chk.className = "card-checkbox";
      chk.checked = isSel;
      chk.onclick = (e) => {{
        e.stopPropagation();
        if (chk.checked) selectedIndices.add(it.index);
        else selectedIndices.delete(it.index);
        renderGrid();
      }};
      topBar.appendChild(chk);
      card.appendChild(topBar);

      // Previews
      if (isPair) {{
        const dual = document.createElement("div");
        dual.className = "pair-dual-wrap";

        const leftHalf = document.createElement("div");
        leftHalf.className = `pair-half ${{it.choice === "with_overlay" || it.choice === "delete_both" ? "is-deleted" : ""}}`;
        leftHalf.onclick = () => setItemChoice(it, "without_overlay");
        leftHalf.title = "Click to prefer Without Overlay (Original)";
        leftHalf.innerHTML = `<img src="/image?path=${{encodeURIComponent(it.main_rel)}}" loading="lazy"><span class="pair-half-label">Original</span>`;

        const rightHalf = document.createElement("div");
        rightHalf.className = `pair-half ${{it.choice === "without_overlay" || it.choice === "delete_both" ? "is-deleted" : ""}}`;
        rightHalf.onclick = () => setItemChoice(it, "with_overlay");
        rightHalf.title = "Click to prefer With Overlay (Composite)";
        rightHalf.innerHTML = `<img src="/image?path=${{encodeURIComponent(it.comp_rel)}}" loading="lazy"><span class="pair-half-label">Composite</span>`;

        dual.appendChild(leftHalf);
        dual.appendChild(rightHalf);
        card.appendChild(dual);
      }} else {{
        const thumbWrap = document.createElement("div");
        thumbWrap.className = "thumb-wrap";
        thumbWrap.onclick = () => setItemChoice(it, it.choice === "keep" ? "delete" : "keep");

        if (it.is_video) {{
          const vid = document.createElement("video");
          vid.src = `/image?path=${{encodeURIComponent(it.rel_path)}}`;
          vid.muted = true;
          vid.loop = true;
          vid.preload = "metadata";
          vid.onmouseenter = () => vid.play().catch(() => {{}});
          vid.onmouseleave = () => vid.pause();
          thumbWrap.appendChild(vid);
        }} else {{
          const img = document.createElement("img");
          img.src = `/image?path=${{encodeURIComponent(it.rel_path)}}`;
          img.loading = "lazy";
          thumbWrap.appendChild(img);
        }}
        card.appendChild(thumbWrap);
      }}

      // Footer
      const footer = document.createElement("div");
      footer.className = "card-footer";

      const meta = document.createElement("div");
      meta.className = "card-footer-meta";
      meta.innerHTML = `<span class="card-name" title="${{it.filename}}">${{it.filename}}</span><span>${{formatBytes(it.size_bytes)}}</span>`;
      footer.appendChild(meta);

      if (isPair) {{
        const btnBar = document.createElement("div");
        btnBar.className = "pair-btn-bar";
        btnBar.innerHTML = `
          <button style="${{it.choice === 'both' ? 'background: #1d4ed8;' : ''}}" onclick="setItemChoiceByIndex(${{it.index}}, 'both')">Both</button>
          <button style="${{it.choice === 'without_overlay' ? 'background: #d97706;' : ''}}" onclick="setItemChoiceByIndex(${{it.index}}, 'without_overlay')">Orig</button>
          <button style="${{it.choice === 'with_overlay' ? 'background: #d97706;' : ''}}" onclick="setItemChoiceByIndex(${{it.index}}, 'with_overlay')">Overlay</button>
          <button style="${{it.choice === 'delete_both' ? 'background: #dc2626;' : ''}}" onclick="setItemChoiceByIndex(${{it.index}}, 'delete_both')">Del Both</button>
        `;
        footer.appendChild(btnBar);
      }}

      const focusBtn = document.createElement("button");
      focusBtn.style.padding = "2px 6px";
      focusBtn.style.fontSize = "0.7rem";
      focusBtn.textContent = isPair ? "🗖 Side-by-Side Compare (V)" : "🗖 Focus (V)";
      focusBtn.onclick = () => {{
        curSingleIndex = globalFilteredIdx;
        setViewMode("single");
      }};
      footer.appendChild(focusBtn);

      card.appendChild(footer);
      container.appendChild(card);
    }});
  }}

  function setItemChoiceByIndex(index, choice) {{
    const it = allItems[index];
    if (it) setItemChoice(it, choice);
  }}

  function setItemChoice(it, choice) {{
    it.choice = choice;
    fetch("/api/set_choice", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{index: it.index, choice: choice}})
    }});
    renderView();
  }}

  function toggleSelectPage() {{
    const filtered = getFilteredItems();
    const start = curPage * pageSize;
    const pageItems = filtered.slice(start, start + pageSize);
    const allChecked = pageItems.every(it => selectedIndices.has(it.index));

    if (allChecked) {{
      pageItems.forEach(it => selectedIndices.delete(it.index));
    }} else {{
      pageItems.forEach(it => selectedIndices.add(it.index));
    }}
    renderGrid();
  }}

  function markSelected(choice) {{
    if (!selectedIndices.size) return;
    const indices = Array.from(selectedIndices);
    indices.forEach(idx => {{
      const it = allItems[idx];
      if (it) {{
        if (it.item_type === "pair") {{
          it.choice = choice === "delete" ? "delete_both" : "both";
        }} else {{
          it.choice = choice;
        }}
      }}
    }});
    fetch("/api/set_bulk", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{indices: indices, choice: choice}})
    }});
    selectedIndices.clear();
    renderView();
  }}

  function batchMarkVisible(choice) {{
    const filtered = getFilteredItems();
    const start = curPage * pageSize;
    const pageItems = filtered.slice(start, start + pageSize);
    if (!pageItems.length) return;

    pageItems.forEach(it => {{
      if (it.item_type === "pair") {{
        it.choice = choice === "delete" ? "delete_both" : "both";
      }} else {{
        it.choice = choice;
      }}
    }});
    const indices = pageItems.map(it => it.index);
    fetch("/api/set_bulk", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{indices: indices, choice: choice}})
    }});
    renderView();
  }}

  function batchInvertVisible() {{
    const filtered = getFilteredItems();
    const start = curPage * pageSize;
    const pageItems = filtered.slice(start, start + pageSize);
    if (!pageItems.length) return;

    pageItems.forEach(it => {{
      if (it.item_type === "pair") {{
        it.choice = it.choice === "both" ? "delete_both" : "both";
      }} else {{
        it.choice = it.choice === "keep" ? "delete" : "keep";
      }}
    }});
    renderView();
  }}

  // ------------------ SINGLE / COMPARE VIEW ------------------
  function renderSingle() {{
    const filtered = getFilteredItems();
    if (!filtered.length) {{
      setViewMode("grid");
      return;
    }}
    if (curSingleIndex < 0) curSingleIndex = 0;
    if (curSingleIndex >= filtered.length) curSingleIndex = filtered.length - 1;

    const it = filtered[curSingleIndex];
    document.getElementById("s-cur").textContent = curSingleIndex + 1;
    document.getElementById("s-total").textContent = filtered.length;
    document.getElementById("s-filename").textContent = it.filename;
    document.getElementById("s-size").textContent = formatBytes(it.size_bytes);
    document.getElementById("s-tag").textContent = `[${{it.source.toUpperCase()}} / ${{it.media_type.toUpperCase()}}]`;

    const isPair = it.item_type === "pair";
    document.getElementById("single-box-regular").style.display = isPair ? "none" : "block";
    document.getElementById("single-box-pair").style.display = isPair ? "block" : "none";

    const badge = document.getElementById("s-badge");

    if (isPair) {{
      document.getElementById("img-orig").src = `/image?path=${{encodeURIComponent(it.main_rel)}}`;
      document.getElementById("img-comp").src = `/image?path=${{encodeURIComponent(it.comp_rel)}}`;

      document.getElementById("btn-both").classList.toggle("active", it.choice === "both");
      document.getElementById("btn-without").classList.toggle("active", it.choice === "without_overlay");
      document.getElementById("btn-with").classList.toggle("active", it.choice === "with_overlay");
      document.getElementById("btn-del-both").classList.toggle("active", it.choice === "delete_both");

      const boxOrig = document.getElementById("box-orig");
      const boxComp = document.getElementById("box-comp");
      const badgeOrig = document.getElementById("badge-orig");
      const badgeComp = document.getElementById("badge-comp");
      const st = document.getElementById("status-text");

      boxOrig.classList.remove("selected", "unselected");
      boxComp.classList.remove("selected", "unselected");

      if (it.choice === "without_overlay") {{
        boxOrig.classList.add("selected");
        boxComp.classList.add("unselected");
        badgeOrig.className = "badge-status status-keep";
        badgeOrig.textContent = "✓ Preferred (Keeping)";
        badgeComp.className = "badge-status status-delete";
        badgeComp.textContent = "✕ Will Delete";
        st.innerHTML = '<span style="color: #60a5fa; font-weight: 600;">Preferred: Without Overlay</span> &bull; <strong style="color: #f87171;">Will delete composite</strong>';
        badge.className = "badge-status status-ov";
        badge.textContent = "KEEP WITHOUT OVERLAY";
      }} else if (it.choice === "with_overlay") {{
        boxOrig.classList.add("unselected");
        boxComp.classList.add("selected");
        badgeOrig.className = "badge-status status-delete";
        badgeOrig.textContent = "✕ Will Delete";
        badgeComp.className = "badge-status status-keep";
        badgeComp.textContent = "✓ Preferred (Keeping)";
        st.innerHTML = '<span style="color: #4ade80; font-weight: 600;">Preferred: With Overlay</span> &bull; <strong style="color: #f87171;">Will delete original</strong>';
        badge.className = "badge-status status-ov";
        badge.textContent = "KEEP WITH OVERLAY";
      }} else if (it.choice === "delete_both") {{
        boxOrig.classList.add("unselected");
        boxComp.classList.add("unselected");
        badgeOrig.className = "badge-status status-delete";
        badgeOrig.textContent = "✕ Will Delete";
        badgeComp.className = "badge-status status-delete";
        badgeComp.textContent = "✕ Will Delete";
        st.innerHTML = '<strong style="color: #f87171;">Marked to delete BOTH original and composite</strong>';
        badge.className = "badge-status status-delete";
        badge.textContent = "DELETE BOTH";
      }} else {{
        boxOrig.classList.add("selected");
        boxComp.classList.add("selected");
        badgeOrig.className = "badge-status status-keep";
        badgeOrig.textContent = "Keeping";
        badgeComp.className = "badge-status status-keep";
        badgeComp.textContent = "Keeping";
        st.innerHTML = '<span style="color: #60a5fa;">Keeping both files (neither deleted)</span>';
        badge.className = "badge-status status-both";
        badge.textContent = "KEEP BOTH";
      }}
    }} else {{
      badge.className = `badge-status ${{it.choice === "delete" ? "status-delete" : "status-keep"}}`;
      badge.textContent = it.choice === "delete" ? "✕ WILL DELETE" : "✓ KEEPING";

      const wrap = document.getElementById("single-regular-wrap");
      wrap.innerHTML = "";
      if (it.is_video) {{
        const vid = document.createElement("video");
        vid.src = `/image?path=${{encodeURIComponent(it.rel_path)}}`;
        vid.controls = true;
        vid.autoplay = true;
        vid.loop = true;
        vid.muted = true;
        wrap.appendChild(vid);
      }} else {{
        const img = document.createElement("img");
        img.src = `/image?path=${{encodeURIComponent(it.rel_path)}}`;
        img.alt = it.filename;
        wrap.appendChild(img);
      }}
    }}
  }}

  function setChoice(c, autoAdvance = true) {{
    if (advanceTimer) clearTimeout(advanceTimer);
    const filtered = getFilteredItems();
    const it = filtered[curSingleIndex];
    if (!it) return;
    setItemChoice(it, c);
    renderSingle();

    if (autoAdvance && curSingleIndex < filtered.length - 1) {{
      advanceTimer = setTimeout(() => moveSingle(1), 180);
    }}
  }}

  function setSingleChoice(c, autoAdvance = true) {{
    setChoice(c, autoAdvance);
  }}

  function toggleSingleRegularChoiceAndAdvance() {{
    const filtered = getFilteredItems();
    const it = filtered[curSingleIndex];
    if (!it) return;
    setChoice(it.choice === "keep" ? "delete" : "keep", true);
  }}

  function moveSingle(d) {{
    if (advanceTimer) clearTimeout(advanceTimer);
    const filtered = getFilteredItems();
    curSingleIndex = Math.min(filtered.length - 1, Math.max(0, curSingleIndex + d));
    renderSingle();
  }}

  function applyAllDeletions() {{
    const toDelSingle = allItems.filter(it => it.item_type !== "pair" && it.choice === "delete");
    const toDelPairs = allItems.filter(it => it.item_type === "pair" && it.choice !== "both");

    let totalFiles = toDelSingle.length;
    let totalBytes = toDelSingle.reduce((a, b) => a + (b.size_bytes || 0), 0);

    toDelPairs.forEach(p => {{
      if (p.choice === "with_overlay") {{ totalFiles += 1; totalBytes += p.main_size; }}
      else if (p.choice === "without_overlay") {{ totalFiles += 1; totalBytes += p.composite_size; }}
      else if (p.choice === "delete_both") {{ totalFiles += 2; totalBytes += p.size_bytes; }}
    }});

    if (totalFiles === 0) {{
      alert("No files are marked for deletion.");
      return;
    }}

    if (!confirm(`Permanently delete ${{totalFiles}} file(s) (${{formatBytes(totalBytes)}})? This action is irreversible.`)) return;

    const choicesPayload = Object.fromEntries(allItems.map(it => [it.index, it.choice]));
    fetch("/api/apply", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{choices: choicesPayload}})
    }})
    .then(r => r.json())
    .then(data => {{
      const res = data.result || {{}};
      document.getElementById("done-msg").textContent = `Review Applied: Permanently deleted ${{res.deleted || totalFiles}} file(s) (${{formatBytes(totalBytes)}} freed). Kept ${{res.kept || 0}} file(s).`;
      document.getElementById("done-modal").style.display = "flex";
    }});
  }}

  // ------------------ GLOBAL KEYBOARD SHORTCUTS ------------------
  window.addEventListener("keydown", (e) => {{
    if (viewMode === "single") {{
      const filtered = getFilteredItems();
      const it = filtered[curSingleIndex];
      const isPair = it && it.item_type === "pair";

      if (e.key === "ArrowLeft" || e.key === "p") moveSingle(-1);
      else if (e.key === "ArrowRight" || e.key === "n") moveSingle(1);
      else if (e.key === "v" || e.key === "Escape") setViewMode("grid");
      else if (isPair) {{
        if (e.key === "1") setChoice("both");
        else if (e.key === "2") setChoice("without_overlay");
        else if (e.key === "3") setChoice("with_overlay");
        else if (e.key === "4" || e.key === "d" || e.key === "ArrowDown") setChoice("delete_both");
      }} else {{
        if (e.key === "k" || e.key === "Enter" || e.key === " ") setChoice("keep");
        else if (e.key === "d" || e.key === "ArrowDown") setChoice("delete");
      }}
    }} else {{
      if (e.key === "[" || e.key === "PageUp") changePage(-1);
      else if (e.key === "]" || e.key === "PageDown") changePage(1);
      else if (e.key === "v") setViewMode("single");
    }}
  }});

  // Initial render
  renderView();
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
    if composites is not None or chat_media is not None:
        unified_items: list[dict[str, Any]] = []
        for idx, p in enumerate(composites or []):
            m_abs = output_root / p["main_rel_path"]
            c_abs = output_root / p["composite_rel_path"]
            m_size = m_abs.stat().st_size if m_abs.is_file() else 0
            c_size = c_abs.stat().st_size if c_abs.is_file() else 0
            unified_items.append({
                "item_type": "pair",
                "source": "memories",
                "media_type": "pairs",
                "rel_path": p["main_rel_path"],
                "filename": pathlib.PurePosixPath(p["main_rel_path"]).name,
                "is_video": False,
                "main_rel_path": p["main_rel_path"],
                "composite_rel_path": p["composite_rel_path"],
                "main_size": m_size,
                "composite_size": c_size,
                "size_bytes": m_size + c_size,
                "choice": p.get("choice", "both"),
            })
        for it in (chat_media or []):
            rel = it["rel_path"]
            fname = it.get("filename") or pathlib.PurePosixPath(rel).name
            is_vid = it.get("is_video", False)
            mtype = it.get("media_type") or classify_chat_media_type(rel, fname, is_vid)
            unified_items.append({
                "item_type": "single",
                "source": "chat_media",
                "media_type": mtype,
                "rel_path": rel,
                "filename": fname,
                "is_video": is_vid,
                "size_bytes": it.get("size_bytes", 0),
                "choice": it.get("choice", "keep"),
            })
    else:
        unified_items = discover_all_review_items(output_root)

    if mode == "overlays":
        unified_items = [it for it in unified_items if it.get("item_type") == "pair"]
    elif mode == "chat_media":
        unified_items = [it for it in unified_items if it.get("source") == "chat_media"]

    # Re-index
    for i, it in enumerate(unified_items):
        it["index"] = i

    manager = UnifiedReviewManager(output_root, unified_items)

    pair_items_compat = [
        {"main_rel_path": it["main_rel_path"], "composite_rel_path": it["composite_rel_path"], "choice": it["choice"]}
        for it in unified_items if it.get("item_type") == "pair"
    ]
    pair_manager = PairReviewManager(output_root, pair_items_compat)

    chat_items_compat = [
        {"rel_path": it["rel_path"], "filename": it["filename"], "is_video": it["is_video"], "size_bytes": it["size_bytes"], "media_type": it["media_type"], "choice": it["choice"]}
        for it in unified_items if it.get("source") == "chat_media"
    ]
    chat_manager = ChatMediaReviewManager(output_root, chat_items_compat)

    if not manager.items:
        return {
            "total_pairs": 0,
            "kept_both": 0,
            "kept_with_overlay": 0,
            "kept_without_overlay": 0,
            "deleted_both": 0,
            "deleted_files": [],
            "errors": [],
            "chat_media_total": 0,
            "chat_media_kept": 0,
            "chat_media_deleted": 0,
            "chat_deleted_files": [],
        }

    initial_src = "chat_media" if mode == "chat_media" else ("memories" if mode == "overlays" else "all")
    initial_cat = "pairs" if mode == "overlays" else "all"

    class RequestHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                content = _render_html(
                    [
                        {
                            "index": it.index,
                            "item_type": it.item_type,
                            "source": it.source,
                            "media_type": it.media_type,
                            "rel_path": it.rel_path,
                            "filename": it.filename,
                            "is_video": it.is_video,
                            "size_bytes": it.size_bytes,
                            "main_rel": it.main_rel_path,
                            "comp_rel": it.composite_rel_path,
                            "main_size": it.main_size,
                            "comp_size": it.composite_size,
                            "choice": it.choice,
                        }
                        for it in manager.items
                    ],
                    initial_source=initial_src,
                    initial_category=initial_cat,
                ).encode("utf-8")
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
            elif parsed.path == "/api/items":
                data = json.dumps([
                    {
                        "index": it.index,
                        "item_type": it.item_type,
                        "source": it.source,
                        "media_type": it.media_type,
                        "rel_path": it.rel_path,
                        "filename": it.filename,
                        "is_video": it.is_video,
                        "size_bytes": it.size_bytes,
                        "main_rel": it.main_rel_path,
                        "comp_rel": it.composite_rel_path,
                        "main_size": it.main_size,
                        "comp_size": it.composite_size,
                        "choice": it.choice,
                    }
                    for it in manager.items
                ]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif parsed.path == "/api/pairs":
                pair_items = [it for it in manager.items if it.item_type == "pair"]
                data = json.dumps([
                    {
                        "index": idx,
                        "main_rel": item.main_rel_path,
                        "comp_rel": item.composite_rel_path,
                        "choice": item.choice,
                    }
                    for idx, item in enumerate(pair_items)
                ]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif parsed.path == "/api/chat/items":
                chat_items = [it for it in manager.items if it.source == "chat_media"]
                data = json.dumps([
                    {
                        "index": idx,
                        "rel_path": item.rel_path,
                        "filename": item.filename,
                        "is_video": item.is_video,
                        "size_bytes": item.size_bytes,
                        "media_type": item.media_type,
                        "choice": item.choice,
                    }
                    for idx, item in enumerate(chat_items)
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
                    if 0 <= idx < len(manager.items):
                        manager.set_choice(idx, choice)
                    else:
                        pair_items = [it for it in manager.items if it.item_type == "pair"]
                        if 0 <= idx < len(pair_items):
                            pair_items[idx].choice = choice
                self._send_json({"ok": True, "index": idx, "choice": choice})
            elif parsed.path == "/api/set_bulk":
                indices = payload.get("indices", [])
                choice = payload.get("choice", "keep")
                if isinstance(indices, list) and isinstance(choice, str):
                    for i in indices:
                        if isinstance(i, int) and 0 <= i < len(manager.items):
                            it = manager.items[i]
                            if it.item_type == "pair":
                                it.choice = "delete_both" if choice == "delete" else "both"
                            else:
                                it.choice = choice
                self._send_json({"ok": True, "count": len(indices), "choice": choice})
            elif parsed.path == "/api/set_group_choice":
                group = payload.get("group", "all")
                choice = payload.get("choice", "keep")
                source = payload.get("source", "all")
                count = manager.set_group_choice(group, choice, source=source)
                self._send_json({"ok": True, "group": group, "choice": choice, "count": count})
            elif parsed.path == "/api/delete_group":
                group = payload.get("group", "all")
                source = payload.get("source", "all")
                result = manager.delete_group(group, source=source)
                self._send_json({"ok": True, "result": result})
            elif parsed.path == "/api/apply":
                choices = payload.get("choices")
                if isinstance(choices, dict):
                    pair_items = [it for it in manager.items if it.item_type == "pair"]
                    for k, v in choices.items():
                        try:
                            idx = int(k)
                            if 0 <= idx < len(manager.items):
                                manager.set_choice(idx, str(v))
                            elif 0 <= idx < len(pair_items):
                                pair_items[idx].choice = str(v)
                        except ValueError:
                            pass
                result = manager.apply_deletions()
                self.server.applied_result = result
                self._send_json({"ok": True, "result": result})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            # Compatibility endpoints:
            elif parsed.path == "/api/set_all":
                choice = payload.get("choice")
                if isinstance(choice, str):
                    for it in manager.items:
                        if it.item_type == "pair":
                            it.choice = choice
                self._send_json({"ok": True, "choice": choice})
            elif parsed.path == "/api/chat/set_choice":
                idx = payload.get("index")
                choice = payload.get("choice")
                chat_items = [it for it in manager.items if it.source == "chat_media"]
                if isinstance(idx, int) and isinstance(choice, str) and 0 <= idx < len(chat_items):
                    chat_items[idx].choice = choice
                self._send_json({"ok": True, "index": idx, "choice": choice})
            elif parsed.path == "/api/chat/set_bulk":
                indices = payload.get("indices", [])
                choice = payload.get("choice", "keep")
                chat_items = [it for it in manager.items if it.source == "chat_media"]
                if isinstance(indices, list) and isinstance(choice, str):
                    for i in indices:
                        if 0 <= i < len(chat_items):
                            chat_items[i].choice = choice
                self._send_json({"ok": True, "count": len(indices), "choice": choice})
            elif parsed.path == "/api/chat/set_group_choice":
                group = payload.get("group", "all")
                choice = payload.get("choice", "keep")
                count = manager.set_group_choice(group, choice, source="chat_media")
                self._send_json({"ok": True, "group": group, "choice": choice, "count": count})
            elif parsed.path == "/api/chat/delete_group":
                group = payload.get("group", "all")
                result = manager.delete_group(group, source="chat_media")
                self._send_json({"ok": True, "result": result})
            elif parsed.path == "/api/chat/set_all":
                choice = payload.get("choice", "keep")
                if isinstance(choice, str):
                    manager.set_group_choice("all", choice, source="chat_media")
                self._send_json({"ok": True, "choice": choice})
            elif parsed.path == "/api/chat/apply":
                choices = payload.get("choices")
                chat_items = [it for it in manager.items if it.source == "chat_media"]
                if isinstance(choices, dict):
                    for k, v in choices.items():
                        try:
                            idx = int(k)
                            if 0 <= idx < len(chat_items):
                                chat_items[idx].choice = str(v)
                        except ValueError:
                            pass
                result = manager.apply_deletions()
                self.server.applied_result = result
                self._send_json({"ok": True, "result": result})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif parsed.path == "/api/apply_all":
                result = manager.apply_deletions()
                self.server.applied_result = result
                self._send_json({"ok": True, "result": result})
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
    server.applied_result = None
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}"
    pair_count = sum(1 for it in manager.items if it.item_type == "pair")
    single_count = len(manager.items) - pair_count

    if logger:
        logger.info("Unified Review GUI available at %s (items=%d, pairs=%d)", url, len(manager.items), pair_count)
    print(f"\nUnified Review GUI running at: {url}")
    print(f"Loaded: {len(manager.items)} total items ({pair_count} overlay pairs, {single_count} standalone media).")
    print("Review media in your browser and click 'Apply All Deletions' to finish.\n")

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

    res = server.applied_result or manager.apply_deletions()
    res.setdefault("total_pairs", res.get("pairs_total", 0))
    res.setdefault("kept_both", res.get("pairs_kept_both", 0))
    res.setdefault("kept_with_overlay", res.get("pairs_kept_with_overlay", 0))
    res.setdefault("kept_without_overlay", res.get("pairs_kept_without_overlay", 0))
    res.setdefault("chat_media_total", sum(1 for it in manager.items if it.source == "chat_media"))
    res.setdefault("chat_media_kept", res.get("kept", 0))
    res.setdefault("chat_media_deleted", res.get("deleted", 0))
    res.setdefault("chat_deleted_files", res.get("deleted_files", []))
    return res


launch_review_gui = launch_overlay_review_gui


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Unified interactive GUI to review Snapchat Memories overlay pairs and filter/delete chat media."
    )
    parser.add_argument(
        "output_dir", type=pathlib.Path, nargs="?", default=pathlib.Path("snapchat-processed"),
        help="Path to the processed export output directory (default: snapchat-processed).",
    )
    parser.add_argument(
        "--output", "-o", dest="output_opt", type=pathlib.Path, default=None,
        help="Alternative flag to specify the processed export output directory.",
    )
    parser.add_argument(
        "--source", "-s", choices=["all", "memories", "chat_media"], default="all",
        help="Initial media source folder to display: 'all' (default), 'memories', or 'chat_media'.",
    )
    parser.add_argument(
        "--mode", "-m", default="auto",
        help="Initial review filter: 'auto' (default), 'overlays' (Memories pairs), 'chat_media', or 'both'.",
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
    target_dir = args.output_opt or args.output_dir or pathlib.Path("snapchat-processed")
    if not target_dir.is_dir():
        print(f"Error: directory '{target_dir}' does not exist.", file=sys.stderr)
        return 1

    pairs: list[dict[str, str]] | None = None
    if args.report and args.report.is_file():
        try:
            report_data = json.loads(args.report.read_text(encoding="utf-8"))
            pairs = report_data.get("composites", [])
        except Exception as exc:
            print(f"Warning: failed to read report '{args.report}': {exc}. Scanning directory instead.", file=sys.stderr)

    items = discover_all_review_items(target_dir, pairs_hint=pairs)
    if not items:
        print(f"No media items found in '{target_dir}'.", file=sys.stderr)
        return 0

    mode = args.mode
    if args.source != "all":
        mode = args.source

    pair_count = sum(1 for it in items if it.get("item_type") == "pair")
    print(f"Found {len(items)} media items (including {pair_count} overlay pairs) in '{target_dir}'.")
    result = launch_review_gui(
        target_dir,
        composites=None,
        chat_media=None,
        mode=mode,
        port=args.port,
        open_browser=not args.no_browser,
    )
    print("\nReview Complete:")
    if result.get("total_pairs", 0) > 0:
        print(f"  • Overlay Pairs: {result.get('total_pairs', 0)} total | {result.get('kept_both', 0)} kept both | {result.get('kept_with_overlay', 0)} with overlay | {result.get('kept_without_overlay', 0)} without overlay | {result.get('deleted_both', 0)} both deleted")
    print(f"  • Total Kept: {result.get('kept', 0)}")
    print(f"  • Total Deleted Files: {len(result.get('deleted_files', []))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
