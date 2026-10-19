#!/usr/bin/env python3
"""
Snapchat Export Processor (`process_snapchat_export.py`)

Non-destructive CLI tool that mirrors media assets (images and videos) from a Snapchat
data export into a separate output directory, applies strictly evidence-backed date/time
and location metadata updates via batch ExifTool, composites matching Memories main+overlay
image pairs via Pillow (retaining original mains and omitting raw overlays), and exports
`chat_history.json` into per-conversation Markdown files with configurable chunking and
verified attachment links.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import concurrent.futures
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import urllib.parse

from bs4 import BeautifulSoup
from PIL import Image, ImageOps


SUPPORTED_IMAGE_EXTENSIONS: set[str] = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif"}
SUPPORTED_VIDEO_EXTENSIONS: set[str] = {".mp4", ".mov", ".m4v"}
UNSUPPORTED_VIDEO_OR_MEDIA_EXTENSIONS: set[str] = {
    ".avi",
    ".mkv",
    ".webm",
    ".wmv",
    ".flv",
    ".3gp",
    ".gif",
    ".heic",
    ".heif",
    ".bmp",
}
ALL_MEDIA_EXTENSIONS: set[str] = (
    SUPPORTED_IMAGE_EXTENSIONS
    | SUPPORTED_VIDEO_EXTENSIONS
    | UNSUPPORTED_VIDEO_OR_MEDIA_EXTENSIONS
)

MEMORIES_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<uuid>[0-9a-fA-F-]+)-(?P<role>main|overlay)\.(?P<ext>[^.]+)$"
)
LEADING_DATE_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[_-](?P<rest>.+)\.(?P<ext>[^.]+)$"
)
UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
B_TOKEN_RE = re.compile(r"(b~[A-Za-z0-9_\-.~]+)")
LOCATION_RE = re.compile(
    r"Latitude,\s*Longitude:\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


@dataclass
class GalleryEntry:
    html_rel_path: str
    main_src: str | None
    overlay_src: str | None
    date_str: str | None
    main_rel_path: str | None = None
    overlay_rel_path: str | None = None
    composite_rel_path: str | None = None


@dataclass
class MemoriesJsonRecord:
    index: int
    raw_date: str
    date_str: str | None
    full_timestamp_exif: str | None
    full_timestamp_iso: str | None
    media_type: str
    latitude: float | None
    longitude: float | None
    is_zero_coord: bool
    download_link: str
    media_download_url: str
    local_path_hint: str = ""


@dataclass
class SidecarRecord:
    rel_path: str
    is_valid_json: bool
    media_id: str | None = None
    target_file: str | None = None
    date_str: str | None = None
    full_timestamp_exif: str | None = None
    full_timestamp_iso: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    error: str | None = None


@dataclass
class AssetEvidence:
    rel_path: str
    category: str
    filename_date: str | None = None
    gallery_dates: set[str] = field(default_factory=set)
    linked_full_timestamps: set[str] = field(default_factory=set)
    linked_locations: list[tuple[float, float]] = field(default_factory=list)
    evidence_sources: list[str] = field(default_factory=list)
    ambiguous_reasons: list[str] = field(default_factory=list)
    conflict_reasons: list[str] = field(default_factory=list)
    composite_rel_path: str | None = None


class ExifToolError(Exception):
    """Raised when ExifTool fails or rejects a metadata operation."""


class ExifToolAdapter:
    def __init__(self, exiftool_bin: str = "exiftool") -> None:
        self.exiftool_bin = exiftool_bin
        self.resolved_bin = shutil.which(exiftool_bin)

    def is_available(self) -> bool:
        return self.resolved_bin is not None

    def read_metadata(self, file_path: pathlib.Path) -> dict[str, Any]:
        if not self.resolved_bin:
            raise ExifToolError(f"ExifTool binary '{self.exiftool_bin}' not found on PATH")
        res = subprocess.run(
            [self.resolved_bin, "-j", "-n", str(file_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            raise ExifToolError(res.stderr.strip() or f"ExifTool failed to read {file_path.name}")
        try:
            payload = json.loads(res.stdout)
            if isinstance(payload, list) and payload:
                data = payload[0]
                if "Error" in data and len(data) <= 4:
                    raise ExifToolError(str(data["Error"]))
                return data
            return {}
        except json.JSONDecodeError as exc:
            raise ExifToolError(f"Invalid JSON from ExifTool for {file_path.name}: {exc}") from exc

    def read_metadata_batch(
        self,
        file_paths: list[pathlib.Path],
        chunk_size: int = 200,
    ) -> dict[str, dict[str, Any]]:
        if not self.resolved_bin or not file_paths:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for i in range(0, len(file_paths), chunk_size):
            chunk = file_paths[i : i + chunk_size]
            cmd = [self.resolved_bin, "-j", "-n"] + [str(p) for p in chunk]
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                try:
                    payload = json.loads(res.stdout)
                    if isinstance(payload, list):
                        for item in payload:
                            src_file = item.get("SourceFile")
                            if src_file:
                                p = pathlib.Path(src_file)
                                result[p.resolve().as_posix()] = item
                                result[str(p)] = item
                                result[p.as_posix()] = item
                except Exception:
                    for p in chunk:
                        try:
                            meta = self.read_metadata(p)
                            result[p.resolve().as_posix()] = meta
                            result[str(p)] = meta
                        except Exception:
                            pass
        return result

    def write_metadata_batch(
        self,
        updates: list[tuple[pathlib.Path, dict[str, Any]]],
        chunk_size: int = 200,
    ) -> list[tuple[pathlib.Path, str]]:
        if not updates or not self.resolved_bin:
            return []
        failed: list[tuple[pathlib.Path, str]] = []
        for i in range(0, len(updates), chunk_size):
            chunk = updates[i : i + chunk_size]
            lines: list[str] = []
            for path, tags in chunk:
                lines.append("-overwrite_original")
                for k, v in tags.items():
                    lines.append(f"-{k}={v}")
                lines.append(str(path))
                lines.append("-execute")

            res = subprocess.run(
                [self.resolved_bin, "-@", "-"],
                input="\n".join(lines),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                err_map: dict[str, str] = {}
                for line in res.stderr.splitlines():
                    if "Error:" in line and " - " in line:
                        parts = line.split(" - ", 1)
                        err_map[parts[1].strip()] = parts[0].strip()
                for path, tags in chunk:
                    sp = str(path)
                    if sp in err_map:
                        failed.append((path, err_map[sp]))
                    elif res.returncode != 0 and not err_map:
                        failed.append((path, res.stderr.strip() or "ExifTool batch write error"))
        return failed

    def write_metadata(self, file_path: pathlib.Path, tag_updates: dict[str, Any]) -> None:
        if not tag_updates:
            return
        if not self.resolved_bin:
            raise ExifToolError(f"ExifTool binary '{self.exiftool_bin}' not found on PATH")
        failed = self.write_metadata_batch([(file_path, tag_updates)])
        if failed:
            raise ExifToolError(failed[0][1])


def is_slow_or_9p_fs(path: pathlib.Path) -> bool:
    try:
        resolved = str(path.resolve())
        if pathlib.Path("/proc/mounts").is_file():
            mounts: list[tuple[str, str]] = []
            with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3:
                        mounts.append((parts[1], parts[2]))
            mounts.sort(key=lambda m: len(m[0]), reverse=True)
            for mp, fstype in mounts:
                if resolved == mp or resolved.startswith(mp.rstrip("/") + "/"):
                    return fstype.lower() in ("9p", "drvfs", "cifs", "smbfs", "nfs")
    except Exception:
        pass
    return False


def setup_logger(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("snapchat_processor")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def validate_paths(
    source_dir: pathlib.Path,
    output_dir: pathlib.Path,
    overwrite: bool = False,
) -> tuple[pathlib.Path, pathlib.Path]:
    if not source_dir.exists() or not source_dir.is_dir():
        raise ValueError(f"Source directory does not exist or is not a directory: {source_dir}")

    src_resolved = source_dir.resolve()
    out_resolved = output_dir.resolve()

    if out_resolved == src_resolved:
        raise ValueError("Refusing output path: output directory cannot be the same as source directory.")

    if out_resolved.is_relative_to(src_resolved):
        raise ValueError("Refusing output path: output directory cannot be inside the source tree.")

    if src_resolved.is_relative_to(out_resolved):
        raise ValueError("Refusing output path: source directory cannot be inside the output directory.")

    if out_resolved.exists() and not overwrite:
        raise FileExistsError(
            f"Refusing to overwrite existing output path '{output_dir}'. Pass --overwrite to allow."
        )

    return src_resolved, out_resolved


def parse_timestamp_utc(raw_str: str | None) -> datetime | None:
    if not raw_str or not isinstance(raw_str, str):
        return None
    s = raw_str.strip()
    if not s:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S UTC",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y:%m:%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        if s.endswith(" UTC"):
            s = s[:-4] + "+00:00"
        elif s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_date_only(raw_str: str | None) -> str | None:
    if not raw_str or not isinstance(raw_str, str):
        return None
    s = raw_str.strip()
    m = re.match(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})$", s)
    if not m:
        return None
    try:
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_location_string(loc_str: str | None) -> tuple[float | None, float | None, bool]:
    if not loc_str or not isinstance(loc_str, str):
        return None, None, False
    s = loc_str.strip()
    if not s:
        return None, None, False
    m = LOCATION_RE.search(s)
    if not m:
        m = re.match(r"^\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*$", s)
    if not m:
        return None, None, False
    try:
        lat = float(m.group(1))
        lon = float(m.group(2))
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return None, None, False
        if abs(lat) < 1e-9 and abs(lon) < 1e-9:
            return 0.0, 0.0, True
        return lat, lon, False
    except ValueError:
        return None, None, False


def is_raw_overlay_path(rel_path_str: str, explicit_overlay_paths: set[str]) -> bool:
    norm = pathlib.PurePosixPath(rel_path_str).as_posix()
    if norm in explicit_overlay_paths:
        return True
    name = pathlib.PurePosixPath(norm).name
    if MEMORIES_FILENAME_RE.match(name):
        m = MEMORIES_FILENAME_RE.match(name)
        if m and m.group("role").lower() == "overlay":
            return True
    if re.search(r"-overlay\.[A-Za-z0-9]+$", name, re.IGNORECASE):
        return True
    return False


def _memories_html_sort_key(path: pathlib.Path) -> tuple[int, str]:
    m = re.search(r"memories(\d+)?\.html$", path.name, re.IGNORECASE)
    if m:
        num = int(m.group(1)) if m.group(1) else 1
        return (num, path.name)
    return (9999, path.name)


def resolve_gallery_src(
    source_root: pathlib.Path,
    html_file: pathlib.Path,
    raw_src: str | None,
    all_rel_paths: set[str],
) -> str | None:
    if not raw_src:
        return None
    cleaned = raw_src.strip()
    while cleaned.startswith("./") or cleaned.startswith(".//"):
        cleaned = cleaned.lstrip("./")
    if not cleaned:
        return None

    html_rel_dir = html_file.parent.relative_to(source_root).as_posix()
    candidates = []
    if html_rel_dir and html_rel_dir != ".":
        candidates.append(f"{html_rel_dir}/{cleaned}")
    candidates.append(f"memories/{cleaned}")
    candidates.append(cleaned)

    for cand in candidates:
        norm = pathlib.PurePosixPath(cand).as_posix()
        if norm in all_rel_paths:
            return norm

    if html_rel_dir and html_rel_dir != ".":
        return pathlib.PurePosixPath(f"{html_rel_dir}/{cleaned}").as_posix()
    if (source_root / "memories").is_dir():
        return pathlib.PurePosixPath(f"memories/{cleaned}").as_posix()
    return pathlib.PurePosixPath(cleaned).as_posix()


def parse_memories_galleries(
    source_root: pathlib.Path,
    all_rel_paths: set[str],
) -> list[GalleryEntry]:
    html_candidates: list[pathlib.Path] = []
    for sub in ("memories", ".", "html"):
        base = source_root / sub
        if base.is_dir():
            matching_files = [
                p for p in base.glob("memories*.html")
                if p.is_file() and p.name != "memories_history.html"
            ]
            for p in sorted(matching_files, key=_memories_html_sort_key):
                if p not in html_candidates:
                    html_candidates.append(p)

    entries: list[GalleryEntry] = []
    for html_file in html_candidates:
        rel_html = html_file.relative_to(source_root).as_posix()
        soup = BeautifulSoup(html_file.read_text(encoding="utf-8", errors="replace"), "html.parser")
        for container in soup.find_all("div", class_="image-container"):
            video_tag = container.find("video")
            img_tags = container.find_all("img")
            main_img_tag = None
            overlay_img_tag = None
            for im in img_tags:
                classes = im.get("class") or []
                if isinstance(classes, str):
                    classes = classes.split()
                if "overlay-image" in classes:
                    overlay_img_tag = im
                elif main_img_tag is None:
                    main_img_tag = im

            main_src = None
            if video_tag and video_tag.get("src"):
                main_src = video_tag.get("src")
            elif main_img_tag and main_img_tag.get("src"):
                main_src = main_img_tag.get("src")

            overlay_src = overlay_img_tag.get("src") if overlay_img_tag else None
            text_line = container.find("div", class_="text-line")
            date_str = parse_date_only(text_line.get_text(strip=True)) if text_line else None

            main_rel = resolve_gallery_src(source_root, html_file, main_src, all_rel_paths)
            ov_rel = resolve_gallery_src(source_root, html_file, overlay_src, all_rel_paths)
            entries.append(
                GalleryEntry(
                    html_rel_path=rel_html,
                    main_src=main_src,
                    overlay_src=overlay_src,
                    date_str=date_str,
                    main_rel_path=main_rel,
                    overlay_rel_path=ov_rel,
                )
            )
    return entries


def parse_memories_history_json(source_root: pathlib.Path) -> list[MemoriesJsonRecord]:
    candidates = [
        source_root / "json" / "memories_history.json",
        source_root / "memories_history.json",
    ]
    json_path = next((p for p in candidates if p.is_file()), None)
    if not json_path:
        return []

    raw_data = json.loads(json_path.read_text(encoding="utf-8"))
    saved_list = raw_data.get("Saved Media", []) if isinstance(raw_data, dict) else []
    records: list[MemoriesJsonRecord] = []
    for idx, item in enumerate(saved_list):
        if not isinstance(item, dict):
            continue
        raw_date = str(item.get("Date") or "").strip()
        dt = parse_timestamp_utc(raw_date)
        date_str = dt.strftime("%Y-%m-%d") if dt else parse_date_only(raw_date)
        full_exif = dt.strftime("%Y:%m:%d %H:%M:%S") if dt else None
        full_iso = dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None
        lat, lon, is_zero = parse_location_string(item.get("Location"))
        records.append(
            MemoriesJsonRecord(
                index=idx,
                raw_date=raw_date,
                date_str=date_str,
                full_timestamp_exif=full_exif,
                full_timestamp_iso=full_iso,
                media_type=str(item.get("Media Type") or "").strip(),
                latitude=None if is_zero else lat,
                longitude=None if is_zero else lon,
                is_zero_coord=is_zero,
                download_link=str(item.get("Download Link") or "").strip(),
                media_download_url=str(item.get("Media Download Url") or "").strip(),
                local_path_hint=str(item.get("Local Path") or item.get("File") or "").strip(),
            )
        )
    return records


def parse_unknown_sidecar(source_root: pathlib.Path, rel_path: str) -> SidecarRecord:
    abs_path = source_root / rel_path
    try:
        text = abs_path.read_text(encoding="utf-8")
        data = json.loads(text)
    except Exception as exc:
        return SidecarRecord(rel_path=rel_path, is_valid_json=False, error=str(exc))

    if not isinstance(data, dict):
        return SidecarRecord(
            rel_path=rel_path,
            is_valid_json=False,
            error="JSON root is not an object",
        )

    media_id = (
        data.get("Media ID")
        or data.get("media_id")
        or data.get("id")
        or data.get("mediaId")
    )
    if media_id is not None:
        media_id = str(media_id).strip() or None

    target_file = (
        data.get("file")
        or data.get("filename")
        or data.get("path")
        or data.get("local_path")
        or data.get("File")
    )
    if target_file is not None:
        target_file = str(target_file).strip() or None

    raw_date = (
        data.get("Date")
        or data.get("date")
        or data.get("Created")
        or data.get("created")
        or data.get("timestamp")
    )
    dt = parse_timestamp_utc(str(raw_date)) if raw_date else None
    date_str = dt.strftime("%Y-%m-%d") if dt else (parse_date_only(str(raw_date)) if raw_date else None)
    full_exif = dt.strftime("%Y:%m:%d %H:%M:%S") if dt else None
    full_iso = dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None

    lat: float | None = None
    lon: float | None = None
    if "Location" in data or "location" in data:
        loc_val = data.get("Location") or data.get("location")
        if isinstance(loc_val, str):
            p_lat, p_lon, is_z = parse_location_string(loc_val)
            if not is_z:
                lat, lon = p_lat, p_lon
        elif isinstance(loc_val, dict):
            try:
                p_lat = float(loc_val.get("latitude") or loc_val.get("lat"))
                p_lon = float(loc_val.get("longitude") or loc_val.get("lon") or loc_val.get("lng"))
                if not (abs(p_lat) < 1e-9 and abs(p_lon) < 1e-9):
                    lat, lon = p_lat, p_lon
            except Exception:
                pass
    elif "latitude" in data and "longitude" in data:
        try:
            p_lat = float(data["latitude"])
            p_lon = float(data["longitude"])
            if not (abs(p_lat) < 1e-9 and abs(p_lon) < 1e-9):
                lat, lon = p_lat, p_lon
        except Exception:
            pass

    return SidecarRecord(
        rel_path=rel_path,
        is_valid_json=True,
        media_id=media_id,
        target_file=target_file,
        date_str=date_str,
        full_timestamp_exif=full_exif,
        full_timestamp_iso=full_iso,
        latitude=lat,
        longitude=lon,
    )


def extract_chat_media_identifiers(rel_path: str) -> tuple[str | None, set[str]]:
    p = pathlib.PurePosixPath(rel_path)
    name = p.name
    stem = p.stem
    filename_date: str | None = None
    ids: set[str] = {stem}

    m = LEADING_DATE_FILENAME_RE.match(name)
    if m:
        filename_date = parse_date_only(m.group("date"))
        rest = m.group("rest")
        ids.add(rest)
        if "~" in rest:
            after_tilde = rest.split("~", 1)[1]
            ids.add(after_tilde)

    for b_match in B_TOKEN_RE.findall(stem):
        ids.add(b_match)
    for b_no_us in re.findall(r"(b~[A-Za-z0-9\-.~]+)", stem):
        ids.add(b_no_us)
    for uuid_match in UUID_RE.findall(name):
        ids.add(uuid_match.lower())
        ids.add(uuid_match.upper())

    return filename_date, ids


def align_memories_sequence_to_assets(
    gallery_entries: list[GalleryEntry],
    json_records: list[MemoriesJsonRecord],
    already_matched_assets: set[str],
) -> dict[str, MemoriesJsonRecord]:
    if not gallery_entries or not json_records:
        return {}

    target_entries = [
        e for e in gallery_entries
        if e.main_rel_path and e.main_rel_path not in already_matched_assets and e.date_str
    ]
    if not target_entries:
        return {}

    gal_dates = [e.date_str for e in target_entries if e.date_str]
    if any(gal_dates[i] > gal_dates[i + 1] for i in range(len(gal_dates) - 1)):
        return {}

    valid_json_dates = [r.date_str for r in json_records if r.date_str]
    if len(valid_json_dates) >= 2 and valid_json_dates[0] > valid_json_dates[-1]:
        json_seq = list(reversed(json_records))
    else:
        json_seq = list(json_records)

    matched: dict[str, MemoriesJsonRecord] = {}
    j_idx = 0
    for entry in target_entries:
        main_rel = entry.main_rel_path
        if not main_rel or not entry.date_str:
            continue
        g_date = entry.date_str
        ext = pathlib.PurePosixPath(main_rel).suffix.lower()
        g_is_vid = ext in SUPPORTED_VIDEO_EXTENSIONS

        while j_idx < len(json_seq) and json_seq[j_idx].date_str and json_seq[j_idx].date_str < g_date:
            j_idx += 1

        if j_idx < len(json_seq) and json_seq[j_idx].date_str == g_date:
            rec = json_seq[j_idx]
            rec_is_vid = rec.media_type.lower() == "video"
            if g_is_vid == rec_is_vid:
                matched[main_rel] = rec
                j_idx += 1
            else:
                found = False
                lookahead = j_idx + 1
                while lookahead < len(json_seq) and json_seq[lookahead].date_str == g_date:
                    cand = json_seq[lookahead]
                    if (cand.media_type.lower() == "video") == g_is_vid:
                        matched[main_rel] = cand
                        json_seq.pop(lookahead)
                        found = True
                        break
                    lookahead += 1
                if not found:
                    break
        else:
            break

    return matched


def composite_image_pair(
    main_path: pathlib.Path,
    overlay_path: pathlib.Path,
    composite_out_path: pathlib.Path,
) -> None:
    composite_out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(main_path) as raw_main:
            orig_format = raw_main.format or (
                "JPEG" if composite_out_path.suffix.lower() in (".jpg", ".jpeg") else "PNG"
            )
            icc_profile = raw_main.info.get("icc_profile")

            was_transposed = False
            main_img = ImageOps.exif_transpose(raw_main)
            if main_img is not None and main_img is not raw_main:
                was_transposed = True
            else:
                main_img = raw_main

            orig_exif_bytes = raw_main.info.get("exif")
            if was_transposed:
                exif_obj = raw_main.getexif()
                if exif_obj is not None:
                    exif_obj[0x0112] = 1
                    orig_exif_bytes = exif_obj.tobytes()
            elif not orig_exif_bytes:
                exif_obj = raw_main.getexif()
                if exif_obj is not None and len(exif_obj) > 0:
                    orig_exif_bytes = exif_obj.tobytes()

            base_rgba = main_img.convert("RGBA")

        with Image.open(overlay_path) as raw_ov:
            ov_img = ImageOps.exif_transpose(raw_ov)
            if ov_img is None:
                ov_img = raw_ov
            ov_rgba = ov_img.convert("RGBA")
            if ov_rgba.size != base_rgba.size:
                ov_rgba = ov_rgba.resize(base_rgba.size, Image.Resampling.LANCZOS)

        composited = Image.alpha_composite(base_rgba, ov_rgba)
        save_kwargs: dict[str, Any] = {}
        if orig_exif_bytes:
            save_kwargs["exif"] = orig_exif_bytes
        if icc_profile:
            save_kwargs["icc_profile"] = icc_profile

        if orig_format.upper() == "JPEG" or composite_out_path.suffix.lower() in (".jpg", ".jpeg"):
            final_img = composited.convert("RGB")
            save_kwargs["quality"] = 95
            final_img.save(composite_out_path, format="JPEG", **save_kwargs)
        else:
            composited.save(composite_out_path, format=orig_format, **save_kwargs)
    except Exception:
        if composite_out_path.is_file():
            try:
                composite_out_path.unlink()
            except OSError:
                pass
        raise


def _extract_embedded_date_and_time(meta: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    for key in ("DateTimeOriginal", "CreateDate", "DateCreated", "XMP:DateCreated", "TrackCreateDate", "MediaCreateDate"):
        val = meta.get(key)
        if not val or not isinstance(val, str):
            continue
        s = val.strip()
        if s.startswith("0000"):
            continue
        m_full = re.match(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[T ](\d{2}:\d{2}:\d{2})", s)
        if m_full:
            d_str = f"{m_full.group(1)}-{m_full.group(2)}-{m_full.group(3)}"
            t_str = m_full.group(4)
            exif_str = f"{m_full.group(1)}:{m_full.group(2)}:{m_full.group(3)} {t_str}"
            return d_str, t_str, exif_str
        m_date = re.match(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})$", s)
        if m_date:
            d_str = f"{m_date.group(1)}-{m_date.group(2)}-{m_date.group(3)}"
            return d_str, None, None
    return None, None, None


def _extract_embedded_gps(meta: dict[str, Any]) -> tuple[float, float] | None:
    if "GPSLatitude" in meta and "GPSLongitude" in meta:
        try:
            lat = float(meta["GPSLatitude"])
            lon = float(meta["GPSLongitude"])
            lat_ref = str(meta.get("GPSLatitudeRef") or "").upper()
            lon_ref = str(meta.get("GPSLongitudeRef") or "").upper()
            if lat_ref == "S" and lat > 0:
                lat = -lat
            if lon_ref == "W" and lon > 0:
                lon = -lon
            return (lat, lon)
        except Exception:
            pass
    if "GPSCoordinates" in meta:
        parts = [p.strip() for p in re.split(r"[, ]+", str(meta["GPSCoordinates"]).strip()) if p.strip()]
        if len(parts) >= 2:
            try:
                return (float(parts[0]), float(parts[1]))
            except Exception:
                pass
    return None


def escape_markdown(text: str | None) -> str:
    if text is None:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    escaped_chars = []
    for ch in s:
        if ch in ("\\", "`", "*", "_", "[", "]", "<", ">", "|", "~"):
            escaped_chars.append("\\" + ch)
        else:
            escaped_chars.append(ch)
    res = "".join(escaped_chars)
    lines = res.split("\n")
    safe_lines = []
    for line in lines:
        stripped = line.lstrip(" \t")
        indent = line[: len(line) - len(stripped)]
        if stripped.startswith("#"):
            safe_lines.append(f"{indent}\\{stripped}")
        elif stripped.startswith(("- ", "+ ")):
            safe_lines.append(f"{indent}\\{stripped}")
        elif re.match(r"^\d+\.\s", stripped):
            dot_idx = stripped.find(".")
            safe_lines.append(f"{indent}{stripped[:dot_idx]}\\.{stripped[dot_idx + 1:]}")
        else:
            safe_lines.append(line)
    return "\n".join(safe_lines)


def slugify_conversation_key(conv_key: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", conv_key.strip()).strip("_")
    return slug or "conversation"


def export_chats_to_markdown(
    chat_json_data: dict[str, Any],
    output_root: pathlib.Path,
    unique_media_id_to_rel_path: dict[str, str],
    ambiguous_media_ids: set[str],
    chunk_size: int = 500,
    dry_run: bool = False,
) -> dict[str, Any]:
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")

    chat_md_dir = output_root / "chat_markdown"
    if not dry_run:
        chat_md_dir.mkdir(parents=True, exist_ok=True)

    conv_summaries: list[dict[str, Any]] = []
    total_messages = 0
    total_linked_attachments = 0
    total_unlinked_attachments = 0
    total_chunk_files = 0
    used_slugs: set[str] = set()

    for conv_key, raw_messages in chat_json_data.items():
        if not isinstance(raw_messages, list):
            continue

        base_slug = slugify_conversation_key(str(conv_key))
        slug = base_slug
        counter = 2
        while slug.lower() in used_slugs:
            slug = f"{base_slug}_{counter}"
            counter += 1
        used_slugs.add(slug.lower())

        titles = [
            str(m.get("Conversation Title")).strip()
            for m in raw_messages
            if isinstance(m, dict) and m.get("Conversation Title") and str(m.get("Conversation Title")).strip()
        ]
        conv_title = titles[0] if titles else None
        display_header = (
            f"{escape_markdown(conv_title)} (`{escape_markdown(str(conv_key))}`)"
            if conv_title
            else f"`{escape_markdown(str(conv_key))}`"
        )

        decorated_msgs = []
        for idx, msg in enumerate(raw_messages):
            if not isinstance(msg, dict):
                continue
            created_str = str(msg.get("Created") or "").strip()
            dt = parse_timestamp_utc(created_str)
            micros_raw = msg.get("Created(microseconds)")
            micros_val: int | None = None
            if isinstance(micros_raw, (int, float)):
                micros_val = int(micros_raw)
            elif isinstance(micros_raw, str) and micros_raw.strip().isdigit():
                micros_val = int(micros_raw.strip())

            if dt is not None:
                sort_ts = dt.timestamp()
                has_ts = 0
                ts_label = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            elif micros_val is not None:
                sort_ts = micros_val / 1000.0 if micros_val < 10**14 else micros_val / 1_000_000.0
                dt = datetime.fromtimestamp(sort_ts, tz=timezone.utc)
                has_ts = 0
                ts_label = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            else:
                sort_ts = float("inf")
                has_ts = 1
                ts_label = "Unknown Timestamp"

            sub_sort = micros_val if micros_val is not None else 0
            decorated_msgs.append((has_ts, sort_ts, sub_sort, idx, ts_label, dt, msg))

        decorated_msgs.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        total_messages += len(decorated_msgs)

        chunks: list[list[tuple[Any, ...]]] = []
        if not decorated_msgs:
            chunks = [[]]
        else:
            for i in range(0, len(decorated_msgs), chunk_size):
                chunks.append(decorated_msgs[i : i + chunk_size])

        num_chunks = len(chunks)
        chunk_filenames: list[str] = []
        for c_idx in range(num_chunks):
            if num_chunks == 1:
                chunk_filenames.append(f"{slug}.md")
            else:
                chunk_filenames.append(f"{slug}_part{c_idx + 1:03d}.md")

        conv_linked = 0
        conv_unlinked = 0
        valid_dts = [item[5] for item in decorated_msgs if item[5] is not None]
        first_date = valid_dts[0].strftime("%Y-%m-%d") if valid_dts else "N/A"
        last_date = valid_dts[-1].strftime("%Y-%m-%d") if valid_dts else "N/A"

        for c_idx, chunk_items in enumerate(chunks):
            fname = chunk_filenames[c_idx]
            total_chunk_files += 1
            lines: list[str] = []
            part_suffix = f" (Part {c_idx + 1} of {num_chunks})" if num_chunks > 1 else ""
            lines.append(f"# Conversation: {display_header}{part_suffix}")
            lines.append("")
            lines.append(f"- **Conversation Key**: `{escape_markdown(str(conv_key))}`")
            if conv_title:
                lines.append(f"- **Conversation Title**: {escape_markdown(conv_title)}")
            lines.append(f"- **Messages in File**: {len(chunk_items)} (Total in Conversation: {len(decorated_msgs)})")
            lines.append(f"- **Date Range**: {first_date} to {last_date}")
            lines.append("")

            nav_parts = ["[Back to Chat Index](index.md)"]
            if num_chunks > 1:
                if c_idx > 0:
                    nav_parts.append(f"[Previous Part]({chunk_filenames[c_idx - 1]})")
                if c_idx + 1 < num_chunks:
                    nav_parts.append(f"[Next Part]({chunk_filenames[c_idx + 1]})")
            lines.append(" | ".join(nav_parts))
            lines.append("")
            lines.append("## Messages")
            lines.append("")

            if not chunk_items:
                lines.append("*(No messages in this conversation)*")
                lines.append("")

            for _, _, _, _, ts_label, _, msg in chunk_items:
                sender = str(msg.get("From") or "Unknown").strip() or "Unknown"
                media_type = str(msg.get("Media Type") or "TEXT").strip() or "TEXT"
                raw_content = msg.get("Content")
                escaped_sender = escape_markdown(sender)
                escaped_media_type = escape_markdown(media_type)

                lines.append(f"### {ts_label} — **{escaped_sender}** (`{escaped_media_type}`)")
                if raw_content is None:
                    lines.append("*(null content)*")
                elif str(raw_content) == "":
                    lines.append("*(empty text)*")
                else:
                    escaped_body = escape_markdown(str(raw_content))
                    lines.append(escaped_body)

                raw_media_ids = str(msg.get("Media IDs") or "").strip()
                if raw_media_ids:
                    tokens = [t.strip() for t in raw_media_ids.split("|") if t.strip()]
                    att_bullets = []
                    for tok in tokens:
                        if tok in unique_media_id_to_rel_path and tok not in ambiguous_media_ids:
                            target_rel = unique_media_id_to_rel_path[tok]
                            rel_from_md = os.path.relpath(
                                output_root / target_rel, chat_md_dir
                            ).replace(os.sep, "/")
                            encoded_rel = "/".join(
                                urllib.parse.quote(part) for part in rel_from_md.split("/")
                            )
                            target_name = pathlib.PurePosixPath(target_rel).name
                            att_bullets.append(
                                f"- Attachment: [{escape_markdown(target_name)}]({encoded_rel})"
                            )
                            conv_linked += 1
                            total_linked_attachments += 1
                        elif tok in ambiguous_media_ids:
                            att_bullets.append("- Attachment: *(ambiguous media ID — unlinked)*")
                            conv_unlinked += 1
                            total_unlinked_attachments += 1
                        else:
                            att_bullets.append("- Attachment: *(unmatched media ID — unlinked)*")
                            conv_unlinked += 1
                            total_unlinked_attachments += 1
                    if att_bullets:
                        lines.extend(att_bullets)
                lines.append("")

            if not dry_run:
                (chat_md_dir / fname).write_text("\n".join(lines), encoding="utf-8")

        conv_summaries.append(
            {
                "conversation_key": str(conv_key),
                "conversation_title": conv_title,
                "message_count": len(decorated_msgs),
                "chunk_count": num_chunks,
                "files": chunk_filenames,
                "first_date": first_date,
                "last_date": last_date,
                "linked_attachments": conv_linked,
                "unlinked_attachments": conv_unlinked,
            }
        )

    index_lines = [
        "# Snapchat Chat History Index",
        "",
        f"- **Total Conversations**: {len(conv_summaries)}",
        f"- **Total Messages**: {total_messages}",
        f"- **Linked Attachments**: {total_linked_attachments}",
        f"- **Unlinked/Ambiguous Attachments**: {total_unlinked_attachments}",
        "",
        "| Conversation | Key | Messages | Date Range | Linked Media | Files |",
        "| :--- | :--- | ---: | :--- | ---: | :--- |",
    ]
    for summary in conv_summaries:
        title_cell = (
            escape_markdown(summary["conversation_title"])
            if summary["conversation_title"]
            else "*(Direct / Untitled)*"
        )
        key_cell = f"`{escape_markdown(summary['conversation_key'])}`"
        file_links = ", ".join(
            f"[Part {i + 1}]({fn})" if len(summary["files"]) > 1 else f"[View]({fn})"
            for i, fn in enumerate(summary["files"])
        )
        index_lines.append(
            f"| {title_cell} | {key_cell} | {summary['message_count']} | "
            f"{summary['first_date']} – {summary['last_date']} | "
            f"{summary['linked_attachments']} | {file_links} |"
        )
    index_lines.append("")

    if not dry_run:
        (chat_md_dir / "index.md").write_text("\n".join(index_lines), encoding="utf-8")

    return {
        "conversations_exported": len(conv_summaries),
        "total_messages_exported": total_messages,
        "total_markdown_files": total_chunk_files + 1,
        "linked_attachments": total_linked_attachments,
        "unlinked_attachments": total_unlinked_attachments,
        "index_path": "chat_markdown/index.md",
        "conversations": conv_summaries,
    }


def process_export(
    source_dir: pathlib.Path,
    output_dir: pathlib.Path,
    dry_run: bool = False,
    overwrite: bool = False,
    chunk_size: int = 500,
    exiftool_bin: str = "exiftool",
    align_memories_sequence: bool = True,
    temp_staging: str = "auto",
    verbose: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    logger = setup_logger(verbose=verbose)
    src_root, out_root = validate_paths(source_dir, output_dir, overwrite=overwrite)
    exiftool = ExifToolAdapter(exiftool_bin=exiftool_bin)

    if not exiftool.is_available():
        logger.warning(
            "ExifTool binary '%s' not found on PATH. Media copying, compositing, and chat export "
            "will proceed, but EXIF/QuickTime tag writes will be skipped.",
            exiftool_bin,
        )

    all_files: list[pathlib.Path] = []
    all_rel_paths: set[str] = set()
    for root, _, files in os.walk(src_root):
        for fname in sorted(files):
            fpath = pathlib.Path(root) / fname
            rel_posix = fpath.relative_to(src_root).as_posix()
            all_files.append(fpath)
            all_rel_paths.add(rel_posix)

    gallery_entries = parse_memories_galleries(src_root, all_rel_paths)
    memories_json_records = parse_memories_history_json(src_root)

    chat_candidates = [
        src_root / "json" / "chat_history.json",
        src_root / "chat_history.json",
    ]
    chat_json_path = next((p for p in chat_candidates if p.is_file()), None)
    chat_json_data: dict[str, Any] = {}
    if chat_json_path:
        chat_json_data = json.loads(chat_json_path.read_text(encoding="utf-8"))

    explicit_overlay_paths: set[str] = {
        e.overlay_rel_path for e in gallery_entries if e.overlay_rel_path
    }
    omitted_overlays: list[str] = []
    mirrored_files: list[str] = []

    for rel_posix in sorted(all_rel_paths):
        ext = pathlib.PurePosixPath(rel_posix).suffix.lower()
        if ext in ALL_MEDIA_EXTENSIONS:
            if is_raw_overlay_path(rel_posix, explicit_overlay_paths):
                omitted_overlays.append(rel_posix)
            else:
                mirrored_files.append(rel_posix)

    use_staging = False
    if not dry_run:
        if temp_staging == "always":
            use_staging = True
        elif temp_staging == "auto":
            use_staging = is_slow_or_9p_fs(out_root) or is_slow_or_9p_fs(src_root)

    temp_stage_dir: tempfile.TemporaryDirectory[str] | None = None
    work_root = out_root
    if use_staging:
        temp_stage_dir = tempfile.TemporaryDirectory(prefix="snapchat_proc_", dir="/tmp")
        work_root = pathlib.Path(temp_stage_dir.name)

    if not dry_run:
        work_root.mkdir(parents=True, exist_ok=True)
        parent_dirs = {(work_root / rel_posix).parent for rel_posix in mirrored_files}
        for d in parent_dirs:
            d.mkdir(parents=True, exist_ok=True)

        def _copy_worker(rel_p: str) -> None:
            shutil.copy2(src_root / rel_p, work_root / rel_p)

        max_copy_workers = min(32, (os.cpu_count() or 4) * 4)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_copy_workers) as executor:
            list(executor.map(_copy_worker, mirrored_files))

    memories_mains_by_stem: dict[str, str] = {}
    memories_overlays_by_stem: dict[str, str] = {}
    uuid_to_memories_main: dict[str, list[str]] = defaultdict(list)

    for rel_posix in sorted(all_rel_paths):
        p = pathlib.PurePosixPath(rel_posix)
        m = MEMORIES_FILENAME_RE.match(p.name)
        if m:
            stem_key = f"{p.parent.as_posix()}/{m.group('date')}_{m.group('uuid').lower()}"
            role = m.group("role").lower()
            if role == "main":
                memories_mains_by_stem[stem_key] = rel_posix
                uuid_to_memories_main[m.group("uuid").lower()].append(rel_posix)
            elif role == "overlay":
                memories_overlays_by_stem[stem_key] = rel_posix

    main_to_overlay: dict[str, str] = {}
    for entry in gallery_entries:
        if entry.main_rel_path and entry.overlay_rel_path:
            main_name = pathlib.PurePosixPath(entry.main_rel_path).name
            ov_name = pathlib.PurePosixPath(entry.overlay_rel_path).name
            m_main = MEMORIES_FILENAME_RE.match(main_name)
            m_ov = MEMORIES_FILENAME_RE.match(ov_name)
            if m_main and m_ov:
                if (
                    m_main.group("date") == m_ov.group("date")
                    and m_main.group("uuid").lower() == m_ov.group("uuid").lower()
                ):
                    main_to_overlay[entry.main_rel_path] = entry.overlay_rel_path
            else:
                main_to_overlay[entry.main_rel_path] = entry.overlay_rel_path

    for stem_key, main_rel in memories_mains_by_stem.items():
        if stem_key in memories_overlays_by_stem and main_rel not in main_to_overlay:
            main_to_overlay[main_rel] = memories_overlays_by_stem[stem_key]

    composites_created: list[dict[str, str]] = []
    failed_composites: list[dict[str, str]] = []
    video_overlays_omitted: list[dict[str, str]] = []
    main_to_composite_rel: dict[str, str] = {}

    composite_tasks: list[tuple[str, str, str]] = []
    for main_rel, ov_rel in sorted(main_to_overlay.items()):
        if main_rel not in all_rel_paths or ov_rel not in all_rel_paths:
            continue
        main_p = pathlib.PurePosixPath(main_rel)
        ext = main_p.suffix.lower()
        if ext in SUPPORTED_IMAGE_EXTENSIONS:
            comp_name = re.sub(r"-main(\.[^.]+)$", r"-composite\1", main_p.name, flags=re.IGNORECASE)
            if comp_name == main_p.name:
                comp_name = f"{main_p.stem}-composite{main_p.suffix}"
            comp_rel = (main_p.parent / comp_name).as_posix() if main_p.parent.as_posix() != "." else comp_name
            composite_tasks.append((main_rel, ov_rel, comp_rel))
        else:
            video_overlays_omitted.append(
                {
                    "main_rel_path": main_rel,
                    "overlay_rel_path": ov_rel,
                    "reason": "video_or_non_still_overlay_omitted_without_image_composite",
                }
            )

    if not dry_run:
        def _comp_worker(task: tuple[str, str, str]) -> tuple[str, str, str, bool, str | None]:
            m_rel, o_rel, c_rel = task
            try:
                composite_image_pair(src_root / m_rel, src_root / o_rel, work_root / c_rel)
                return m_rel, o_rel, c_rel, True, None
            except Exception as exc:
                return m_rel, o_rel, c_rel, False, str(exc)

        max_comp_workers = min(16, os.cpu_count() or 4)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_comp_workers) as executor:
            comp_results = list(executor.map(_comp_worker, composite_tasks))

        for m_rel, o_rel, c_rel, success, err in comp_results:
            if success:
                composites_created.append(
                    {
                        "main_rel_path": m_rel,
                        "overlay_rel_path": o_rel,
                        "composite_rel_path": c_rel,
                    }
                )
                main_to_composite_rel[m_rel] = c_rel
            else:
                logger.warning(
                    "Skipping overlay compositing for %s onto %s: %s",
                    o_rel,
                    m_rel,
                    err,
                )
                failed_composites.append(
                    {
                        "main_rel_path": m_rel,
                        "overlay_rel_path": o_rel,
                        "reason": f"composite_error:{err}",
                    }
                )
    else:
        for m_rel, o_rel, c_rel in composite_tasks:
            try:
                with Image.open(src_root / m_rel), Image.open(src_root / o_rel):
                    composites_created.append(
                        {
                            "main_rel_path": m_rel,
                            "overlay_rel_path": o_rel,
                            "composite_rel_path": c_rel,
                        }
                    )
                    main_to_composite_rel[m_rel] = c_rel
            except Exception as exc:
                failed_composites.append(
                    {
                        "main_rel_path": m_rel,
                        "overlay_rel_path": o_rel,
                        "reason": f"unreadable_image_pair:{exc}",
                    }
                )

    asset_evidence: dict[str, AssetEvidence] = {}
    chat_id_to_files: dict[str, list[str]] = defaultdict(list)
    stem_to_files: dict[str, list[str]] = defaultdict(list)
    sidecar_records: list[SidecarRecord] = []
    unsupported_files: list[dict[str, str]] = []

    for rel_posix in all_rel_paths:
        p = pathlib.PurePosixPath(rel_posix)
        ext = p.suffix.lower()
        if ext == ".unknown":
            sc = parse_unknown_sidecar(src_root, rel_posix)
            sidecar_records.append(sc)
            if not sc.is_valid_json:
                unsupported_files.append(
                    {
                        "rel_path": rel_posix,
                        "reason": f"unreadable_unknown_sidecar:{sc.error}",
                    }
                )

    for rel_posix in mirrored_files:
        p = pathlib.PurePosixPath(rel_posix)
        ext = p.suffix.lower()
        if ext not in ALL_MEDIA_EXTENSIONS:
            continue

        top_folder = p.parts[0].lower() if len(p.parts) > 1 else ""
        m_mem = MEMORIES_FILENAME_RE.match(p.name)
        if top_folder == "memories" or m_mem:
            cat = "memories"
            f_date = parse_date_only(m_mem.group("date")) if m_mem else None
            if not f_date:
                m_lead = LEADING_DATE_FILENAME_RE.match(p.name)
                f_date = parse_date_only(m_lead.group("date")) if m_lead else None
            ev = AssetEvidence(
                rel_path=rel_posix,
                category=cat,
                filename_date=f_date,
                composite_rel_path=main_to_composite_rel.get(rel_posix),
            )
            if f_date:
                ev.evidence_sources.append("filename_date")
            asset_evidence[rel_posix] = ev
        else:
            cat = "chat_media" if top_folder == "chat_media" else "other"
            f_date, cand_ids = extract_chat_media_identifiers(rel_posix)
            ev = AssetEvidence(
                rel_path=rel_posix,
                category=cat,
                filename_date=f_date,
            )
            if f_date:
                ev.evidence_sources.append("filename_date")
            asset_evidence[rel_posix] = ev
            for cid in cand_ids:
                chat_id_to_files[cid].append(rel_posix)
            stem_to_files[p.stem].append(rel_posix)

    for entry in gallery_entries:
        if entry.main_rel_path and entry.main_rel_path in asset_evidence and entry.date_str:
            asset_evidence[entry.main_rel_path].gallery_dates.add(entry.date_str)
            if "gallery_html_date" not in asset_evidence[entry.main_rel_path].evidence_sources:
                asset_evidence[entry.main_rel_path].evidence_sources.append("gallery_html_date")

    zero_coord_skipped_count = 0
    matched_json_by_asset: dict[str, list[MemoriesJsonRecord]] = defaultdict(list)
    asset_by_filename: dict[str, list[str]] = defaultdict(list)
    for rel_p in asset_evidence:
        asset_by_filename[pathlib.PurePosixPath(rel_p).name].append(rel_p)

    for rec in memories_json_records:
        if rec.is_zero_coord:
            zero_coord_skipped_count += 1
        link_blobs = [rec.download_link, rec.media_download_url, rec.local_path_hint]
        non_empty_blobs = [b for b in link_blobs if b]
        if not non_empty_blobs:
            continue

        matched_assets: set[str] = set()
        for blob in non_empty_blobs:
            decoded_blob = urllib.parse.unquote(blob)
            blob_name = pathlib.PurePosixPath(decoded_blob.split("?")[0]).name
            if blob_name in asset_by_filename:
                matched_assets.update(asset_by_filename[blob_name])
            if not matched_assets:
                for uuid_tok in UUID_RE.findall(decoded_blob):
                    u_low = uuid_tok.lower()
                    if u_low in uuid_to_memories_main:
                        for rel_p in uuid_to_memories_main[u_low]:
                            if rel_p in asset_evidence:
                                matched_assets.add(rel_p)
            if not matched_assets:
                for fname, rel_list in asset_by_filename.items():
                    if fname in decoded_blob:
                        matched_assets.update(rel_list)

        if len(matched_assets) == 1:
            target_rel = next(iter(matched_assets))
            matched_json_by_asset[target_rel].append(rec)
        elif len(matched_assets) > 1:
            for target_rel in matched_assets:
                asset_evidence[target_rel].ambiguous_reasons.append(
                    f"memories_json_record_{rec.index}_matches_multiple_assets"
                )

    sequence_aligned_count = 0
    if align_memories_sequence and memories_json_records:
        seq_matches = align_memories_sequence_to_assets(
            gallery_entries=gallery_entries,
            json_records=memories_json_records,
            already_matched_assets=set(matched_json_by_asset.keys()),
        )
        for target_rel, rec in seq_matches.items():
            if target_rel in asset_evidence and target_rel not in matched_json_by_asset:
                matched_json_by_asset[target_rel].append(rec)
                sequence_aligned_count += 1

    matched_record_indices = {r.index for recs in matched_json_by_asset.values() for r in recs}
    unlinked_memories_json_count = len(memories_json_records) - len(matched_record_indices)

    for target_rel, recs in matched_json_by_asset.items():
        ev = asset_evidence[target_rel]
        for r in recs:
            if r.full_timestamp_exif:
                ev.linked_full_timestamps.add(r.full_timestamp_exif)
            if r.latitude is not None and r.longitude is not None:
                ev.linked_locations.append((r.latitude, r.longitude))
        if target_rel in [e.main_rel_path for e in gallery_entries]:
            ev.evidence_sources.append("memories_history_json")

    media_id_to_msg_timestamps: dict[str, set[str]] = defaultdict(set)
    all_chat_media_ids: set[str] = set()
    for _, raw_msgs in chat_json_data.items():
        if not isinstance(raw_msgs, list):
            continue
        for msg in raw_msgs:
            if not isinstance(msg, dict):
                continue
            raw_ids = str(msg.get("Media IDs") or "").strip()
            if not raw_ids:
                continue
            dt = parse_timestamp_utc(str(msg.get("Created") or ""))
            exif_ts = dt.strftime("%Y:%m:%d %H:%M:%S") if dt else None
            for tok in (t.strip() for t in raw_ids.split("|") if t.strip()):
                all_chat_media_ids.add(tok)
                if exif_ts:
                    media_id_to_msg_timestamps[tok].add(exif_ts)

    unique_media_id_to_rel_path: dict[str, str] = {}
    ambiguous_media_ids: set[str] = set()

    for mid in sorted(all_chat_media_ids):
        matching_files = sorted(set(chat_id_to_files.get(mid, [])))
        msg_timestamps = media_id_to_msg_timestamps.get(mid, set())
        if len(matching_files) == 1:
            target_rel = matching_files[0]
            if len(msg_timestamps) > 1:
                ambiguous_media_ids.add(mid)
                asset_evidence[target_rel].conflict_reasons.append(
                    "multiple_chat_messages_with_disagreeing_timestamps_for_media_id"
                )
            else:
                unique_media_id_to_rel_path[mid] = target_rel
                if len(msg_timestamps) == 1:
                    asset_evidence[target_rel].linked_full_timestamps.add(next(iter(msg_timestamps)))
                    asset_evidence[target_rel].evidence_sources.append("chat_history_json")
        elif len(matching_files) > 1:
            ambiguous_media_ids.add(mid)
            for target_rel in matching_files:
                asset_evidence[target_rel].ambiguous_reasons.append(
                    "multiple_chat_media_files_match_same_media_id"
                )

    for sc in sidecar_records:
        if not sc.is_valid_json:
            continue
        matched_targets: set[str] = set()
        if sc.target_file:
            for rel_p in asset_evidence:
                if rel_p == sc.target_file or pathlib.PurePosixPath(rel_p).name == sc.target_file:
                    matched_targets.add(rel_p)
        if not matched_targets and sc.media_id:
            for rel_p in chat_id_to_files.get(sc.media_id, []):
                matched_targets.add(rel_p)
        if not matched_targets:
            sc_stem = pathlib.PurePosixPath(sc.rel_path).stem
            base_stem = pathlib.PurePosixPath(sc_stem).stem if "." in sc_stem else sc_stem
            for cand_stem in {sc_stem, base_stem}:
                if cand_stem in stem_to_files:
                    matched_targets.update(stem_to_files[cand_stem])

        if len(matched_targets) == 1:
            target_rel = next(iter(matched_targets))
            ev = asset_evidence[target_rel]
            if sc.full_timestamp_exif:
                ev.linked_full_timestamps.add(sc.full_timestamp_exif)
            elif sc.date_str:
                ev.gallery_dates.add(sc.date_str)
            if sc.latitude is not None and sc.longitude is not None:
                ev.linked_locations.append((sc.latitude, sc.longitude))
            ev.evidence_sources.append("unknown_json_sidecar")
        elif len(matched_targets) > 1:
            for target_rel in matched_targets:
                asset_evidence[target_rel].ambiguous_reasons.append(
                    "unknown_sidecar_matches_multiple_files"
                )

    changed_files: list[dict[str, Any]] = []
    skipped_files: list[dict[str, Any]] = []
    conflict_files: list[dict[str, Any]] = []
    ambiguous_files: list[dict[str, Any]] = []
    existing_meta_cache: dict[str, dict[str, Any]] = {}

    if exiftool.is_available():
        files_to_read = [
            (work_root / rel_pos) if not dry_run else (src_root / rel_pos)
            for rel_pos in asset_evidence
            if ((work_root / rel_pos) if not dry_run else (src_root / rel_pos)).is_file()
        ]
        if files_to_read:
            existing_meta_cache = exiftool.read_metadata_batch(files_to_read)

    pending_updates: list[tuple[pathlib.Path, dict[str, Any], str, list[str]]] = []

    for rel_posix, ev in sorted(asset_evidence.items()):
        ext = pathlib.PurePosixPath(rel_posix).suffix.lower()
        if ext in UNSUPPORTED_VIDEO_OR_MEDIA_EXTENSIONS:
            unsupported_files.append(
                {
                    "rel_path": rel_posix,
                    "reason": f"unsupported_media_format:{ext}",
                }
            )
            continue

        if ev.ambiguous_reasons:
            ambiguous_files.append(
                {
                    "rel_path": rel_posix,
                    "reasons": sorted(set(ev.ambiguous_reasons)),
                }
            )
            continue

        target_file_for_read = (work_root / rel_posix) if not dry_run else (src_root / rel_posix)
        resolved_key = target_file_for_read.resolve().as_posix()
        existing_meta = existing_meta_cache.get(
            resolved_key, existing_meta_cache.get(str(target_file_for_read))
        )
        if existing_meta is None and exiftool.is_available() and target_file_for_read.is_file():
            try:
                existing_meta = exiftool.read_metadata(target_file_for_read)
                existing_meta_cache[resolved_key] = existing_meta
            except ExifToolError as exc:
                unsupported_files.append(
                    {
                        "rel_path": rel_posix,
                        "reason": f"exiftool_read_error:{exc}",
                    }
                )
                continue
        if existing_meta is None:
            existing_meta = {}

        emb_date, emb_time, emb_full_exif = _extract_embedded_date_and_time(existing_meta)
        emb_gps = _extract_embedded_gps(existing_meta)

        date_only_candidates: set[str] = set()
        if ev.filename_date:
            date_only_candidates.add(ev.filename_date)
        date_only_candidates.update(ev.gallery_dates)

        date_conflict_reasons: list[str] = list(ev.conflict_reasons)
        if len(date_only_candidates) > 1:
            date_conflict_reasons.append("disagreement_between_filename_and_gallery_date")

        agreed_date_only = next(iter(date_only_candidates)) if len(date_only_candidates) == 1 else None

        if len(ev.linked_full_timestamps) > 1:
            date_conflict_reasons.append("multiple_disagreeing_linked_full_timestamps")

        linked_full_ts = (
            next(iter(ev.linked_full_timestamps)) if len(ev.linked_full_timestamps) == 1 else None
        )
        if linked_full_ts and agreed_date_only:
            linked_date_part = linked_full_ts[:10].replace(":", "-")
            if linked_date_part != agreed_date_only:
                date_conflict_reasons.append(
                    "linked_full_timestamp_date_disagrees_with_filename_or_gallery_date"
                )

        if emb_date:
            if linked_full_ts:
                if emb_full_exif and emb_full_exif != linked_full_ts:
                    date_conflict_reasons.append("existing_embedded_timestamp_conflicts_with_linked_timestamp")
                elif not emb_full_exif and emb_date != linked_full_ts[:10].replace(":", "-"):
                    date_conflict_reasons.append("existing_embedded_date_conflicts_with_linked_timestamp")
            elif agreed_date_only and emb_date != agreed_date_only:
                date_conflict_reasons.append("existing_embedded_date_conflicts_with_date_only_source")

        loc_conflict_reasons: list[str] = []
        agreed_location: tuple[float, float] | None = None
        if ev.linked_locations:
            first_lat, first_lon = ev.linked_locations[0]
            for lat, lon in ev.linked_locations[1:]:
                if abs(lat - first_lat) > 1e-4 or abs(lon - first_lon) > 1e-4:
                    loc_conflict_reasons.append("multiple_disagreeing_linked_locations")
                    break
            if not loc_conflict_reasons:
                agreed_location = (first_lat, first_lon)
                if emb_gps is not None:
                    if abs(emb_gps[0] - first_lat) > 1e-4 or abs(emb_gps[1] - first_lon) > 1e-4:
                        loc_conflict_reasons.append("existing_embedded_gps_conflicts_with_linked_location")

        tag_updates: dict[str, Any] = {}
        actions_taken: list[str] = []

        if not date_conflict_reasons:
            if linked_full_ts:
                if emb_full_exif == linked_full_ts:
                    actions_taken.append("full_timestamp_already_present")
                else:
                    if ext in SUPPORTED_IMAGE_EXTENSIONS:
                        tag_updates["EXIF:DateTimeOriginal"] = linked_full_ts
                        tag_updates["EXIF:CreateDate"] = linked_full_ts
                        tag_updates["EXIF:ModifyDate"] = linked_full_ts
                        tag_updates["XMP:DateCreated"] = linked_full_ts[:10].replace(":", "-")
                    elif ext in SUPPORTED_VIDEO_EXTENSIONS:
                        tag_updates["QuickTime:CreateDate"] = linked_full_ts
                        tag_updates["QuickTime:ModifyDate"] = linked_full_ts
                        tag_updates["TrackCreateDate"] = linked_full_ts
                        tag_updates["MediaCreateDate"] = linked_full_ts
                    actions_taken.append("full_timestamp_written")
            elif agreed_date_only:
                if emb_time and emb_date == agreed_date_only:
                    actions_taken.append("preserved_existing_time")
                elif emb_date == agreed_date_only and not emb_time:
                    actions_taken.append("date_only_already_present")
                else:
                    if ext in SUPPORTED_IMAGE_EXTENSIONS:
                        tag_updates["XMP:DateCreated"] = agreed_date_only
                        tag_updates["IPTC:DateCreated"] = agreed_date_only.replace("-", ":")
                        actions_taken.append("date_only_written")
                    elif ext in SUPPORTED_VIDEO_EXTENSIONS:
                        actions_taken.append("date_only_skipped_video_requires_time")

        if not loc_conflict_reasons and agreed_location is not None:
            lat, lon = agreed_location
            if emb_gps is not None and abs(emb_gps[0] - lat) <= 1e-4 and abs(emb_gps[1] - lon) <= 1e-4:
                actions_taken.append("location_already_present")
            else:
                if ext in SUPPORTED_IMAGE_EXTENSIONS:
                    tag_updates["EXIF:GPSLatitude"] = abs(lat)
                    tag_updates["EXIF:GPSLatitudeRef"] = "N" if lat >= 0 else "S"
                    tag_updates["EXIF:GPSLongitude"] = abs(lon)
                    tag_updates["EXIF:GPSLongitudeRef"] = "E" if lon >= 0 else "W"
                elif ext in SUPPORTED_VIDEO_EXTENSIONS:
                    tag_updates["QuickTime:GPSCoordinates"] = f"{lat} {lon}"
                    tag_updates["GPSLatitude"] = lat
                    tag_updates["GPSLongitude"] = lon
                actions_taken.append("location_written")

        all_conflicts = date_conflict_reasons + loc_conflict_reasons
        if all_conflicts:
            conflict_files.append(
                {
                    "rel_path": rel_posix,
                    "conflicts": sorted(set(all_conflicts)),
                    "partial_actions": actions_taken,
                }
            )

        if tag_updates:
            if not dry_run:
                if exiftool.is_available():
                    targets = [work_root / rel_posix]
                    if ev.composite_rel_path:
                        targets.append(work_root / ev.composite_rel_path)
                    for t_path in targets:
                        if t_path.is_file():
                            pending_updates.append((t_path, tag_updates, rel_posix, actions_taken))
                else:
                    skipped_files.append(
                        {
                            "rel_path": rel_posix,
                            "reasons": ["exiftool_not_available_on_system"],
                        }
                    )
            else:
                changed_files.append(
                    {
                        "rel_path": rel_posix,
                        "composite_rel_path": ev.composite_rel_path,
                        "actions": actions_taken,
                        "evidence_sources": ev.evidence_sources,
                    }
                )
        elif not all_conflicts:
            skipped_files.append(
                {
                    "rel_path": rel_posix,
                    "reasons": actions_taken or ["no_applicable_metadata_changes"],
                }
            )

    if not dry_run and pending_updates:
        batch_items = [(t_path, tags) for t_path, tags, _, _ in pending_updates]
        write_errors = dict(exiftool.write_metadata_batch(batch_items, chunk_size=200))
        for t_path, tags, rel_posix, actions_taken in pending_updates:
            if t_path in write_errors:
                unsupported_files.append(
                    {
                        "rel_path": t_path.relative_to(work_root).as_posix(),
                        "reason": f"exiftool_write_error:{write_errors[t_path]}",
                    }
                )
            else:
                if not any(c["rel_path"] == rel_posix for c in changed_files):
                    changed_files.append(
                        {
                            "rel_path": rel_posix,
                            "composite_rel_path": asset_evidence[rel_posix].composite_rel_path,
                            "actions": actions_taken,
                            "evidence_sources": asset_evidence[rel_posix].evidence_sources,
                        }
                    )

    chat_report: dict[str, Any] = {
        "conversations_exported": 0,
        "total_messages_exported": 0,
        "total_markdown_files": 0,
        "linked_attachments": 0,
        "unlinked_attachments": 0,
        "index_path": None,
        "conversations": [],
    }
    if chat_json_data:
        chat_report = export_chats_to_markdown(
            chat_json_data=chat_json_data,
            output_root=work_root,
            unique_media_id_to_rel_path=unique_media_id_to_rel_path,
            ambiguous_media_ids=ambiguous_media_ids,
            chunk_size=chunk_size,
            dry_run=dry_run,
        )

    if use_staging and not dry_run:
        out_root.mkdir(parents=True, exist_ok=True)
        staged_files: list[pathlib.Path] = []
        for root, _, files in os.walk(work_root):
            for fname in files:
                staged_files.append(pathlib.Path(root) / fname)

        for sf in staged_files:
            rel = sf.relative_to(work_root)
            (out_root / rel).parent.mkdir(parents=True, exist_ok=True)

        def _transfer_worker(sf: pathlib.Path) -> None:
            rel = sf.relative_to(work_root)
            shutil.copy2(sf, out_root / rel)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_copy_workers) as executor:
            list(executor.map(_transfer_worker, staged_files))

        if temp_stage_dir:
            temp_stage_dir.cleanup()

    report: dict[str, Any] = {
        "dry_run": dry_run,
        "summary": {
            "total_source_files": len(all_rel_paths),
            "mirrored_files": len(mirrored_files),
            "omitted_overlays": len(omitted_overlays),
            "composites_created": len(composites_created),
            "failed_composites": len(failed_composites),
            "video_overlays_omitted": len(video_overlays_omitted),
            "memories_gallery_entries": len(gallery_entries),
            "memories_json_records": len(memories_json_records),
            "unlinked_memories_json_records": unlinked_memories_json_count,
            "memories_sequence_aligned": sequence_aligned_count,
            "zero_coordinates_skipped": zero_coord_skipped_count,
            "exiftool_available": exiftool.is_available(),
            "changed_files": len(changed_files),
            "skipped_files": len(skipped_files),
            "conflict_files": len(conflict_files),
            "ambiguous_files": len(ambiguous_files),
            "unsupported_files": len(unsupported_files),
            "chat_conversations_exported": chat_report["conversations_exported"],
            "chat_messages_exported": chat_report["total_messages_exported"],
            "chat_attachments_linked": chat_report["linked_attachments"],
        },
        "omitted_overlays": omitted_overlays,
        "composites": composites_created,
        "failed_composites": failed_composites,
        "video_overlays_omitted": video_overlays_omitted,
        "changed": changed_files,
        "skipped": skipped_files,
        "conflicts": conflict_files,
        "ambiguous": ambiguous_files,
        "unsupported": unsupported_files,
        "chats": chat_report,
    }

    logger.info(
        "Processed export (dry_run=%s): source_files=%d, mirrored=%d, omitted_overlays=%d, "
        "composites=%d, metadata_changed=%d, conflicts=%d, ambiguous=%d, unsupported=%d, "
        "chats_exported=%d",
        dry_run,
        report["summary"]["total_source_files"],
        report["summary"]["mirrored_files"],
        report["summary"]["omitted_overlays"],
        report["summary"]["composites_created"],
        report["summary"]["changed_files"],
        report["summary"]["conflict_files"],
        report["summary"]["ambiguous_files"],
        report["summary"]["unsupported_files"],
        report["summary"]["chat_conversations_exported"],
    )

    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safe, non-destructive Snapchat export processor for Memories and Chat Media."
    )
    parser.add_argument(
        "--source",
        "-s",
        required=True,
        type=pathlib.Path,
        help="Path to the Snapchat export source directory.",
    )
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        type=pathlib.Path,
        help="Path to the output directory (must be outside source tree and not already exist unless --overwrite).",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Analyze export and validate all rules without creating or modifying output files.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow writing to an existing output directory.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=500,
        help="Maximum number of messages per conversation Markdown file chunk (default: 500).",
    )
    parser.add_argument(
        "--exiftool-bin",
        default="exiftool",
        help="Path or binary name for ExifTool (default: exiftool).",
    )
    parser.add_argument(
        "--no-align-memories-sequence",
        action="store_true",
        help="Disable sequence alignment fallback for Memories when download links are empty.",
    )
    parser.add_argument(
        "--temp-staging",
        choices=["auto", "always", "never"],
        default="auto",
        help="Stage files in Linux /tmp directory during processing (default: auto; enabled for 9p/Windows mounts).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose status logging (never logs chat content or personal metadata).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        process_export(
            source_dir=args.source,
            output_dir=args.output,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
            chunk_size=args.chunk_size,
            exiftool_bin=args.exiftool_bin,
            align_memories_sequence=not args.no_align_memories_sequence,
            temp_staging=args.temp_staging,
            verbose=args.verbose,
        )
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
