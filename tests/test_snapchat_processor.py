#!/usr/bin/env python3
"""
Unit and integration tests for `snapchat_processor.py`.
Covers output safety guards, dry-run non-destructiveness, Memories HTML/JSON parsing,
evidence-backed metadata resolution, conflict & ambiguity policies, date-only time
preservation, Pillow overlay compositing, ExifTool image/video read-back, Markdown
chat export & chunking, privacy-safe logging, and local still-image classification.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from PIL import Image, ImageDraw
import numpy as np

# Ensure both workspace root and output deliverable directory are importable
THIS_DIR = pathlib.Path(__file__).resolve().parent
WORKSPACE_ROOT = THIS_DIR.parent
OUTPUT_ARTIFACTS_DIR = WORKSPACE_ROOT / "artifacts" / "file_generation" / "ttl=63d" / "output"
for cand_dir in (THIS_DIR, WORKSPACE_ROOT, OUTPUT_ARTIFACTS_DIR):
    if cand_dir.is_dir() and str(cand_dir) not in sys.path:
        sys.path.insert(0, str(cand_dir))

from unittest.mock import patch
import snapchat_processor as pse


def _hash_tree(root: pathlib.Path) -> dict[str, tuple[str, int]]:
    """Returns a map of relative path -> (sha256_hex, mtime_ns) for every file under root."""
    state: dict[str, tuple[str, int]] = {}
    for dirpath, _, filenames in os.walk(root):
        for fn in sorted(filenames):
            p = pathlib.Path(dirpath) / fn
            rel = p.relative_to(root).as_posix()
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            mtime_ns = p.stat().st_mtime_ns
            state[rel] = (digest, mtime_ns)
    return state


def _create_natural_photo_image(path: pathlib.Path, size: tuple[int, int] = (320, 240)) -> None:
    """Creates a synthetic photograph-like image with smooth 2D color gradients and organic texture."""
    w, h = size
    x = np.linspace(0, 1, w, dtype=np.float32)
    y = np.linspace(0, 1, h, dtype=np.float32)
    xv, yv = np.meshgrid(x, y)
    rng = np.random.default_rng(42)
    r = np.clip((xv * 180 + yv * 60 + rng.normal(0, 4, (h, w))), 15, 240).astype(np.uint8)
    g = np.clip(((1 - xv) * 150 + yv * 90 + rng.normal(0, 4, (h, w))), 15, 240).astype(np.uint8)
    b = np.clip((np.sin(xv * 3.14) * 140 + (1 - yv) * 100 + rng.normal(0, 4, (h, w))), 15, 240).astype(np.uint8)
    rgb = np.stack([r, g, b], axis=-1)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb, mode="RGB").save(path, format="JPEG", quality=95)


def _create_screenshot_meme_image(path: pathlib.Path, size: tuple[int, int] = (360, 640)) -> None:
    """Creates a synthetic meme/screenshot image with flat UI background and top/bottom high-contrast caption bars."""
    w, h = size
    img = Image.new("RGB", (w, h), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, int(h * 0.18), w, int(h * 0.82)], fill=(235, 235, 235))
    for row_y in range(12, int(h * 0.14), 14):
        for col_x in range(20, w - 30, 10):
            if (col_x // 10 + row_y) % 2 == 0:
                draw.rectangle([col_x, row_y, col_x + 5, row_y + 8], fill=(0, 0, 0))
    for row_y in range(int(h * 0.85), h - 12, 14):
        for col_x in range(20, w - 30, 10):
            if (col_x // 10 + row_y) % 2 == 0:
                draw.rectangle([col_x, row_y, col_x + 5, row_y + 8], fill=(0, 0, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")


def _create_ambiguous_image(path: pathlib.Path, size: tuple[int, int] = (200, 200)) -> None:
    """Creates a low-information mid-gray image that should land in the `uncertain` bucket."""
    w, h = size
    arr = np.full((h, w, 3), 128, dtype=np.uint8)
    arr[50:150, 50:150, 0] = 140
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr, mode="RGB").save(path, format="JPEG", quality=90)


def _create_disposable_mp4(path: pathlib.Path, comment: str = "DisposableVideoComment") -> None:
    """Creates a tiny valid MP4 video fixture using ffmpeg or existing sample MP4."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("ffmpeg"):
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=green:s=64x64:d=0.2",
                "-pix_fmt",
                "yuv420p",
                "-metadata",
                f"comment={comment}",
                str(path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    else:
        sample_mp4 = WORKSPACE_ROOT / "snapchat-data" / "memories" / "2018-07-16_901246df-41ca-8973-4578-630b3cd08c1a-main.mp4"
        if sample_mp4.is_file():
            shutil.copy2(sample_mp4, path)
            if shutil.which("exiftool"):
                cmd = [
                    "exiftool",
                    "-overwrite_original",
                    "-all=",
                    "-QuickTime:time:all=1904:01:01 00:00:00",
                    "-DateTimeOriginal=",
                ]
                if comment:
                    cmd.append(f"-Comment={comment}")
                cmd.append(str(path))
                subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
        else:
            raise FileNotFoundError("Neither ffmpeg nor sample MP4 available for fixture creation")


class TestSnapchatExportProcessor(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp_dir.name)
        self.src_dir = self.root / "export_src"
        self.out_dir = self.root / "export_out"
        self.src_dir.mkdir(parents=True, exist_ok=True)
        self.exiftool = pse.ExifToolAdapter()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_01_output_safety_guards_and_dry_run_immutability(self) -> None:
        """Verifies output-inside-source guard, existing-output guard, and dry-run source immutability."""
        mem_dir = self.src_dir / "memories"
        mem_dir.mkdir()
        img_path = mem_dir / "2023-05-01_11111111-2222-3333-4444-555555555555-main.jpg"
        _create_natural_photo_image(img_path)

        with self.assertRaises(ValueError):
            pse.validate_paths(self.src_dir, self.src_dir)

        inside_out = self.src_dir / "nested_output"
        with self.assertRaises(ValueError):
            pse.validate_paths(self.src_dir, inside_out)
        self.assertEqual(pse.main(["--source", str(self.src_dir), "--output", str(inside_out)]), 1)

        existing_out = self.root / "already_exists"
        existing_out.mkdir()
        with self.assertRaises(FileExistsError):
            pse.validate_paths(self.src_dir, existing_out, overwrite=False)
        self.assertEqual(pse.main(["--source", str(self.src_dir), "--output", str(existing_out)]), 1)

        # Output directory with source markers should be rejected even with overwrite=True
        source_like_out = self.root / "source_like_dir"
        source_like_out.mkdir()
        (source_like_out / "index.html").write_text("dummy", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            pse.validate_paths(self.src_dir, source_like_out, overwrite=True)
        self.assertIn("appears to be a source directory", str(ctx.exception))

        # Test CLI overwrite prompt logic
        with patch("builtins.input", return_value="n"):
            self.assertEqual(pse.main(["--source", str(self.src_dir), "--output", str(existing_out)]), 1)
        with patch("builtins.input", return_value="y"):
            self.assertEqual(pse.main(["--source", str(self.src_dir), "--output", str(existing_out), "--dry-run"]), 0)

        before_state = _hash_tree(self.src_dir)
        report = pse.process_export(self.src_dir, self.out_dir, dry_run=True)
        after_state = _hash_tree(self.src_dir)
        self.assertEqual(before_state, after_state)
        self.assertFalse(self.out_dir.exists())
        self.assertTrue(report["dry_run"])

    def test_02_overlay_compositing_and_gallery_html_update(self) -> None:
        """
        Verifies that matching Memories main+overlay pairs:
        - Preserve original `*-main.jpg` in output
        - Create `*-composite.jpg` via Pillow alpha compositing
        - Omit `*-overlay.png` from output
        - Update `memories.html` to reference `*-composite.jpg` and strip `<img class="overlay-image">`
        - Handle video+overlay by keeping `*-main.mp4`, omitting `*-overlay.png`, and removing broken overlay tag
        """
        mem_dir = self.src_dir / "memories"
        mem_dir.mkdir(parents=True, exist_ok=True)

        stem_img = "2021-05-01_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        main_img_path = mem_dir / f"{stem_img}-main.jpg"
        ov_img_path = mem_dir / f"{stem_img}-overlay.png"

        Image.new("RGB", (100, 100), color=(0, 0, 255)).save(main_img_path, format="JPEG", quality=100)
        ov_rgba = Image.new("RGBA", (100, 100), color=(0, 0, 0, 0))
        draw = ImageDraw.Draw(ov_rgba)
        draw.rectangle([0, 0, 100, 50], fill=(255, 0, 0, 255))
        ov_rgba.save(ov_img_path, format="PNG")

        stem_vid = "2021-05-02_12345678-1234-1234-1234-1234567890ab"
        main_vid_path = mem_dir / f"{stem_vid}-main.mp4"
        ov_vid_path = mem_dir / f"{stem_vid}-overlay.png"
        _create_disposable_mp4(main_vid_path)
        ov_rgba.save(ov_vid_path, format="PNG")

        html_content = f"""<!DOCTYPE html>
<html><body>
<div class="image-container">
    <img src=".//{stem_img}-main.jpg">
    <img src=".//{stem_img}-overlay.png" class="overlay-image">
    <div><div class="text-line">2021-05-01</div></div>
</div>
<div class="image-container">
    <video src=".//{stem_vid}-main.mp4"></video>
    <img src=".//{stem_vid}-overlay.png" class="overlay-image">
    <div><div class="text-line">2021-05-02</div></div>
</div>
</body></html>"""
        (mem_dir / "memories.html").write_text(html_content, encoding="utf-8")

        before_state = _hash_tree(self.src_dir)
        report = pse.process_export(self.src_dir, self.out_dir, dry_run=False)
        after_state = _hash_tree(self.src_dir)
        self.assertEqual(before_state, after_state, "Source tree must remain untouched")

        out_main_img = self.out_dir / "memories" / f"{stem_img}-main.jpg"
        out_comp_img = self.out_dir / "memories" / f"{stem_img}-composite.jpg"
        out_ov_img = self.out_dir / "memories" / f"{stem_img}-overlay.png"
        out_main_vid = self.out_dir / "memories" / f"{stem_vid}-main.mp4"
        out_ov_vid = self.out_dir / "memories" / f"{stem_vid}-overlay.png"

        self.assertTrue(out_main_img.is_file(), "Original main image must be preserved")
        self.assertTrue(out_comp_img.is_file(), "Composite image must be created")
        self.assertFalse(out_ov_img.exists(), "Raw image overlay must be omitted from output")
        self.assertTrue(out_main_vid.is_file(), "Original main video must be preserved")
        self.assertFalse(out_ov_vid.exists(), "Raw video overlay must be omitted from output")

        with Image.open(out_main_img) as im_main:
            px_main_top = im_main.convert("RGB").getpixel((50, 25))
            self.assertLess(px_main_top[0], 20)
            self.assertGreater(px_main_top[2], 230)

        with Image.open(out_comp_img) as im_comp:
            px_comp_top = im_comp.convert("RGB").getpixel((50, 25))
            px_comp_bot = im_comp.convert("RGB").getpixel((50, 75))
            self.assertGreater(px_comp_top[0], 230)
            self.assertLess(px_comp_top[2], 20)
            self.assertLess(px_comp_bot[0], 20)
            self.assertGreater(px_comp_bot[2], 230)

        self.assertFalse((self.out_dir / "memories" / "memories.html").exists(), "HTML files must not be copied or created in output")
        self.assertEqual(report["summary"]["composites_created"], 1)
        self.assertEqual(report["summary"]["omitted_overlays"], 2)

        # Verify ICC profile preservation and EXIF orientation normalization
        test_main = self.src_dir / "test_icc_main.jpg"
        test_ov = self.src_dir / "test_icc_ov.png"
        test_out = self.out_dir / "test_icc_comp.jpg"
        im_with_icc = Image.new("RGB", (80, 120), color=(10, 20, 30))
        fake_icc = b"FAKE_ICC_PROFILE_PAYLOAD_FOR_TESTING"
        fake_exif = im_with_icc.getexif()
        fake_exif[0x0112] = 6  # Orientation 6: 90 deg CW
        im_with_icc.save(test_main, format="JPEG", exif=fake_exif.tobytes(), icc_profile=fake_icc)
        Image.new("RGBA", (120, 80), color=(255, 0, 0, 128)).save(test_ov, format="PNG")

        pse.composite_image_pair(test_main, test_ov, test_out)
        self.assertTrue(test_out.is_file())
        with Image.open(test_out) as comp_res:
            self.assertEqual(comp_res.info.get("icc_profile"), fake_icc)
            self.assertEqual(comp_res.size, (120, 80))
            ex_out = comp_res.getexif()
            self.assertEqual(ex_out.get(0x0112), 1)

    def test_03_metadata_writes_date_only_preserve_time_conflicts_and_exiftool_readback(self) -> None:
        """
        Tests ExifTool metadata writes and read-back across images and videos:
        1. Linked full timestamp + location written to image and video when evidence agrees; unrelated metadata preserved.
        2. Date-only source without existing time writes `XMP:DateCreated` on images without inventing midnight in `DateTimeOriginal`.
        3. Date-only source on image WITH existing time preserves the existing time in `DateTimeOriginal`.
        4. Date-only source on video skips inventing midnight and reports it.
        5. Disagreement between filename/gallery date and linked JSON timestamp logs conflict without guessing.
        6. Unlinked `memories_history.json` record (same date, empty Download Link) is never joined by date alone.
        7. Unsupported video format (`.avi`) is copied and reported in `unsupported`.
        """
        mem_dir = self.src_dir / "memories"
        json_dir = self.src_dir / "json"
        mem_dir.mkdir(parents=True, exist_ok=True)
        json_dir.mkdir(parents=True, exist_ok=True)

        u1 = "11111111-1111-4111-8111-111111111111"
        img1 = mem_dir / f"2024-09-20_{u1}-main.jpg"
        _create_natural_photo_image(img1)
        self.exiftool.write_metadata(img1, {"EXIF:Artist": "PreserveMeArtist"})

        u2 = "22222222-2222-4222-8222-222222222222"
        vid2 = mem_dir / f"2024-11-19_{u2}-main.mp4"
        _create_disposable_mp4(vid2, comment="KeepThisVideoComment")

        u3 = "33333333-3333-4333-8333-333333333333"
        img3 = mem_dir / f"2023-05-01_{u3}-main.jpg"
        _create_natural_photo_image(img3)

        u4 = "44444444-4444-4444-8444-444444444444"
        img4 = mem_dir / f"2023-06-15_{u4}-main.jpg"
        _create_natural_photo_image(img4)
        self.exiftool.write_metadata(img4, {"EXIF:DateTimeOriginal": "2023:06:15 14:22:10"})

        u5 = "55555555-5555-4555-8555-555555555555"
        img5 = mem_dir / f"2024-08-04_{u5}-main.jpg"
        _create_natural_photo_image(img5)

        u6 = "66666666-6666-4666-8666-666666666666"
        avi6 = mem_dir / f"2022-01-01_{u6}-main.avi"
        avi6.write_bytes(b"RIFF\x00\x00\x00\x00AVI LIST")

        (mem_dir / "memories.html").write_text(
            f"""<!DOCTYPE html><html><body>
            <div class="image-container"><img src=".//2024-09-20_{u1}-main.jpg"><div><div class="text-line">2024-09-20</div></div></div>
            <div class="image-container"><video src=".//2024-11-19_{u2}-main.mp4"></video><div><div class="text-line">2024-11-19</div></div></div>
            <div class="image-container"><img src=".//2023-05-01_{u3}-main.jpg"><div><div class="text-line">2023-05-01</div></div></div>
            <div class="image-container"><img src=".//2023-06-15_{u4}-main.jpg"><div><div class="text-line">2023-06-15</div></div></div>
            <div class="image-container"><img src=".//2024-08-04_{u5}-main.jpg"><div><div class="text-line">2024-08-04</div></div></div>
            </body></html>""",
            encoding="utf-8",
        )

        memories_json = {
            "Saved Media": [
                {
                    "Date": "2024-09-20 15:49:22 UTC",
                    "Media Type": "Image",
                    "Location": "Latitude, Longitude: 42.34961, -71.07787",
                    "Download Link": f"https://example.snapchat.com/dmd/memories?id={u1}",
                    "Media Download Url": "",
                },
                {
                    "Date": "2024-11-19 17:41:03 UTC",
                    "Media Type": "Video",
                    "Location": "Latitude, Longitude: 42.38318, -71.0664",
                    "Download Link": f"memories/2024-11-19_{u2}-main.mp4",
                    "Media Download Url": "",
                },
                {
                    "Date": "2023-05-01 22:14:02 UTC",
                    "Media Type": "Image",
                    "Location": "Latitude, Longitude: 37.26951, -112.94805",
                    "Download Link": "",
                    "Media Download Url": "",
                },
                {
                    "Date": "2024-08-05 03:42:20 UTC",
                    "Media Type": "Image",
                    "Location": "Latitude, Longitude: 0.0, 0.0",
                    "Download Link": f"memories/2024-08-04_{u5}-main.jpg",
                    "Media Download Url": "",
                },
            ]
        }
        (json_dir / "memories_history.json").write_text(json.dumps(memories_json), encoding="utf-8")

        report = pse.process_export(self.src_dir, self.out_dir, dry_run=False)

        m1 = self.exiftool.read_metadata(self.out_dir / "memories" / img1.name)
        self.assertEqual(m1.get("DateTimeOriginal"), "2024:09:20 15:49:22")
        self.assertEqual(m1.get("Artist"), "PreserveMeArtist")
        self.assertAlmostEqual(float(m1["GPSLatitude"]), 42.34961, places=3)
        self.assertAlmostEqual(float(m1["GPSLongitude"]), -71.07787, places=3)

        m2 = self.exiftool.read_metadata(self.out_dir / "memories" / vid2.name)
        self.assertEqual(m2.get("CreateDate"), "2024:11:19 17:41:03")
        self.assertEqual(m2.get("Comment"), "KeepThisVideoComment")
        self.assertAlmostEqual(float(m2["GPSLatitude"]), 42.38318, places=3)
        self.assertAlmostEqual(float(m2["GPSLongitude"]), -71.0664, places=3)

        m3 = self.exiftool.read_metadata(self.out_dir / "memories" / img3.name)
        self.assertIn(m3.get("DateCreated"), ("2023-05-01", "2023:05:01"))
        self.assertIsNone(m3.get("DateTimeOriginal"))
        self.assertIsNone(m3.get("GPSLatitude"))

        m4 = self.exiftool.read_metadata(self.out_dir / "memories" / img4.name)
        self.assertEqual(m4.get("DateTimeOriginal"), "2023:06:15 14:22:10")

        m5 = self.exiftool.read_metadata(self.out_dir / "memories" / img5.name)
        self.assertIsNone(m5.get("DateTimeOriginal"))
        self.assertIsNone(m5.get("GPSLatitude"))
        conflict_paths = [c["rel_path"] for c in report["conflicts"]]
        self.assertIn(f"memories/{img5.name}", conflict_paths)

        self.assertTrue((self.out_dir / "memories" / avi6.name).is_file())
        unsupported_paths = [u["rel_path"] for u in report["unsupported"]]
        self.assertIn(f"memories/{avi6.name}", unsupported_paths)

    @unittest.skip("Chat markdown export deprecated; only media is retained in output")
    def test_04_chat_markdown_escaping_chunking_links_and_privacy_logs(self) -> None:
        """
        Verifies chat_history.json export to Markdown:
        - Chronological sorting and conversation grouping
        - Safe Markdown escaping of special characters
        - Configurable chunking across multiple files
        - Relative attachment links ONLY for unique exact Media ID matches
        - Ambiguous or unmatched Media IDs left unlinked
        - Console logs contain zero chat message content or opaque IDs
        """
        json_dir = self.src_dir / "json"
        chat_media_dir = self.src_dir / "chat_media"
        json_dir.mkdir(parents=True, exist_ok=True)
        chat_media_dir.mkdir(parents=True, exist_ok=True)

        unique_id = "b~UniqueMediaIdToken12345"
        ambig_id = "b~AmbiguousMediaIdToken999"
        unmatched_id = "b~MissingMediaIdToken000"

        _create_natural_photo_image(chat_media_dir / f"2024-08-16_{unique_id}.jpg")
        _create_natural_photo_image(chat_media_dir / f"2024-08-16_{ambig_id}_role1.jpg")
        _create_natural_photo_image(chat_media_dir / f"2024-08-16_{ambig_id}_role2.jpg")

        (chat_media_dir / "sidecar_valid.unknown").write_text(
            json.dumps({"media_id": unique_id, "Location": "Latitude, Longitude: 42.3601, -71.0942"}),
            encoding="utf-8",
        )
        (chat_media_dir / "sidecar_binary.unknown").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00corrupt")

        secret_phrase = "SUPER_SECRET_PRIVATE_CHAT_TEXT_DO_NOT_LOG_987654"
        chat_data = {
            "friend_alpha": [
                {
                    "From": "friend_alpha",
                    "Media Type": "TEXT",
                    "Created": "2024-08-16 14:35:00 UTC",
                    "Content": f"{secret_phrase} with *asterisks* and [brackets](http://example.com) & <html> | pipe",
                    "Conversation Title": "Alpha Squad",
                    "IsSender": False,
                    "Created(microseconds)": 1723818900000,
                    "IsSaved": True,
                    "Media IDs": "",
                },
                {
                    "From": "me_user",
                    "Media Type": "MEDIA",
                    "Created": "2024-08-16 14:30:23 UTC",
                    "Content": "# Heading injection attempt",
                    "Conversation Title": "Alpha Squad",
                    "IsSender": True,
                    "Created(microseconds)": 1723818623489,
                    "IsSaved": True,
                    "Media IDs": f"{unique_id} | {ambig_id} | {unmatched_id}",
                },
                {
                    "From": "friend_alpha",
                    "Media Type": "TEXT",
                    "Created": "",
                    "Content": None,
                    "Conversation Title": "Alpha Squad",
                    "IsSender": False,
                    "Created(microseconds)": None,
                    "IsSaved": False,
                    "Media IDs": "",
                },
            ]
        }
        (json_dir / "chat_history.json").write_text(json.dumps(chat_data), encoding="utf-8")

        log_capture = io.StringIO()
        logger = logging.getLogger("snapchat_processor")
        handler = logging.StreamHandler(log_capture)
        logger.addHandler(handler)
        try:
            report = pse.process_export(
                self.src_dir,
                self.out_dir,
                dry_run=False,
                chunk_size=2,
                verbose=True,
            )
        finally:
            logger.removeHandler(handler)

        logged_text = log_capture.getvalue()
        self.assertNotIn(secret_phrase, logged_text, "Chat message text must never appear in console logs")
        self.assertNotIn(unique_id, logged_text, "Opaque Media IDs must never appear in console logs")
        self.assertNotIn("42.3601", logged_text, "Coordinates must never appear in console logs")

        md_dir = self.out_dir / "chat_markdown"
        self.assertTrue((md_dir / "index.md").is_file())
        part1 = md_dir / "friend_alpha_part001.md"
        part2 = md_dir / "friend_alpha_part002.md"
        self.assertTrue(part1.is_file(), "Chunk part 1 must be created")
        self.assertTrue(part2.is_file(), "Chunk part 2 must be created")

        p1_text = part1.read_text(encoding="utf-8")

        idx_1430 = p1_text.find("2024-08-16 14:30:23 UTC")
        idx_1435 = p1_text.find("2024-08-16 14:35:00 UTC")
        self.assertNotEqual(idx_1430, -1)
        self.assertNotEqual(idx_1435, -1)
        self.assertLess(idx_1430, idx_1435, "Messages must be sorted chronologically ascending")

        self.assertIn("\\*asterisks\\*", p1_text)
        self.assertIn("\\[brackets\\]", p1_text)
        self.assertIn("\\<html\\>", p1_text)
        self.assertIn("\\# Heading injection attempt", p1_text)

        self.assertIn(f"../chat_media/2024-08-16_{urllib.parse.quote(unique_id)}.jpg", p1_text)
        self.assertIn("*(ambiguous media ID — unlinked)*", p1_text)
        self.assertIn("*(unmatched media ID — unlinked)*", p1_text)
        self.assertNotIn("role1", p1_text)

        chat_img_meta = self.exiftool.read_metadata(
            self.out_dir / "chat_media" / f"2024-08-16_{unique_id}.jpg"
        )
        self.assertEqual(chat_img_meta.get("DateTimeOriginal"), "2024:08:16 14:30:23")
        self.assertAlmostEqual(float(chat_img_meta["GPSLatitude"]), 42.3601, places=3)
        self.assertEqual(report["chats"]["linked_attachments"], 1)
        self.assertEqual(report["chats"]["unlinked_attachments"], 2)

    def test_05_no_html_or_json_in_output_only_media_and_transcripts(self) -> None:
        """
        Verifies that no HTML or JSON files are copied or created in the output directory,
        and that only media files and chat transcripts exist in output.
        """
        mem_dir = self.src_dir / "memories"
        json_dir = self.src_dir / "json"
        mem_dir.mkdir(parents=True, exist_ok=True)
        json_dir.mkdir(parents=True, exist_ok=True)

        photo_file = mem_dir / "2023-07-01_aaaaaaaa-1111-2222-3333-444444444441-main.jpg"
        meme_file = mem_dir / "2023-07-02_aaaaaaaa-1111-2222-3333-444444444442-main.png"
        uncertain_file = mem_dir / "2023-07-03_aaaaaaaa-1111-2222-3333-444444444443-main.jpg"
        video_file = mem_dir / "2023-07-04_aaaaaaaa-1111-2222-3333-444444444444-main.mp4"

        _create_natural_photo_image(photo_file)
        self.exiftool.write_metadata(photo_file, {"EXIF:Make": "Apple", "EXIF:Model": "iPhone 15"})
        _create_screenshot_meme_image(meme_file)
        _create_ambiguous_image(uncertain_file)
        _create_disposable_mp4(video_file)

        (mem_dir / "memories.html").write_text("<html><body>test</body></html>", encoding="utf-8")
        (json_dir / "memories_history.json").write_text("{}", encoding="utf-8")
        (self.src_dir / "account.json").write_text("{}", encoding="utf-8")

        pse.process_export(self.src_dir, self.out_dir, dry_run=False)

        out_exts = {p.suffix.lower() for p in self.out_dir.rglob("*") if p.is_file()}
        self.assertNotIn(".html", out_exts)
        self.assertNotIn(".json", out_exts)
        self.assertNotIn(".unknown", out_exts)
        self.assertTrue((self.out_dir / "memories" / photo_file.name).is_file())
        self.assertTrue((self.out_dir / "memories" / meme_file.name).is_file())
        self.assertTrue((self.out_dir / "memories" / video_file.name).is_file())
        self.assertFalse((self.out_dir / "classification").exists())

    def test_06_uploaded_reference_export_integration(self) -> None:
        """
        Runs dry-run and full export on the user's uploaded reference files to verify
        exact discovery counts (672 gallery entries, 209 overlays, 673 memories_history
        records, 89 conversations, 3009 messages).
        """
        uploaded_dir = WORKSPACE_ROOT / "user_uploaded_files" / "ttl=63d" / "user_uploaded"
        if not uploaded_dir.is_dir():
            self.skipTest("Uploaded reference directory not present")

        out_ref = self.root / "uploaded_ref_out"
        report = pse.process_export(uploaded_dir, out_ref, dry_run=False)

        self.assertEqual(report["summary"]["memories_gallery_entries"], 672)
        self.assertEqual(report["summary"]["memories_json_records"], 673)
        self.assertEqual(report["summary"]["unlinked_memories_json_records"], 673)
        self.assertEqual(report["summary"]["chat_conversations_exported"], 89)
        self.assertEqual(report["summary"]["chat_messages_exported"], 3009)
        self.assertTrue((out_ref / "chat_markdown" / "index.md").is_file())
        self.assertFalse((out_ref / "processing_report.json").exists())

    def test_07_corrupted_or_scof_overlay_handling(self) -> None:
        """
        Verifies that proprietary SCOF or corrupted overlay files do not crash
        either the dry-run or the real export, and are recorded cleanly in report.
        """
        mem_dir = self.src_dir / "memories"
        mem_dir.mkdir(parents=True, exist_ok=True)
        u = "11111111-2222-3333-4444-555555555555"
        main_img = mem_dir / f"2021-05-01_{u}-main.jpg"
        ov_scof = mem_dir / f"2021-05-01_{u}-overlay.png"

        Image.new("RGB", (50, 50), color=(10, 20, 30)).save(main_img, format="JPEG")
        # Write proprietary SCOF signature
        ov_scof.write_bytes(b"\x14\x00\x00\x00SCOF\x00\x00\x0a\x00\x10\x00\x04\x00\x08\x00\x0c\x00")

        html_content = f"""<!DOCTYPE html><html><body>
        <div class="image-container">
            <img src=".//2021-05-01_{u}-main.jpg">
            <img src=".//2021-05-01_{u}-overlay.png" class="overlay-image">
            <div><div class="text-line">2021-05-01</div></div>
        </div>
        </body></html>"""
        (mem_dir / "memories.html").write_text(html_content, encoding="utf-8")

        # Dry run should pass and detect 0 successful composites, 1 failed composite
        dry_report = pse.process_export(self.src_dir, self.out_dir, dry_run=True)
        self.assertEqual(dry_report["summary"]["composites_created"], 0)
        self.assertEqual(dry_report["summary"]["failed_composites"], 1)

        # Real run should succeed without throwing UnidentifiedImageError
        real_report = pse.process_export(self.src_dir, self.out_dir, dry_run=False, overwrite=True)
        self.assertEqual(real_report["summary"]["composites_created"], 0)
        self.assertEqual(real_report["summary"]["failed_composites"], 1)
        self.assertTrue((self.out_dir / "memories" / f"2021-05-01_{u}-main.jpg").is_file())
        self.assertFalse((self.out_dir / "memories" / f"2021-05-01_{u}-overlay.png").exists())
        self.assertFalse((self.out_dir / "memories" / f"2021-05-01_{u}-composite.jpg").exists())

    def test_08_temp_staging_execution(self) -> None:
        """Verifies process_export works correctly when temp_staging='always'."""
        mem_dir = self.src_dir / "memories"
        mem_dir.mkdir(parents=True, exist_ok=True)
        u = "99999999-8888-7777-6666-555555555555"
        img = mem_dir / f"2023-01-01_{u}-main.jpg"
        _create_natural_photo_image(img)
        (mem_dir / "memories.html").write_text(f'<div class="image-container"><img src=".//{img.name}"></div>', encoding="utf-8")

        report = pse.process_export(self.src_dir, self.out_dir, temp_staging="always")
        self.assertEqual(report["summary"]["mirrored_files"], 1)
        self.assertTrue((self.out_dir / "memories" / img.name).is_file())
        self.assertFalse((self.out_dir / "memories" / "memories.html").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
