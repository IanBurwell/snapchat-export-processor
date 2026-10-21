#!/usr/bin/env python3
"""
Snapchat Export Processor (`snapchat_processor.py`)

Non-destructive CLI tool that mirrors media assets (images and videos) from a Snapchat
data export into a separate output directory, applies strictly evidence-backed date/time
and location metadata updates via batch ExifTool, and composites matching Memories main+overlay
image pairs via Pillow (retaining original mains and omitting raw overlays).
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Callable
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
import time
from typing import Any, TextIO
import urllib.parse

from bs4 import BeautifulSoup
from PIL import Image, ImageOps


SUPPORTED_IMAGE_EXTENSIONS: set[str] = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif"}
SUPPORTED_VIDEO_EXTENSIONS: set[str] = {".mp4", ".mov", ".m4v"}
UNSUPPORTED_VIDEO_OR_MEDIA_EXTENSIONS: set[str] = {
    ".avi", ".mkv", ".webm", ".wmv", ".flv", ".3gp", ".gif", ".heic", ".heif", ".bmp",
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
TARGET_METADATA_READ_TAGS: list[str] = [
    "-DateTimeOriginal",
    "-CreateDate",
    "-DateCreated",
    "-XMP:DateCreated",
    "-TrackCreateDate",
    "-MediaCreateDate",
    "-GPSLatitude",
    "-GPSLongitude",
    "-GPSLatitudeRef",
    "-GPSLongitudeRef",
    "-GPSCoordinates",
]


@dataclass
class GalleryEntry:
    html_rel_path: str
    main_src: str | None
    overlay_src: str | None
    date_str: str | None
    main_rel_path: str | None = None
    overlay_rel_path: str | None = None


@dataclass
class MemoriesJsonRecord:
    index: int
    raw_date: str
    date_str: str | None
    full_timestamp_exif: str | None
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
        chunk_size: int = 100,
        tags: list[str] | None = None,
        max_workers: int | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> dict[str, dict[str, Any]]:
        if not self.resolved_bin or not file_paths:
            return {}

        def _read_chunk(chunk: list[pathlib.Path]) -> dict[str, dict[str, Any]]:
            cmd = [self.resolved_bin, "-j", "-n", "-fast"]
            if tags:
                cmd.extend(tags)
            cmd.extend(str(p) for p in chunk)
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            chunk_res: dict[str, dict[str, Any]] = {}
            if res.stdout.strip():
                try:
                    payload = json.loads(res.stdout)
                    if isinstance(payload, list):
                        for item in payload:
                            if src_file := item.get("SourceFile"):
                                chunk_res[pathlib.Path(src_file).resolve().as_posix()] = item
                except Exception:
                    pass
            for p in chunk:
                resolved_p = p.resolve().as_posix()
                if resolved_p not in chunk_res:
                    try:
                        chunk_res[resolved_p] = self.read_metadata(p)
                    except Exception:
                        pass
            if on_progress:
                on_progress(len(chunk))
            return chunk_res

        chunks = [file_paths[i : i + chunk_size] for i in range(0, len(file_paths), chunk_size)]
        workers = max_workers or min(8, os.cpu_count() or 4)
        result: dict[str, dict[str, Any]] = {}
        if len(chunks) <= 1 or workers <= 1:
            for c in chunks:
                result.update(_read_chunk(c))
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                for chunk_res in executor.map(_read_chunk, chunks):
                    result.update(chunk_res)
        return result

    def write_metadata_batch(
        self,
        updates: list[tuple[pathlib.Path, dict[str, Any]]],
        chunk_size: int = 200,
        on_progress: Callable[[int], None] | None = None,
    ) -> list[tuple[pathlib.Path, str]]:
        if not updates or not self.resolved_bin:
            return []
        failed: list[tuple[pathlib.Path, str]] = []
        for i in range(0, len(updates), chunk_size):
            chunk = updates[i : i + chunk_size]
            lines: list[str] = []
            for path, tags in chunk:
                lines.append("-overwrite_original")
                lines.extend(f"-{k}={v}" for k, v in tags.items())
                lines.extend((str(path), "-execute"))

            res = subprocess.run(
                [self.resolved_bin, "-@", "-"],
                input="\n".join(lines),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                err_map = {
                    parts[1].strip(): parts[0].strip()
                    for line in res.stderr.splitlines()
                    if "Error:" in line and " - " in line and (parts := line.split(" - ", 1))
                }
                fallback_err = res.stderr.strip() or "ExifTool batch write error"
                for path, _ in chunk:
                    sp = str(path)
                    if sp in err_map:
                        failed.append((path, err_map[sp]))
                    elif not err_map:
                        failed.append((path, fallback_err))
            if on_progress:
                on_progress(len(chunk))
        return failed

    def write_metadata(self, file_path: pathlib.Path, tags: dict[str, Any]) -> None:
        errors = self.write_metadata_batch([(file_path, tags)])
        if errors:
            raise ExifToolError(f"ExifTool write failed for {file_path}: {errors[0][1]}")


def is_slow_or_9p_fs(path: pathlib.Path) -> bool:
    try:
        resolved = str(path.resolve())
        mounts_file = pathlib.Path("/proc/mounts")
        if mounts_file.is_file():
            mounts = [
                (parts[1], parts[2].lower())
                for line in mounts_file.read_text(encoding="utf-8", errors="ignore").splitlines()
                if len(parts := line.split()) >= 3
            ]
            mounts.sort(key=lambda m: len(m[0]), reverse=True)
            for mp, fstype in mounts:
                if resolved == mp or resolved.startswith(mp.rstrip("/") + "/"):
                    return fstype in ("9p", "drvfs", "cifs", "smbfs", "nfs")
    except Exception:
        pass
    return False


class ProgressTracker:
    def __init__(
        self,
        total_phases: int = 6,
        enabled: bool = True,
        stream: TextIO | None = None,
    ) -> None:
        self.total_phases = total_phases
        self.enabled = enabled
        self.stream = stream or sys.stderr
        self.is_tty = self.enabled and hasattr(self.stream, "isatty") and self.stream.isatty()
        self.phase_records: list[dict[str, Any]] = []
        self._phase_index: int = 0
        self._phase_name: str = ""
        self._total: int = 0
        self._count: int = 0
        self._unit: str = "items"
        self._start_time: float = 0.0
        self._last_refresh: float = 0.0
        self._last_line_len: int = 0

    def start_phase(self, name: str, total: int = 0, unit: str = "files") -> None:
        self._phase_index += 1
        self._phase_name = name
        self._total = max(0, total)
        self._count = 0
        self._unit = unit
        self._start_time = time.perf_counter()
        self._last_refresh = 0.0
        self._render(force=True)

    def advance(self, amount: int = 1) -> None:
        self._count += amount
        now = time.perf_counter()
        if now - self._last_refresh >= 0.08 or (self._total > 0 and self._count >= self._total):
            self._render()

    def make_callback(self) -> Callable[[int], None]:
        return self.advance

    def finish_phase(self, details: str = "") -> None:
        elapsed = time.perf_counter() - self._start_time
        rate = (self._count / elapsed) if elapsed > 0.001 else 0.0
        self.phase_records.append({
            "name": self._phase_name,
            "duration": elapsed,
            "count": self._count,
            "total": self._total,
            "unit": self._unit,
            "rate": rate,
            "details": details,
        })
        if not self.enabled:
            return

        rate_str = f" ({rate:.1f} {self._unit}/s)" if self._count > 0 and elapsed > 0.05 else ""
        count_str = f"{self._count:,} {self._unit}" if self._total > 0 else (details or "done")
        line = f"✔ [{self._phase_index}/{self.total_phases}] {self._phase_name}: {count_str} in {elapsed:.2f}s{rate_str}"
        if self.is_tty:
            pad = max(0, self._last_line_len - len(line))
            self.stream.write(f"\r{line}{' ' * pad}\n")
        else:
            self.stream.write(f"{line}\n")
        self.stream.flush()
        self._last_line_len = 0

    def _render(self, force: bool = False) -> None:
        if not self.enabled or not self.is_tty:
            return
        now = time.perf_counter()
        self._last_refresh = now
        elapsed = now - self._start_time
        pct = (self._count / self._total * 100.0) if self._total > 0 else 0.0
        rate = (self._count / elapsed) if elapsed > 0.001 else 0.0
        rate_str = f"{rate:.1f} {self._unit}/s" if elapsed > 0.2 else f"-- {self._unit}/s"

        term_w = 80
        try:
            term_w = os.get_terminal_size().columns
        except Exception:
            pass

        prefix = f"[{self._phase_index}/{self.total_phases}] {self._phase_name}: "
        suffix = (
            f" {pct:5.1f}% ({self._count:,}/{self._total:,} {self._unit}) [{elapsed:.1f}s, {rate_str}]"
            if self._total > 0
            else f" ({self._count:,} {self._unit}) [{elapsed:.1f}s]"
        )
        avail = max(10, min(30, term_w - len(prefix) - len(suffix) - 3))

        if self._total > 0:
            filled = int(avail * min(1.0, self._count / self._total))
            bar_chars = "=" * filled
            if filled < avail:
                bar_chars += ">" + " " * (avail - filled - 1)
            bar = f"[{bar_chars}]"
            line = f"\r{prefix}{bar}{suffix}"
        else:
            line = f"\r{prefix}{suffix}"

        pad = max(0, self._last_line_len - len(line))
        self.stream.write(f"{line}{' ' * pad}")
        self.stream.flush()
        self._last_line_len = len(line)

    def print_summary(self) -> dict[str, Any]:
        total_time = sum(p["duration"] for p in self.phase_records)
        active_phases = [p for p in self.phase_records if p["duration"] > 0.0001 or p["count"] > 0]
        slowest = max(active_phases, key=lambda p: p["duration"]) if active_phases else None

        timing_data: dict[str, Any] = {
            "total_seconds": round(total_time, 2),
            "slowest_phase": slowest["name"] if slowest else None,
            "slowest_duration_seconds": round(slowest["duration"], 2) if slowest else 0.0,
            "phases": [
                {
                    "name": p["name"],
                    "duration_seconds": round(p["duration"], 2),
                    "percentage": round((p["duration"] / total_time * 100), 1) if total_time > 0 else 0.0,
                    "count": p["count"],
                    "unit": p["unit"],
                    "rate": round(p["rate"], 1),
                }
                for p in sorted(self.phase_records, key=lambda x: x["duration"], reverse=True)
            ],
        }

        if not self.enabled:
            return timing_data

        lines = [
            "",
            "=" * 78,
            "Execution Time Breakdown:",
            "-" * 78,
            f"{'Phase':<32} {'Time':>8} {'% Total':>9}   {'Throughput / Details':<25}",
            "-" * 78,
        ]
        for p in self.phase_records:
            pct = (p["duration"] / total_time * 100) if total_time > 0 else 0.0
            is_slowest = (slowest and p["name"] == slowest["name"] and len(active_phases) > 1 and p["duration"] > 0.005)
            slowest_tag = " [SLOWEST]" if is_slowest else ""
            if p["count"] > 0 and p["duration"] > 0.05:
                detail = f"{p['count']:,} {p['unit']} ({p['rate']:.1f}/s){slowest_tag}"
            elif p["details"]:
                detail = f"{p['details']}{slowest_tag}"
            elif p["count"] > 0:
                detail = f"{p['count']:,} {p['unit']}{slowest_tag}"
            else:
                detail = slowest_tag.strip()
            lines.append(f"{p['name']:<32} {p['duration']:>7.2f}s {pct:>8.1f}%   {detail:<25}")

        lines.extend([
            "-" * 78,
            f"{'Total Time':<32} {total_time:>7.2f}s {'100.0%':>9}",
        ])
        if slowest and slowest["duration"] > 0.005:
            pct_slowest = (slowest["duration"] / total_time * 100) if total_time > 0 else 0.0
            lines.append(f"Slowest Phase: {slowest['name']} ({slowest['duration']:.2f}s, {pct_slowest:.1f}% of total)")
        lines.extend(["=" * 78, ""])

        self.stream.write("\n".join(lines) + "\n")
        self.stream.flush()

        return timing_data


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
    if not source_dir.is_dir():
        raise ValueError(f"Source directory does not exist or is not a directory: {source_dir}")

    src_resolved = source_dir.resolve()
    out_resolved = output_dir.resolve()

    if out_resolved == src_resolved:
        raise ValueError("Refusing output path: output directory cannot be the same as source directory.")
    if out_resolved.is_relative_to(src_resolved):
        raise ValueError("Refusing output path: output directory cannot be inside the source tree.")
    if src_resolved.is_relative_to(out_resolved):
        raise ValueError("Refusing output path: source directory cannot be inside the output directory.")

    if out_resolved.is_dir():
        source_markers = ("index.html", "html", "json", "memories.html", "chat_history.json", "memories_history.json")
        found = [m for m in source_markers if (out_resolved / m).exists()]
        if found:
            raise ValueError(
                f"Refusing output path: destination '{output_dir}' appears to be a source directory "
                f"(contains: {', '.join(found)}). Output must be a separate destination."
            )

    if out_resolved.exists() and not overwrite:
        raise FileExistsError(
            f"Refusing to overwrite existing output path '{output_dir}'."
        )

    return src_resolved, out_resolved


def parse_timestamp_utc(raw_str: str | None) -> datetime | None:
    if not raw_str or not isinstance(raw_str, str) or not (s := raw_str.strip()):
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S UTC",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y:%m:%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        if s.endswith(" UTC"):
            s = s[:-4] + "+00:00"
        elif s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_date_only(raw_str: str | None) -> str | None:
    if not raw_str or not isinstance(raw_str, str):
        return None
    m = re.match(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})$", raw_str.strip())
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _validate_coords(lat: Any, lon: Any) -> tuple[float | None, float | None, bool]:
    try:
        f_lat, f_lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None, False
    if not (-90.0 <= f_lat <= 90.0 and -180.0 <= f_lon <= 180.0):
        return None, None, False
    if abs(f_lat) < 1e-9 and abs(f_lon) < 1e-9:
        return 0.0, 0.0, True
    return f_lat, f_lon, False


def parse_location_string(loc_str: str | None) -> tuple[float | None, float | None, bool]:
    if not loc_str or not isinstance(loc_str, str) or not (s := loc_str.strip()):
        return None, None, False
    m = LOCATION_RE.search(s) or re.match(
        r"^\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*$", s
    )
    return _validate_coords(m.group(1), m.group(2)) if m else (None, None, False)


def is_raw_overlay_path(rel_path_str: str, explicit_overlay_paths: set[str]) -> bool:
    norm = pathlib.PurePosixPath(rel_path_str).as_posix()
    if norm in explicit_overlay_paths:
        return True
    name = pathlib.PurePosixPath(norm).name
    if (m := MEMORIES_FILENAME_RE.match(name)) and m.group("role").lower() == "overlay":
        return True
    return bool(re.search(r"-overlay\.[A-Za-z0-9]+$", name, re.IGNORECASE))


def _memories_html_sort_key(path: pathlib.Path) -> tuple[int, str]:
    m = re.search(r"memories(\d+)?\.html$", path.name, re.IGNORECASE)
    return (int(m.group(1)) if (m and m.group(1)) else (1 if m else 9999), path.name)


def resolve_gallery_src(
    source_root: pathlib.Path,
    html_file: pathlib.Path,
    raw_src: str | None,
    all_rel_paths: set[str],
) -> str | None:
    if not raw_src or not (cleaned := re.sub(r"^(?:\./+)+", "", raw_src.strip())):
        return None

    html_rel_dir = html_file.parent.relative_to(source_root).as_posix()
    candidates = [f"{html_rel_dir}/{cleaned}"] if (html_rel_dir and html_rel_dir != ".") else []
    candidates.extend((f"memories/{cleaned}", cleaned))

    for cand in candidates:
        if (norm := pathlib.PurePosixPath(cand).as_posix()) in all_rel_paths:
            return norm

    fallback = (
        candidates[0]
        if (html_rel_dir and html_rel_dir != ".") or (source_root / "memories").is_dir()
        else cleaned
    )
    return pathlib.PurePosixPath(fallback).as_posix()


def parse_memories_galleries(
    source_root: pathlib.Path,
    all_rel_paths: set[str],
) -> list[GalleryEntry]:
    html_candidates: list[pathlib.Path] = []
    for sub in ("memories", ".", "html"):
        base = source_root / sub
        if base.is_dir():
            matching = [
                p for p in base.glob("memories*.html")
                if p.is_file() and p.name != "memories_history.html"
            ]
            for p in sorted(matching, key=_memories_html_sort_key):
                if p not in html_candidates:
                    html_candidates.append(p)

    entries: list[GalleryEntry] = []
    for html_file in html_candidates:
        rel_html = html_file.relative_to(source_root).as_posix()
        soup = BeautifulSoup(html_file.read_text(encoding="utf-8", errors="replace"), "html.parser")
        for container in soup.find_all("div", class_="image-container"):
            video_tag = container.find("video")
            main_img_tag = overlay_img_tag = None
            for im in container.find_all("img"):
                classes = im.get("class") or []
                if isinstance(classes, str):
                    classes = classes.split()
                if "overlay-image" in classes:
                    overlay_img_tag = im
                elif main_img_tag is None:
                    main_img_tag = im

            main_src = (
                video_tag.get("src")
                if video_tag and video_tag.get("src")
                else (main_img_tag.get("src") if main_img_tag and main_img_tag.get("src") else None)
            )
            overlay_src = overlay_img_tag.get("src") if overlay_img_tag else None
            text_line = container.find("div", class_="text-line")
            entries.append(
                GalleryEntry(
                    html_rel_path=rel_html,
                    main_src=main_src,
                    overlay_src=overlay_src,
                    date_str=parse_date_only(text_line.get_text(strip=True)) if text_line else None,
                    main_rel_path=resolve_gallery_src(source_root, html_file, main_src, all_rel_paths),
                    overlay_rel_path=resolve_gallery_src(source_root, html_file, overlay_src, all_rel_paths),
                )
            )
    return entries


def parse_memories_history_json(source_root: pathlib.Path) -> list[MemoriesJsonRecord]:
    candidates = (
        source_root / "json" / "memories_history.json",
        source_root / "memories_history.json",
    )
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
        lat, lon, is_zero = parse_location_string(item.get("Location"))
        records.append(
            MemoriesJsonRecord(
                index=idx,
                raw_date=raw_date,
                date_str=dt.strftime("%Y-%m-%d") if dt else parse_date_only(raw_date),
                full_timestamp_exif=dt.strftime("%Y:%m:%d %H:%M:%S") if dt else None,
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


def _first_str(data: dict[str, Any], *keys: str) -> str | None:
    for k in keys:
        if (val := data.get(k)) is not None and (s := str(val).strip()):
            return s
    return None


def parse_unknown_sidecar(source_root: pathlib.Path, rel_path: str) -> SidecarRecord:
    try:
        data = json.loads((source_root / rel_path).read_text(encoding="utf-8"))
    except Exception as exc:
        return SidecarRecord(rel_path=rel_path, is_valid_json=False, error=str(exc))

    if not isinstance(data, dict):
        return SidecarRecord(rel_path=rel_path, is_valid_json=False, error="JSON root is not an object")

    raw_date = _first_str(data, "Date", "date", "Created", "created", "timestamp")
    dt = parse_timestamp_utc(raw_date) if raw_date else None
    date_str = dt.strftime("%Y-%m-%d") if dt else (parse_date_only(raw_date) if raw_date else None)

    lat: float | None = None
    lon: float | None = None
    loc_val = data.get("Location") if "Location" in data else data.get("location")
    if isinstance(loc_val, str):
        p_lat, p_lon, is_z = parse_location_string(loc_val)
    elif isinstance(loc_val, dict):
        raw_lat = loc_val.get("latitude") if "latitude" in loc_val else loc_val.get("lat")
        raw_lon = (
            loc_val.get("longitude")
            if "longitude" in loc_val
            else (loc_val.get("lon") if "lon" in loc_val else loc_val.get("lng"))
        )
        p_lat, p_lon, is_z = _validate_coords(raw_lat, raw_lon)
    elif "latitude" in data and "longitude" in data:
        p_lat, p_lon, is_z = _validate_coords(data["latitude"], data["longitude"])
    else:
        p_lat, p_lon, is_z = None, None, False

    if not is_z:
        lat, lon = p_lat, p_lon

    return SidecarRecord(
        rel_path=rel_path,
        is_valid_json=True,
        media_id=_first_str(data, "Media ID", "media_id", "id", "mediaId"),
        target_file=_first_str(data, "file", "filename", "path", "local_path", "File"),
        date_str=date_str,
        full_timestamp_exif=dt.strftime("%Y:%m:%d %H:%M:%S") if dt else None,
        latitude=lat,
        longitude=lon,
    )


def extract_chat_media_identifiers(rel_path: str) -> tuple[str | None, set[str]]:
    p = pathlib.PurePosixPath(rel_path)
    name, stem = p.name, p.stem
    filename_date: str | None = None
    ids: set[str] = {stem}

    if m := LEADING_DATE_FILENAME_RE.match(name):
        filename_date = parse_date_only(m.group("date"))
        rest = m.group("rest")
        ids.add(rest)
        if "~" in rest:
            ids.add(rest.split("~", 1)[1])

    ids.update(B_TOKEN_RE.findall(stem))
    ids.update(re.findall(r"(b~[A-Za-z0-9\-.~]+)", stem))
    for uuid_match in UUID_RE.findall(name):
        ids.add(uuid_match.lower())
        ids.add(uuid_match.upper())

    return filename_date, ids


def align_memories_sequence_to_assets(
    gallery_entries: list[GalleryEntry],
    json_records: list[MemoriesJsonRecord],
    already_matched_assets: set[str],
) -> dict[str, MemoriesJsonRecord]:
    target_entries = [
        e for e in gallery_entries
        if e.main_rel_path and e.main_rel_path not in already_matched_assets and e.date_str
    ]
    if not target_entries or not json_records:
        return {}

    gal_dates = [e.date_str for e in target_entries if e.date_str]
    if any(gal_dates[i] > gal_dates[i + 1] for i in range(len(gal_dates) - 1)):
        return {}

    valid_json_dates = [r.date_str for r in json_records if r.date_str]
    json_seq = (
        list(reversed(json_records))
        if len(valid_json_dates) >= 2 and valid_json_dates[0] > valid_json_dates[-1]
        else list(json_records)
    )

    matched: dict[str, MemoriesJsonRecord] = {}
    j_idx = 0
    for entry in target_entries:
        main_rel, g_date = entry.main_rel_path, entry.date_str
        if not main_rel or not g_date:
            continue
        g_is_vid = pathlib.PurePosixPath(main_rel).suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS

        while j_idx < len(json_seq) and json_seq[j_idx].date_str and json_seq[j_idx].date_str < g_date:
            j_idx += 1

        if j_idx >= len(json_seq) or json_seq[j_idx].date_str != g_date:
            break

        if (json_seq[j_idx].media_type.lower() == "video") == g_is_vid:
            matched[main_rel] = json_seq[j_idx]
            j_idx += 1
        else:
            lookahead = next(
                (
                    k for k in range(j_idx + 1, len(json_seq))
                    if json_seq[k].date_str == g_date
                    and (json_seq[k].media_type.lower() == "video") == g_is_vid
                ),
                None,
            )
            if lookahead is None:
                break
            matched[main_rel] = json_seq.pop(lookahead)

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
            main_img = ImageOps.exif_transpose(raw_main) or raw_main

            orig_exif_bytes = raw_main.info.get("exif")
            exif_obj = raw_main.getexif()
            if main_img is not raw_main and exif_obj:
                exif_obj[0x0112] = 1
                orig_exif_bytes = exif_obj.tobytes()
            elif not orig_exif_bytes and exif_obj:
                orig_exif_bytes = exif_obj.tobytes()

            base_rgba = main_img.convert("RGBA")

        with Image.open(overlay_path) as raw_ov:
            ov_img = ImageOps.exif_transpose(raw_ov) or raw_ov
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
            composited.convert("RGB").save(
                composite_out_path, format="JPEG", quality=95, **save_kwargs
            )
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
    for key in (
        "DateTimeOriginal", "CreateDate", "DateCreated",
        "XMP:DateCreated", "TrackCreateDate", "MediaCreateDate",
    ):
        val = meta.get(key)
        if not val or not isinstance(val, str) or (s := val.strip()).startswith("0000"):
            continue
        if m_full := re.match(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[T ](\d{2}:\d{2}:\d{2})", s):
            d_str = f"{m_full.group(1)}-{m_full.group(2)}-{m_full.group(3)}"
            t_str = m_full.group(4)
            return d_str, t_str, f"{m_full.group(1)}:{m_full.group(2)}:{m_full.group(3)} {t_str}"
        if m_date := re.match(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})$", s):
            return f"{m_date.group(1)}-{m_date.group(2)}-{m_date.group(3)}", None, None
    return None, None, None


def _extract_embedded_gps(meta: dict[str, Any]) -> tuple[float, float] | None:
    if "GPSLatitude" in meta and "GPSLongitude" in meta:
        try:
            lat = float(meta["GPSLatitude"])
            lon = float(meta["GPSLongitude"])
            if str(meta.get("GPSLatitudeRef") or "").upper() == "S" and lat > 0:
                lat = -lat
            if str(meta.get("GPSLongitudeRef") or "").upper() == "W" and lon > 0:
                lon = -lon
            return (lat, lon)
        except Exception:
            pass
    if "GPSCoordinates" in meta:
        parts = [p for p in re.split(r"[, ]+", str(meta["GPSCoordinates"]).strip()) if p]
        if len(parts) >= 2:
            try:
                return (float(parts[0]), float(parts[1]))
            except Exception:
                pass
    return None


def _copy_rel_files(
    src_root: pathlib.Path,
    dst_root: pathlib.Path,
    rel_paths: list[str],
    max_workers: int,
    copy_stat: bool = False,
    on_progress: Callable[[int], None] | None = None,
) -> None:
    if not rel_paths:
        return
    dst_root.mkdir(parents=True, exist_ok=True)
    for parent in {(dst_root / rel).parent for rel in rel_paths}:
        parent.mkdir(parents=True, exist_ok=True)
    copy_fn = shutil.copy2 if copy_stat else shutil.copyfile
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(copy_fn, src_root / rel, dst_root / rel) for rel in rel_paths]
        for f in concurrent.futures.as_completed(futures):
            f.result()
            if on_progress:
                on_progress(1)


def save_debug_output(
    debug_dir: pathlib.Path,
    src_root: pathlib.Path,
    work_root: pathlib.Path,
    exiftool: ExifToolAdapter,
    unsupported_files: list[dict[str, Any]],
    conflict_files: list[dict[str, Any]],
    ambiguous_files: list[dict[str, Any]],
    failed_composites: list[dict[str, Any]],
    video_overlays_omitted: list[dict[str, Any]],
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    debug_dir.mkdir(parents=True, exist_ok=True)
    unsupported_dir = debug_dir / "unsupported"
    conflicts_dir = debug_dir / "conflicts"
    ambiguous_dir = debug_dir / "ambiguous"
    failed_comp_dir = debug_dir / "failed_composites"

    exif_updates: list[tuple[pathlib.Path, dict[str, Any]]] = []

    for item in unsupported_files:
        rel_p = item["rel_path"]
        reason = item.get("reason", "unknown")
        src_cand = src_root / rel_p
        if not src_cand.is_file():
            src_cand = work_root / rel_p
        dst = unsupported_dir / rel_p
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src_cand.is_file():
            try:
                shutil.copy2(src_cand, dst)
            except OSError:
                pass
        note_path = dst.parent / f"{dst.name}.debug.txt"
        note_path.write_text(f"Unsupported File: {rel_p}\nReason: {reason}\n", encoding="utf-8")
        if dst.is_file():
            comment = f"[Snapchat Debug] Unsupported: {reason}"
            exif_updates.append((dst, {"EXIF:UserComment": comment, "XMP-dc:Description": comment}))

    for item in conflict_files:
        rel_p = item["rel_path"]
        confs = item.get("conflicts", [])
        partials = item.get("partial_actions", [])
        det = item.get("details", {})
        src_cand = work_root / rel_p
        if not src_cand.is_file():
            src_cand = src_root / rel_p
        dst = conflicts_dir / rel_p
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src_cand.is_file():
            try:
                shutil.copy2(src_cand, dst)
            except OSError:
                pass
        note_lines = [
            f"Conflict File: {rel_p}",
            "Conflicts:",
            *(f"  - {c}" for c in confs),
            f"Partial Actions: {', '.join(partials) if partials else 'None'}",
            "Evidence Details:",
            f"  Filename Date: {det.get('filename_date')}",
            f"  Gallery Dates: {sorted(det.get('gallery_dates') or [])}",
            f"  Linked Full Timestamps: {sorted(det.get('linked_full_timestamps') or [])}",
            f"  Embedded EXIF Date: {det.get('embedded_date')}",
            f"  Embedded EXIF Time: {det.get('embedded_time')}",
            f"  Embedded EXIF Full: {det.get('embedded_full_exif')}",
            f"  Embedded GPS: {det.get('embedded_gps')}",
            f"  Linked Locations: {det.get('linked_locations') or []}",
        ]
        note_path = dst.parent / f"{dst.name}.debug.txt"
        note_path.write_text("\n".join(note_lines) + "\n", encoding="utf-8")
        if dst.is_file():
            conf_str = "; ".join(confs)
            comment = f"[Snapchat Debug] Conflicts: {conf_str}"
            exif_updates.append((dst, {"EXIF:UserComment": comment, "XMP-dc:Description": comment}))

    for item in ambiguous_files:
        rel_p = item["rel_path"]
        reasons = item.get("reasons", [])
        src_cand = work_root / rel_p
        if not src_cand.is_file():
            src_cand = src_root / rel_p
        dst = ambiguous_dir / rel_p
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src_cand.is_file():
            try:
                shutil.copy2(src_cand, dst)
            except OSError:
                pass
        note_lines = [
            f"Ambiguous Match File: {rel_p}",
            "Reasons:",
            *(f"  - {r}" for r in reasons),
        ]
        note_path = dst.parent / f"{dst.name}.debug.txt"
        note_path.write_text("\n".join(note_lines) + "\n", encoding="utf-8")
        if dst.is_file():
            comment = f"[Snapchat Debug] Ambiguous: {'; '.join(reasons)}"
            exif_updates.append((dst, {"EXIF:UserComment": comment, "XMP-dc:Description": comment}))

    for item in failed_composites:
        m_rel = item.get("main_rel_path", "")
        o_rel = item.get("overlay_rel_path", "")
        reason = item.get("reason", "unknown")
        pair_dir = failed_comp_dir / pathlib.PurePosixPath(m_rel).stem
        pair_dir.mkdir(parents=True, exist_ok=True)
        for cand_rel in (m_rel, o_rel):
            if cand_rel:
                src_f = src_root / cand_rel
                if src_f.is_file():
                    try:
                        shutil.copy2(src_f, pair_dir / src_f.name)
                    except OSError:
                        pass
        (pair_dir / "failure_info.txt").write_text(
            f"Main: {m_rel}\nOverlay: {o_rel}\nReason: {reason}\n", encoding="utf-8"
        )

    if exiftool.is_available() and exif_updates:
        try:
            exiftool.write_metadata_batch(exif_updates)
        except Exception as exc:
            if logger:
                logger.debug("Failed to write metadata notes to some debug files: %s", exc)

    summary_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "unsupported_count": len(unsupported_files),
        "conflict_count": len(conflict_files),
        "ambiguous_count": len(ambiguous_files),
        "failed_composites_count": len(failed_composites),
        "video_overlays_omitted_count": len(video_overlays_omitted),
        "unsupported_files": unsupported_files,
        "conflict_files": conflict_files,
        "ambiguous_files": ambiguous_files,
        "failed_composites": failed_composites,
        "video_overlays_omitted": video_overlays_omitted,
    }
    (debug_dir / "debug_report.json").write_text(json.dumps(summary_data, indent=2), encoding="utf-8")

    log_lines = [
        "=" * 70,
        f"Snapchat Export Debug Report - {summary_data['generated_at']}",
        "=" * 70,
        f"Unsupported Files:       {len(unsupported_files)}",
        f"Files With Conflicts:    {len(conflict_files)}",
        f"Ambiguous Match Files:   {len(ambiguous_files)}",
        f"Failed Image Composites: {len(failed_composites)}",
        f"Omitted Video Overlays:  {len(video_overlays_omitted)}",
        "-" * 70,
    ]
    if conflict_files:
        log_lines.append("Conflicts Breakdown:")
        for cf in conflict_files:
            log_lines.append(f"  • {cf['rel_path']}: {', '.join(cf.get('conflicts', []))}")
    if unsupported_files:
        log_lines.append("Unsupported Files Breakdown:")
        for uf in unsupported_files:
            log_lines.append(f"  • {uf['rel_path']}: {uf.get('reason', '')}")
    if ambiguous_files:
        log_lines.append("Ambiguous Matches Breakdown:")
        for af in ambiguous_files:
            log_lines.append(f"  • {af['rel_path']}: {', '.join(af.get('reasons', []))}")
    if failed_composites:
        log_lines.append("Failed Composites Breakdown:")
        for fc in failed_composites:
            log_lines.append(f"  • {fc.get('main_rel_path')}: {fc.get('reason', '')}")
    log_lines.append("=" * 70)

    (debug_dir / "debug_log.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return summary_data


def process_export(
    source_dir: pathlib.Path = pathlib.Path("snapchat-data"),
    output_dir: pathlib.Path = pathlib.Path("snapchat-processed"),
    dry_run: bool = False,
    overwrite: bool = False,
    exiftool_bin: str = "exiftool",
    align_memories_sequence: bool = True,
    temp_staging: str = "auto",
    verbose: bool = False,
    show_progress: bool = True,
    debug: bool = False,
    debug_dir: pathlib.Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    logger = setup_logger(verbose=verbose)
    src_root, out_root = validate_paths(source_dir, output_dir, overwrite=overwrite)
    exiftool = ExifToolAdapter(exiftool_bin=exiftool_bin)

    if not exiftool.is_available():
        logger.warning(
            "ExifTool binary '%s' not found on PATH. Media copying and compositing "
            "will proceed, but EXIF/QuickTime tag writes will be skipped.",
            exiftool_bin,
        )

    use_staging = not dry_run and (
        temp_staging == "always"
        or (temp_staging == "auto" and (is_slow_or_9p_fs(out_root) or is_slow_or_9p_fs(src_root)))
    )
    temp_stage_dir = (
        tempfile.TemporaryDirectory(prefix="snapchat_proc_", dir="/tmp") if use_staging else None
    )
    work_root = pathlib.Path(temp_stage_dir.name) if temp_stage_dir else out_root
    max_copy_workers = min(16, (os.cpu_count() or 4) * 2)

    total_phases = 6 if (use_staging and not dry_run) else 5
    progress = ProgressTracker(total_phases=total_phases, enabled=show_progress)

    progress.start_phase("Scanning & discovery", total=0, unit="files")

    all_rel_paths: set[str] = {
        (pathlib.Path(root) / fname).relative_to(src_root).as_posix()
        for root, _, files in os.walk(src_root)
        for fname in sorted(files)
    }

    gallery_entries = parse_memories_galleries(src_root, all_rel_paths)
    memories_json_records = parse_memories_history_json(src_root)

    chat_candidates = (src_root / "json" / "chat_history.json", src_root / "chat_history.json")
    chat_json_path = next((p for p in chat_candidates if p.is_file()), None)
    chat_json_data: dict[str, Any] = (
        json.loads(chat_json_path.read_text(encoding="utf-8")) if chat_json_path else {}
    )

    explicit_overlay_paths: set[str] = {
        e.overlay_rel_path for e in gallery_entries if e.overlay_rel_path
    }
    omitted_overlays: list[str] = []
    mirrored_files: list[str] = []
    memories_mains_by_stem: dict[str, str] = {}
    memories_overlays_by_stem: dict[str, str] = {}
    uuid_to_memories_main: dict[str, list[str]] = defaultdict(list)
    sidecar_records: list[SidecarRecord] = []
    unsupported_files: list[dict[str, str]] = []

    for rel_posix in sorted(all_rel_paths):
        p = pathlib.PurePosixPath(rel_posix)
        ext = p.suffix.lower()
        if ext in ALL_MEDIA_EXTENSIONS:
            if is_raw_overlay_path(rel_posix, explicit_overlay_paths):
                omitted_overlays.append(rel_posix)
            else:
                mirrored_files.append(rel_posix)
        elif ext == ".unknown":
            sc = parse_unknown_sidecar(src_root, rel_posix)
            sidecar_records.append(sc)
            if not sc.is_valid_json:
                unsupported_files.append({
                    "rel_path": rel_posix,
                    "reason": f"unreadable_unknown_sidecar:{sc.error}",
                })

        if m := MEMORIES_FILENAME_RE.match(p.name):
            stem_key = f"{p.parent.as_posix()}/{m.group('date')}_{m.group('uuid').lower()}"
            role = m.group("role").lower()
            if role == "main":
                memories_mains_by_stem[stem_key] = rel_posix
                uuid_to_memories_main[m.group("uuid").lower()].append(rel_posix)
            elif role == "overlay":
                memories_overlays_by_stem[stem_key] = rel_posix

    progress.finish_phase(details=f"{len(all_rel_paths):,} files found")

    progress.start_phase("Copying media", total=len(mirrored_files), unit="files")
    if not dry_run and mirrored_files:
        _copy_rel_files(
            src_root, work_root, mirrored_files, max_copy_workers, on_progress=progress.make_callback()
        )
    progress.finish_phase()

    main_to_overlay: dict[str, str] = {}
    for entry in gallery_entries:
        if entry.main_rel_path and entry.overlay_rel_path:
            m_main = MEMORIES_FILENAME_RE.match(pathlib.PurePosixPath(entry.main_rel_path).name)
            m_ov = MEMORIES_FILENAME_RE.match(pathlib.PurePosixPath(entry.overlay_rel_path).name)
            if not (m_main and m_ov) or (
                m_main.group("date") == m_ov.group("date")
                and m_main.group("uuid").lower() == m_ov.group("uuid").lower()
            ):
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
        if main_p.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS:
            comp_name = re.sub(r"-main(\.[^.]+)$", r"-composite\1", main_p.name, flags=re.IGNORECASE)
            if comp_name == main_p.name:
                comp_name = f"{main_p.stem}-composite{main_p.suffix}"
            comp_rel = (main_p.parent / comp_name).as_posix() if main_p.parent.as_posix() != "." else comp_name
            composite_tasks.append((main_rel, ov_rel, comp_rel))
        else:
            video_overlays_omitted.append({
                "main_rel_path": main_rel,
                "overlay_rel_path": ov_rel,
                "reason": "video_or_non_still_overlay_omitted_without_image_composite",
            })

    def _comp_worker(task: tuple[str, str, str]) -> tuple[str, str, str, bool, str | None]:
        m_rel, o_rel, c_rel = task
        try:
            if dry_run:
                with Image.open(src_root / m_rel), Image.open(src_root / o_rel):
                    pass
            else:
                composite_image_pair(src_root / m_rel, src_root / o_rel, work_root / c_rel)
            return m_rel, o_rel, c_rel, True, None
        except Exception as exc:
            return m_rel, o_rel, c_rel, False, str(exc)

    progress.start_phase("Compositing overlays", total=len(composite_tasks), unit="images")
    comp_results: list[tuple[str, str, str, bool, str | None]] = []
    if dry_run:
        for t in composite_tasks:
            comp_results.append(_comp_worker(t))
            progress.advance(1)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, os.cpu_count() or 4)) as executor:
            futures = [executor.submit(_comp_worker, t) for t in composite_tasks]
            for f in concurrent.futures.as_completed(futures):
                comp_results.append(f.result())
                progress.advance(1)
    progress.finish_phase()

    err_prefix = "unreadable_image_pair" if dry_run else "composite_error"
    for m_rel, o_rel, c_rel, success, err in comp_results:
        if success:
            composites_created.append({
                "main_rel_path": m_rel,
                "overlay_rel_path": o_rel,
                "composite_rel_path": c_rel,
            })
            main_to_composite_rel[m_rel] = c_rel
        else:
            if not dry_run:
                logger.warning("Skipping overlay compositing for %s onto %s: %s", o_rel, m_rel, err)
            failed_composites.append({
                "main_rel_path": m_rel,
                "overlay_rel_path": o_rel,
                "reason": f"{err_prefix}:{err}",
            })

    asset_evidence: dict[str, AssetEvidence] = {}
    chat_id_to_files: dict[str, list[str]] = defaultdict(list)
    stem_to_files: dict[str, list[str]] = defaultdict(list)

    for rel_posix in mirrored_files:
        p = pathlib.PurePosixPath(rel_posix)
        top_folder = p.parts[0].lower() if len(p.parts) > 1 else ""
        m_mem = MEMORIES_FILENAME_RE.match(p.name)
        if top_folder == "memories" or m_mem:
            cat = "memories"
            m_date = m_mem or LEADING_DATE_FILENAME_RE.match(p.name)
            f_date = parse_date_only(m_date.group("date")) if m_date else None
            comp_rel = main_to_composite_rel.get(rel_posix)
        else:
            cat = "chat_media" if top_folder == "chat_media" else "other"
            f_date, cand_ids = extract_chat_media_identifiers(rel_posix)
            comp_rel = None
            for cid in cand_ids:
                chat_id_to_files[cid].append(rel_posix)
            stem_to_files[p.stem].append(rel_posix)

        asset_evidence[rel_posix] = AssetEvidence(
            rel_path=rel_posix,
            category=cat,
            filename_date=f_date,
            evidence_sources=["filename_date"] if f_date else [],
            composite_rel_path=comp_rel,
        )

    gallery_main_paths: set[str] = set()
    for entry in gallery_entries:
        if entry.main_rel_path:
            gallery_main_paths.add(entry.main_rel_path)
            if entry.main_rel_path in asset_evidence and entry.date_str:
                ev = asset_evidence[entry.main_rel_path]
                ev.gallery_dates.add(entry.date_str)
                if "gallery_html_date" not in ev.evidence_sources:
                    ev.evidence_sources.append("gallery_html_date")

    zero_coord_skipped_count = 0
    matched_json_by_asset: dict[str, list[MemoriesJsonRecord]] = defaultdict(list)
    asset_by_filename: dict[str, list[str]] = defaultdict(list)
    for rel_p in asset_evidence:
        asset_by_filename[pathlib.PurePosixPath(rel_p).name].append(rel_p)

    for rec in memories_json_records:
        if rec.is_zero_coord:
            zero_coord_skipped_count += 1
        non_empty_blobs = [b for b in (rec.download_link, rec.media_download_url, rec.local_path_hint) if b]
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
                    for rel_p in uuid_to_memories_main.get(uuid_tok.lower(), ()):
                        if rel_p in asset_evidence:
                            matched_assets.add(rel_p)
            if not matched_assets:
                for fname, rel_list in asset_by_filename.items():
                    if fname in decoded_blob:
                        matched_assets.update(rel_list)

        if len(matched_assets) == 1:
            matched_json_by_asset[next(iter(matched_assets))].append(rec)
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
        if target_rel in gallery_main_paths:
            ev.evidence_sources.append("memories_history_json")

    media_id_to_msg_timestamps: dict[str, set[str]] = defaultdict(set)
    all_chat_media_ids: set[str] = set()
    for raw_msgs in chat_json_data.values():
        if not isinstance(raw_msgs, list):
            continue
        for msg in raw_msgs:
            if not isinstance(msg, dict) or not (raw_ids := str(msg.get("Media IDs") or "").strip()):
                continue
            dt = parse_timestamp_utc(str(msg.get("Created") or ""))
            exif_ts = dt.strftime("%Y:%m:%d %H:%M:%S") if dt else None
            for tok in (t.strip() for t in raw_ids.split("|") if t.strip()):
                all_chat_media_ids.add(tok)
                if exif_ts:
                    media_id_to_msg_timestamps[tok].add(exif_ts)

    for mid in sorted(all_chat_media_ids):
        matching_files = sorted(set(chat_id_to_files.get(mid, ())))
        msg_timestamps = media_id_to_msg_timestamps.get(mid, set())
        if len(matching_files) == 1:
            target_rel = matching_files[0]
            if len(msg_timestamps) > 1:
                asset_evidence[target_rel].conflict_reasons.append(
                    "multiple_chat_messages_with_disagreeing_timestamps_for_media_id"
                )
            elif len(msg_timestamps) == 1:
                asset_evidence[target_rel].linked_full_timestamps.add(next(iter(msg_timestamps)))
                asset_evidence[target_rel].evidence_sources.append("chat_history_json")
        elif len(matching_files) > 1:
            for target_rel in matching_files:
                asset_evidence[target_rel].ambiguous_reasons.append(
                    "multiple_chat_media_files_match_same_media_id"
                )

    for sc in sidecar_records:
        if not sc.is_valid_json:
            continue
        matched_targets: set[str] = set()
        if sc.target_file:
            matched_targets.update(
                rel_p for rel_p in asset_evidence
                if rel_p == sc.target_file or pathlib.PurePosixPath(rel_p).name == sc.target_file
            )
        if not matched_targets and sc.media_id:
            matched_targets.update(chat_id_to_files.get(sc.media_id, ()))
        if not matched_targets:
            sc_stem = pathlib.PurePosixPath(sc.rel_path).stem
            base_stem = pathlib.PurePosixPath(sc_stem).stem if "." in sc_stem else sc_stem
            for cand_stem in {sc_stem, base_stem}:
                matched_targets.update(stem_to_files.get(cand_stem, ()))

        if len(matched_targets) == 1:
            ev = asset_evidence[next(iter(matched_targets))]
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

    read_base = src_root if dry_run else work_root
    files_to_read = (
        [read_base / rel_pos for rel_pos in asset_evidence if (read_base / rel_pos).is_file()]
        if exiftool.is_available()
        else []
    )
    progress.start_phase("Reading metadata", total=len(files_to_read), unit="files")
    if files_to_read:
        existing_meta_cache = exiftool.read_metadata_batch(
            files_to_read,
            tags=TARGET_METADATA_READ_TAGS,
            on_progress=progress.make_callback(),
        )
    progress.finish_phase()

    pending_updates: list[tuple[pathlib.Path, dict[str, Any], str, list[str]]] = []

    for rel_posix, ev in sorted(asset_evidence.items()):
        ext = pathlib.PurePosixPath(rel_posix).suffix.lower()
        if ext in UNSUPPORTED_VIDEO_OR_MEDIA_EXTENSIONS:
            unsupported_files.append({"rel_path": rel_posix, "reason": f"unsupported_media_format:{ext}"})
            continue

        if ev.ambiguous_reasons:
            ambiguous_files.append({"rel_path": rel_posix, "reasons": sorted(set(ev.ambiguous_reasons))})
            continue

        target_file_for_read = read_base / rel_posix
        resolved_key = target_file_for_read.resolve().as_posix()
        existing_meta = existing_meta_cache.get(resolved_key)
        if existing_meta is None and exiftool.is_available() and target_file_for_read.is_file():
            try:
                existing_meta = exiftool.read_metadata(target_file_for_read)
                existing_meta_cache[resolved_key] = existing_meta
            except ExifToolError as exc:
                unsupported_files.append({"rel_path": rel_posix, "reason": f"exiftool_read_error:{exc}"})
                continue
        existing_meta = existing_meta or {}

        emb_date, emb_time, emb_full_exif = _extract_embedded_date_and_time(existing_meta)
        emb_gps = _extract_embedded_gps(existing_meta)

        date_only_candidates = set(ev.gallery_dates)
        if ev.filename_date:
            date_only_candidates.add(ev.filename_date)

        date_conflict_reasons: list[str] = list(ev.conflict_reasons)
        if len(date_only_candidates) > 1:
            date_conflict_reasons.append("disagreement_between_filename_and_gallery_date")

        agreed_date_only = next(iter(date_only_candidates)) if len(date_only_candidates) == 1 else None

        if len(ev.linked_full_timestamps) > 1:
            date_conflict_reasons.append("multiple_disagreeing_linked_full_timestamps")

        linked_full_ts = (
            next(iter(ev.linked_full_timestamps)) if len(ev.linked_full_timestamps) == 1 else None
        )
        if linked_full_ts and agreed_date_only and linked_full_ts[:10].replace(":", "-") != agreed_date_only:
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
            if any(
                abs(lat - first_lat) > 1e-4 or abs(lon - first_lon) > 1e-4
                for lat, lon in ev.linked_locations[1:]
            ):
                loc_conflict_reasons.append("multiple_disagreeing_linked_locations")
            else:
                agreed_location = (first_lat, first_lon)
                if emb_gps is not None and (
                    abs(emb_gps[0] - first_lat) > 1e-4 or abs(emb_gps[1] - first_lon) > 1e-4
                ):
                    loc_conflict_reasons.append("existing_embedded_gps_conflicts_with_linked_location")

        tag_updates: dict[str, Any] = {}
        actions_taken: list[str] = []

        if not date_conflict_reasons:
            if linked_full_ts:
                if emb_full_exif == linked_full_ts:
                    actions_taken.append("full_timestamp_already_present")
                else:
                    if ext in SUPPORTED_IMAGE_EXTENSIONS:
                        tag_updates.update({
                            "EXIF:DateTimeOriginal": linked_full_ts,
                            "EXIF:CreateDate": linked_full_ts,
                            "EXIF:ModifyDate": linked_full_ts,
                            "XMP:DateCreated": linked_full_ts[:10].replace(":", "-"),
                        })
                    elif ext in SUPPORTED_VIDEO_EXTENSIONS:
                        tag_updates.update({
                            "QuickTime:CreateDate": linked_full_ts,
                            "QuickTime:ModifyDate": linked_full_ts,
                            "TrackCreateDate": linked_full_ts,
                            "MediaCreateDate": linked_full_ts,
                        })
                    actions_taken.append("full_timestamp_written")
            elif agreed_date_only:
                if emb_date == agreed_date_only:
                    actions_taken.append("preserved_existing_time" if emb_time else "date_only_already_present")
                elif ext in SUPPORTED_IMAGE_EXTENSIONS:
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
                    tag_updates.update({
                        "EXIF:GPSLatitude": abs(lat),
                        "EXIF:GPSLatitudeRef": "N" if lat >= 0 else "S",
                        "EXIF:GPSLongitude": abs(lon),
                        "EXIF:GPSLongitudeRef": "E" if lon >= 0 else "W",
                    })
                elif ext in SUPPORTED_VIDEO_EXTENSIONS:
                    tag_updates.update({
                        "QuickTime:GPSCoordinates": f"{lat} {lon}",
                        "GPSLatitude": lat,
                        "GPSLongitude": lon,
                    })
                actions_taken.append("location_written")

        all_conflicts = date_conflict_reasons + loc_conflict_reasons
        if all_conflicts:
            conflict_files.append({
                "rel_path": rel_posix,
                "conflicts": sorted(set(all_conflicts)),
                "partial_actions": actions_taken,
                "details": {
                    "filename_date": ev.filename_date,
                    "gallery_dates": sorted(ev.gallery_dates),
                    "linked_full_timestamps": sorted(ev.linked_full_timestamps),
                    "embedded_date": emb_date,
                    "embedded_time": emb_time,
                    "embedded_full_exif": emb_full_exif,
                    "embedded_gps": emb_gps,
                    "linked_locations": ev.linked_locations,
                },
            })

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
                    skipped_files.append({"rel_path": rel_posix, "reasons": ["exiftool_not_available_on_system"]})
            else:
                changed_files.append({
                    "rel_path": rel_posix,
                    "composite_rel_path": ev.composite_rel_path,
                    "actions": actions_taken,
                    "evidence_sources": ev.evidence_sources,
                })
        elif not all_conflicts:
            skipped_files.append({
                "rel_path": rel_posix,
                "reasons": actions_taken or ["no_applicable_metadata_changes"],
            })

    progress.start_phase("Writing metadata", total=len(pending_updates), unit="files")
    if not dry_run and pending_updates:
        batch_items = [(t_path, tags) for t_path, tags, _, _ in pending_updates]
        write_errors = dict(
            exiftool.write_metadata_batch(
                batch_items, chunk_size=200, on_progress=progress.make_callback()
            )
        )
        seen_changed: set[str] = set()
        for t_path, _, rel_posix, actions_taken in pending_updates:
            if t_path in write_errors:
                unsupported_files.append({
                    "rel_path": t_path.relative_to(work_root).as_posix(),
                    "reason": f"exiftool_write_error:{write_errors[t_path]}",
                })
            elif rel_posix not in seen_changed:
                seen_changed.add(rel_posix)
                changed_files.append({
                    "rel_path": rel_posix,
                    "composite_rel_path": asset_evidence[rel_posix].composite_rel_path,
                    "actions": actions_taken,
                    "evidence_sources": asset_evidence[rel_posix].evidence_sources,
                })
    progress.finish_phase()

    if use_staging and not dry_run:
        staged_rel_paths = [
            (pathlib.Path(root) / fname).relative_to(work_root).as_posix()
            for root, _, files in os.walk(work_root)
            for fname in files
        ]
        progress.start_phase("Transferring from staging", total=len(staged_rel_paths), unit="files")
        _copy_rel_files(
            work_root, out_root, staged_rel_paths, max_copy_workers, on_progress=progress.make_callback()
        )
        progress.finish_phase()
        if temp_stage_dir:
            temp_stage_dir.cleanup()

    timings = progress.print_summary()

    debug_output_data: dict[str, Any] | None = None
    if debug or debug_dir:
        effective_debug_dir = (debug_dir or (out_root / "debug")).resolve()
        logger.info("Saving debug diagnostic output to %s...", effective_debug_dir)
        debug_output_data = save_debug_output(
            debug_dir=effective_debug_dir,
            src_root=src_root,
            work_root=work_root if not dry_run else src_root,
            exiftool=exiftool,
            unsupported_files=unsupported_files,
            conflict_files=conflict_files,
            ambiguous_files=ambiguous_files,
            failed_composites=failed_composites,
            video_overlays_omitted=video_overlays_omitted,
            logger=logger,
        )

    report: dict[str, Any] = {
        "dry_run": dry_run,
        "timings": timings,
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
    }

    if debug_output_data is not None:
        report["debug"] = debug_output_data
        report["summary"]["debug_dir"] = str((debug_dir or (out_root / "debug")).resolve())

    logger.info(
        "Processed export (dry_run=%s): source_files=%d, mirrored=%d, omitted_overlays=%d, "
        "composites=%d, metadata_changed=%d, conflicts=%d, ambiguous=%d, unsupported=%d",
        dry_run,
        report["summary"]["total_source_files"],
        report["summary"]["mirrored_files"],
        report["summary"]["omitted_overlays"],
        report["summary"]["composites_created"],
        report["summary"]["changed_files"],
        report["summary"]["conflict_files"],
        report["summary"]["ambiguous_files"],
        report["summary"]["unsupported_files"],
    )

    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safe, non-destructive Snapchat export processor for Memories and Chat Media."
    )
    parser.add_argument(
        "--source", "-s", default=pathlib.Path("snapchat-data"), type=pathlib.Path,
        help="Path to the Snapchat export source directory (default: snapchat-data).",
    )
    parser.add_argument(
        "--output", "-o", default=pathlib.Path("snapchat-processed"), type=pathlib.Path,
        help="Path to the output directory (default: snapchat-processed).",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true",
        help="Analyze export and validate all rules without creating or modifying output files.",
    )
    parser.add_argument(
        "--exiftool-bin", default="exiftool",
        help="Path or binary name for ExifTool (default: exiftool).",
    )
    parser.add_argument(
        "--no-align-memories-sequence", action="store_true",
        help="Disable sequence alignment fallback for Memories when download links are empty.",
    )
    parser.add_argument(
        "--temp-staging", choices=["auto", "always", "never"], default="auto",
        help="Stage files in Linux /tmp directory during processing (default: auto; enabled for 9p/Windows mounts).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose status logging (never logs chat content or personal metadata).",
    )
    parser.add_argument(
        "--no-progress", action="store_true",
        help="Disable interactive progress bar and execution time breakdown output.",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Save diagnostic information, problematic files (unsupported, conflicts, ambiguous), logs, and metadata notes to <output>/debug.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    out_path = args.output
    overwrite = False
    if out_path.exists():
        try:
            reply = input(f"Output directory '{out_path}' already exists. Overwrite? [y/N]: ").strip().lower()
            if reply not in ("y", "yes"):
                print("Aborted. Output directory was not modified.", file=sys.stderr)
                return 1
            overwrite = True
        except (EOFError, KeyboardInterrupt, OSError):
            print("\nAborted.", file=sys.stderr)
            return 1

    try:
        process_export(
            source_dir=args.source,
            output_dir=args.output,
            dry_run=args.dry_run,
            overwrite=overwrite,
            exiftool_bin=args.exiftool_bin,
            align_memories_sequence=not args.no_align_memories_sequence,
            temp_staging=args.temp_staging,
            verbose=args.verbose,
            show_progress=not args.no_progress,
            debug=args.debug,
        )
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())