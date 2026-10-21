import json
import os
import pathlib
import shutil
import tempfile
import threading
import urllib.request
import urllib.parse
import unittest
from PIL import Image

import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import snapchat_processor as pse
import review_gui as rog


class TestGuiAndDebug(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp_dir.name)
        self.src_dir = self.root / "export_src"
        self.out_dir = self.root / "export_out"
        self.debug_dir = self.root / "export_debug"
        self.src_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_sample_image(self, path: pathlib.Path, color: tuple[int, int, int] = (100, 150, 200)) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (64, 64), color=color)
        img.save(path)

    def test_pair_review_manager_choices_and_deletions(self) -> None:
        mem_dir = self.out_dir / "memories"
        mem_dir.mkdir(parents=True, exist_ok=True)

        m1 = mem_dir / "2023-01-01_11111111-1111-1111-1111-111111111111-main.jpg"
        c1 = mem_dir / "2023-01-01_11111111-1111-1111-1111-111111111111-composite.jpg"
        self._create_sample_image(m1)
        self._create_sample_image(c1)

        m2 = mem_dir / "2023-01-02_22222222-2222-2222-2222-222222222222-main.jpg"
        c2 = mem_dir / "2023-01-02_22222222-2222-2222-2222-222222222222-composite.jpg"
        self._create_sample_image(m2)
        self._create_sample_image(c2)

        m3 = mem_dir / "2023-01-03_33333333-3333-3333-3333-333333333333-main.jpg"
        c3 = mem_dir / "2023-01-03_33333333-3333-3333-3333-333333333333-composite.jpg"
        self._create_sample_image(m3)
        self._create_sample_image(c3)

        pairs = [
            {"main_rel_path": m1.relative_to(self.out_dir).as_posix(), "composite_rel_path": c1.relative_to(self.out_dir).as_posix()},
            {"main_rel_path": m2.relative_to(self.out_dir).as_posix(), "composite_rel_path": c2.relative_to(self.out_dir).as_posix()},
            {"main_rel_path": m3.relative_to(self.out_dir).as_posix(), "composite_rel_path": c3.relative_to(self.out_dir).as_posix()},
        ]

        mgr = rog.PairReviewManager(self.out_dir, pairs)
        self.assertEqual(len(mgr.items), 3)

        mgr.set_choice(0, "with_overlay")
        mgr.set_choice(1, "without_overlay")
        mgr.set_choice(2, "both")

        res = mgr.apply_deletions()
        self.assertEqual(res["total_pairs"], 3)
        self.assertEqual(res["kept_with_overlay"], 1)
        self.assertEqual(res["kept_without_overlay"], 1)
        self.assertEqual(res["kept_both"], 1)

        self.assertFalse(m1.exists(), "Pair 1 main should be deleted")
        self.assertTrue(c1.exists(), "Pair 1 composite should be kept")

        self.assertTrue(m2.exists(), "Pair 2 main should be kept")
        self.assertFalse(c2.exists(), "Pair 2 composite should be deleted")

        self.assertTrue(m3.exists(), "Pair 3 main should be kept")
        self.assertTrue(c3.exists(), "Pair 3 composite should be kept")

    def test_pair_review_manager_set_all(self) -> None:
        mem_dir = self.out_dir / "memories"
        m1 = mem_dir / "img1-main.jpg"
        c1 = mem_dir / "img1-composite.jpg"
        self._create_sample_image(m1)
        self._create_sample_image(c1)

        mgr = rog.PairReviewManager(self.out_dir, [
            {"main_rel_path": "memories/img1-main.jpg", "composite_rel_path": "memories/img1-composite.jpg"}
        ])
        mgr.set_all_choices("with_overlay")
        self.assertEqual(mgr.items[0].choice, "with_overlay")
        res = mgr.apply_deletions()
        self.assertFalse(m1.exists())
        self.assertTrue(c1.exists())

    def test_discover_overlay_pairs(self) -> None:
        mem_dir = self.out_dir / "memories"
        mem_dir.mkdir(parents=True, exist_ok=True)
        m1 = mem_dir / "2023-01-01_abcd-main.jpg"
        c1 = mem_dir / "2023-01-01_abcd-composite.jpg"
        self._create_sample_image(m1)
        self._create_sample_image(c1)

        other = mem_dir / "standalone.jpg"
        self._create_sample_image(other)

        pairs = rog.discover_overlay_pairs(self.out_dir)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["main_rel_path"], "memories/2023-01-01_abcd-main.jpg")
        self.assertEqual(pairs[0]["composite_rel_path"], "memories/2023-01-01_abcd-composite.jpg")

    def test_save_debug_output(self) -> None:
        unsupported = [
            {"rel_path": "memories/corrupt.heic", "reason": "unsupported_media_format:.heic"}
        ]
        heic_path = self.src_dir / "memories" / "corrupt.heic"
        heic_path.parent.mkdir(parents=True, exist_ok=True)
        heic_path.write_bytes(b"dummy heic content")

        conflicts = [
            {
                "rel_path": "memories/conflict.jpg",
                "conflicts": ["disagreement_between_filename_and_gallery_date"],
                "partial_actions": ["date_only_written"],
                "details": {
                    "filename_date": "2023-01-01",
                    "gallery_dates": ["2023-01-02"],
                    "linked_full_timestamps": [],
                    "embedded_date": "2023-01-03",
                    "embedded_time": None,
                    "embedded_full_exif": None,
                    "embedded_gps": None,
                    "linked_locations": [],
                },
            }
        ]
        self._create_sample_image(self.src_dir / "memories" / "conflict.jpg")

        ambiguous = [
            {"rel_path": "chat_media/ambig.jpg", "reasons": ["multiple_chat_media_files_match_same_media_id"]}
        ]
        self._create_sample_image(self.src_dir / "chat_media" / "ambig.jpg")

        failed_comp = [
            {"main_rel_path": "memories/m.jpg", "overlay_rel_path": "memories/o.png", "reason": "composite_error:corrupt"}
        ]
        self._create_sample_image(self.src_dir / "memories" / "m.jpg")
        self._create_sample_image(self.src_dir / "memories" / "o.png")

        exiftool = pse.ExifToolAdapter()
        summary = pse.save_debug_output(
            debug_dir=self.debug_dir,
            src_root=self.src_dir,
            work_root=self.src_dir,
            exiftool=exiftool,
            unsupported_files=unsupported,
            conflict_files=conflicts,
            ambiguous_files=ambiguous,
            failed_composites=failed_comp,
            video_overlays_omitted=[],
        )

        self.assertEqual(summary["unsupported_count"], 1)
        self.assertEqual(summary["conflict_count"], 1)
        self.assertEqual(summary["ambiguous_count"], 1)
        self.assertEqual(summary["failed_composites_count"], 1)

        self.assertTrue((self.debug_dir / "unsupported" / "memories" / "corrupt.heic").is_file())
        self.assertTrue((self.debug_dir / "unsupported" / "memories" / "corrupt.heic.debug.txt").is_file())

        self.assertTrue((self.debug_dir / "conflicts" / "memories" / "conflict.jpg").is_file())
        conf_note = (self.debug_dir / "conflicts" / "memories" / "conflict.jpg.debug.txt").read_text(encoding="utf-8")
        self.assertIn("disagreement_between_filename_and_gallery_date", conf_note)
        self.assertIn("Filename Date: 2023-01-01", conf_note)
        self.assertIn("Gallery Dates: ['2023-01-02']", conf_note)

        self.assertTrue((self.debug_dir / "ambiguous" / "chat_media" / "ambig.jpg").is_file())
        self.assertTrue((self.debug_dir / "failed_composites" / "m" / "m.jpg").is_file())
        self.assertTrue((self.debug_dir / "failed_composites" / "m" / "failure_info.txt").is_file())

        self.assertTrue((self.debug_dir / "debug_report.json").is_file())
        self.assertTrue((self.debug_dir / "debug_log.txt").is_file())

    def test_launch_overlay_review_gui_web_flow(self) -> None:
        mem_dir = self.out_dir / "memories"
        m1 = mem_dir / "img1-main.jpg"
        c1 = mem_dir / "img1-composite.jpg"
        self._create_sample_image(m1)
        self._create_sample_image(c1)

        pairs = [{"main_rel_path": "memories/img1-main.jpg", "composite_rel_path": "memories/img1-composite.jpg"}]
        result_holder: list[dict[str, Any]] = []

        def _run_server() -> None:
            res = rog.launch_overlay_review_gui(self.out_dir, pairs, port=18944, open_browser=False)
            result_holder.append(res)

        server_thread = threading.Thread(target=_run_server, daemon=True)
        server_thread.start()

        import time
        time.sleep(0.5)

        base_url = "http://127.0.0.1:18944"

        with urllib.request.urlopen(f"{base_url}/") as resp:
            self.assertEqual(resp.status, 200)
            html = resp.read().decode("utf-8")
            self.assertIn("Review Overlay Pairs", html)
            self.assertIn("box-orig", html)
            self.assertIn("box-comp", html)
            self.assertIn("onclick=\"setChoice('without_overlay')\"", html)
            self.assertIn("onclick=\"setChoice('with_overlay')\"", html)

        with urllib.request.urlopen(f"{base_url}/image?path=memories/img1-main.jpg") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "image/jpeg")

        with urllib.request.urlopen(f"{base_url}/api/pairs") as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(len(data), 1)

        set_req = urllib.request.Request(
            f"{base_url}/api/set_choice",
            data=json.dumps({"index": 0, "choice": "with_overlay"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(set_req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("choice"), "with_overlay")

        req = urllib.request.Request(
            f"{base_url}/api/apply",
            data=json.dumps({"choices": {"0": "with_overlay"}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))

        server_thread.join(timeout=3)
        self.assertEqual(len(result_holder), 1)
        self.assertFalse(m1.exists(), "Main image should be deleted")
        self.assertTrue(c1.exists(), "Composite image should be retained")

    def test_standalone_cli_no_pairs(self) -> None:
        code = rog.main(["-o", str(self.out_dir), "--no-browser"])
        self.assertEqual(code, 0)

    def test_full_process_export_with_debug_flag(self) -> None:
        mem_dir = self.src_dir / "memories"
        mem_dir.mkdir(parents=True, exist_ok=True)
        u = "11111111-2222-3333-4444-555555555555"
        img = mem_dir / f"2023-01-01_{u}-main.jpg"
        self._create_sample_image(img)

        unsupported = mem_dir / f"2023-01-02_{u}-main.avi"
        unsupported.write_bytes(b"dummy avi content")

        html_content = f"""<!DOCTYPE html><html><body>
        <div class="image-container">
            <img src=".//{img.name}">
            <div><div class="text-line">2023-01-01</div></div>
        </div>
        </body></html>"""
        (mem_dir / "memories.html").write_text(html_content, encoding="utf-8")

        report = pse.process_export(
            source_dir=self.src_dir,
            output_dir=self.out_dir,
            overwrite=True,
            debug=True,
            show_progress=False,
        )

        self.assertIn("debug", report)
        self.assertTrue((self.out_dir / "debug" / "debug_report.json").is_file())
        self.assertTrue((self.out_dir / "debug" / "debug_log.txt").is_file())
        self.assertTrue((self.out_dir / "debug" / "unsupported" / "memories" / "2023-01-02_11111111-2222-3333-4444-555555555555-main.avi").is_file())

    def test_cli_argument_parsing(self) -> None:
        parser = pse.build_arg_parser()
        args1 = parser.parse_args(["-s", "src", "-o", "dst", "--debug"])
        self.assertEqual(args1.source, pathlib.Path("src"))
        self.assertEqual(args1.output, pathlib.Path("dst"))
        self.assertTrue(args1.debug)

        args_default = parser.parse_args([])
        self.assertEqual(args_default.source, pathlib.Path("snapchat-data"))
        self.assertEqual(args_default.output, pathlib.Path("snapchat-processed"))
        self.assertFalse(args_default.debug)

        rog_parser = rog.build_arg_parser()
        rog_args = rog_parser.parse_args(["target_dir", "--mode", "chat_media", "-p", "8080"])
        self.assertEqual(rog_args.mode, "chat_media")
        self.assertEqual(rog_args.port, 8080)

        rog_args_default = rog_parser.parse_args([])
        self.assertEqual(rog_args_default.output_dir, pathlib.Path("snapchat-processed"))

    def test_classify_chat_media_type(self) -> None:
        self.assertEqual(rog.classify_chat_media_type("chat/video.mp4", "video.mp4", True), "videos")
        self.assertEqual(rog.classify_chat_media_type("chat/snap-thumbnail.jpg", "snap-thumbnail.jpg", False), "thumbnails")
        self.assertEqual(rog.classify_chat_media_type("chat/thumbnails/snap.jpg", "snap.jpg", False), "thumbnails")
        self.assertEqual(rog.classify_chat_media_type("chat/overlay_graphic.png", "overlay_graphic.png", False), "overlays")
        self.assertEqual(rog.classify_chat_media_type("chat/sticker_smile.png", "sticker_smile.png", False), "stickers")
        self.assertEqual(rog.classify_chat_media_type("chat/normal_pic.jpg", "normal_pic.jpg", False), "photos")

    def test_chat_media_delete_group_and_set_group_choice(self) -> None:
        chat_dir = self.out_dir / "chat_media"
        chat_dir.mkdir(parents=True, exist_ok=True)
        t1 = chat_dir / "pic-thumbnail.jpg"
        t2 = chat_dir / "other-thumb.jpg"
        ov = chat_dir / "banner-overlay.png"
        pic = chat_dir / "camera.jpg"
        self._create_sample_image(t1)
        self._create_sample_image(t2)
        self._create_sample_image(ov)
        self._create_sample_image(pic)

        items = [
            {"rel_path": "chat_media/pic-thumbnail.jpg"},
            {"rel_path": "chat_media/other-thumb.jpg"},
            {"rel_path": "chat_media/banner-overlay.png"},
            {"rel_path": "chat_media/camera.jpg"},
        ]
        mgr = rog.ChatMediaReviewManager(self.out_dir, items)
        self.assertEqual(mgr.items[0].media_type, "thumbnails")
        self.assertEqual(mgr.items[1].media_type, "thumbnails")
        self.assertEqual(mgr.items[2].media_type, "overlays")
        self.assertEqual(mgr.items[3].media_type, "photos")

        # Test set_group_choice
        changed_count = mgr.set_group_choice("overlays", "delete")
        self.assertEqual(changed_count, 1)
        self.assertEqual(mgr.items[2].choice, "delete")

        # Test delete_group
        res = mgr.delete_group("thumbnails")
        self.assertEqual(res["deleted"], 2)
        self.assertFalse(t1.exists())
        self.assertFalse(t2.exists())
        self.assertTrue(ov.exists())
        self.assertTrue(pic.exists())

    def test_chat_media_review_manager_choices_and_deletions(self) -> None:
        chat_dir = self.out_dir / "chat_media"
        chat_dir.mkdir(parents=True, exist_ok=True)
        photo = chat_dir / "useful_photo.jpg"
        meme = chat_dir / "funny_meme.jpg"
        screen = chat_dir / "screenshot.png"
        self._create_sample_image(photo)
        self._create_sample_image(meme)
        self._create_sample_image(screen)

        items = [
            {"rel_path": "chat_media/useful_photo.jpg", "size_bytes": 1000},
            {"rel_path": "chat_media/funny_meme.jpg", "size_bytes": 2000},
            {"rel_path": "chat_media/screenshot.png", "size_bytes": 3000},
        ]
        mgr = rog.ChatMediaReviewManager(self.out_dir, items)
        self.assertEqual(len(mgr.items), 3)
        self.assertEqual(mgr.items[0].choice, "keep")

        mgr.set_choice(1, "delete")
        mgr.set_choices_bulk([2], "delete")

        res = mgr.apply_deletions()
        self.assertEqual(res["total_items"], 3)
        self.assertEqual(res["kept"], 1)
        self.assertEqual(res["deleted"], 2)
        self.assertTrue(photo.is_file())
        self.assertFalse(meme.is_file())
        self.assertFalse(screen.is_file())

    def test_discover_chat_media(self) -> None:
        chat_dir = self.out_dir / "chat_media"
        chat_dir.mkdir(parents=True, exist_ok=True)
        img1 = chat_dir / "snap1.jpg"
        vid1 = chat_dir / "video1.mp4"
        txt = chat_dir / "notes.txt"
        self._create_sample_image(img1)
        vid1.write_bytes(b"dummy mp4 content")
        txt.write_text("not media", encoding="utf-8")

        discovered = rog.discover_chat_media(self.out_dir)
        rel_paths = {d["rel_path"] for d in discovered}
        self.assertIn("chat_media/snap1.jpg", rel_paths)
        self.assertIn("chat_media/video1.mp4", rel_paths)
        self.assertNotIn("chat_media/notes.txt", rel_paths)

        vid_item = next(d for d in discovered if d["rel_path"] == "chat_media/video1.mp4")
        self.assertTrue(vid_item["is_video"])

    def test_launch_chat_media_review_gui_web_flow(self) -> None:
        chat_dir = self.out_dir / "chat_media"
        chat_dir.mkdir(parents=True, exist_ok=True)
        img1 = chat_dir / "img1.jpg"
        img2 = chat_dir / "meme.jpg"
        img3 = chat_dir / "extra-thumbnail.jpg"
        self._create_sample_image(img1)
        self._create_sample_image(img2)
        self._create_sample_image(img3)

        chat_items = [
            {"rel_path": "chat_media/img1.jpg", "size_bytes": 100},
            {"rel_path": "chat_media/meme.jpg", "size_bytes": 200},
            {"rel_path": "chat_media/extra-thumbnail.jpg", "size_bytes": 150},
        ]
        result_holder: list[dict[str, Any]] = []

        def _run_server() -> None:
            res = rog.launch_overlay_review_gui(
                self.out_dir,
                composites=[],
                chat_media=chat_items,
                mode="chat_media",
                port=18946,
                open_browser=False,
            )
            result_holder.append(res)

        server_thread = threading.Thread(target=_run_server, daemon=True)
        server_thread.start()

        import time
        time.sleep(0.5)

        base_url = "http://127.0.0.1:18946"

        with urllib.request.urlopen(f"{base_url}/") as resp:
            self.assertEqual(resp.status, 200)
            html = resp.read().decode("utf-8")
            self.assertIn("Chat Media Filter", html)
            self.assertIn("Categories / Media Types", html)

        with urllib.request.urlopen(f"{base_url}/api/chat/items") as resp:
            self.assertEqual(resp.status, 200)
            items = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(len(items), 3)

        group_choice_req = urllib.request.Request(
            f"{base_url}/api/chat/set_group_choice",
            data=json.dumps({"group": "photos", "choice": "keep"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(group_choice_req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))

        del_group_req = urllib.request.Request(
            f"{base_url}/api/chat/delete_group",
            data=json.dumps({"group": "thumbnails"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(del_group_req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertEqual(data["result"]["deleted"], 1)
        self.assertFalse(img3.exists(), "Thumbnail should be deleted by delete_group endpoint")

        set_req = urllib.request.Request(
            f"{base_url}/api/chat/set_choice",
            data=json.dumps({"index": 1, "choice": "delete"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(set_req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("choice"), "delete")

        bulk_req = urllib.request.Request(
            f"{base_url}/api/chat/set_bulk",
            data=json.dumps({"indices": [1], "choice": "delete"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(bulk_req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))

        apply_req = urllib.request.Request(
            f"{base_url}/api/chat/apply",
            data=json.dumps({"choices": {"0": "keep", "1": "delete"}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(apply_req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))

        server_thread.join(timeout=3)
        self.assertEqual(len(result_holder), 1)
        self.assertTrue(img1.exists(), "Kept chat media should exist")
        self.assertFalse(img2.exists(), "Deleted meme should be unlinked")

    def test_process_export_gui_dry_run(self) -> None:
        mem_dir = self.src_dir / "memories"
        mem_dir.mkdir(parents=True, exist_ok=True)
        u = "22222222-3333-4444-5555-666666666666"
        main_img = mem_dir / f"2023-05-01_{u}-main.jpg"
        ov_img = mem_dir / f"2023-05-01_{u}-overlay.png"
        self._create_sample_image(main_img)
        self._create_sample_image(ov_img)

        html_content = f"""<!DOCTYPE html><html><body>
        <div class="image-container">
            <img src=".//{main_img.name}">
            <img src=".//{ov_img.name}" class="overlay-image">
            <div><div class="text-line">2023-05-01</div></div>
        </div>
        </body></html>"""
        (mem_dir / "memories.html").write_text(html_content, encoding="utf-8")

        dry_report = pse.process_export(
            source_dir=self.src_dir,
            output_dir=self.out_dir,
            dry_run=True,
            overwrite=True,
            gui=True,
            show_progress=False,
        )
        self.assertNotIn("overlay_review", dry_report)

    def test_unified_discover_all_review_items(self) -> None:
        mem_dir = self.out_dir / "memories"
        chat_dir = self.out_dir / "chat_media"
        mem_dir.mkdir(parents=True, exist_ok=True)
        chat_dir.mkdir(parents=True, exist_ok=True)

        m1 = mem_dir / "2023-01-01_11111111-1111-1111-1111-111111111111-main.jpg"
        c1 = mem_dir / "2023-01-01_11111111-1111-1111-1111-111111111111-composite.jpg"
        m_standalone = mem_dir / "2023-01-02_22222222-2222-2222-2222-222222222222-main.jpg"
        self._create_sample_image(m1)
        self._create_sample_image(c1)
        self._create_sample_image(m_standalone)

        ch_photo = chat_dir / "photo.jpg"
        ch_thumb = chat_dir / "pic-thumbnail.png"
        self._create_sample_image(ch_photo)
        self._create_sample_image(ch_thumb)

        items = rog.discover_all_review_items(self.out_dir)
        self.assertEqual(len(items), 4)

        pair_items = [it for it in items if it["item_type"] == "pair"]
        self.assertEqual(len(pair_items), 1)
        self.assertEqual(pair_items[0]["main_rel_path"], "memories/2023-01-01_11111111-1111-1111-1111-111111111111-main.jpg")
        self.assertEqual(pair_items[0]["composite_rel_path"], "memories/2023-01-01_11111111-1111-1111-1111-111111111111-composite.jpg")
        self.assertEqual(pair_items[0]["source"], "memories")

        mem_standalone_items = [it for it in items if it["item_type"] == "single" and it["source"] == "memories"]
        self.assertEqual(len(mem_standalone_items), 1)
        self.assertEqual(mem_standalone_items[0]["filename"], m_standalone.name)

        chat_items = [it for it in items if it["source"] == "chat_media"]
        self.assertEqual(len(chat_items), 2)
        media_types = {it["media_type"] for it in chat_items}
        self.assertIn("photos", media_types)
        self.assertIn("thumbnails", media_types)

    def test_unified_review_manager_overlay_pair_deletions(self) -> None:
        mem_dir = self.out_dir / "memories"
        mem_dir.mkdir(parents=True, exist_ok=True)

        # Pair 1: Keep Both (neither deleted)
        m1 = mem_dir / "p1-main.jpg"
        c1 = mem_dir / "p1-composite.jpg"
        self._create_sample_image(m1)
        self._create_sample_image(c1)

        # Pair 2: Keep Without Overlay (composite deleted)
        m2 = mem_dir / "p2-main.jpg"
        c2 = mem_dir / "p2-composite.jpg"
        self._create_sample_image(m2)
        self._create_sample_image(c2)

        # Pair 3: Keep With Overlay (main deleted)
        m3 = mem_dir / "p3-main.jpg"
        c3 = mem_dir / "p3-composite.jpg"
        self._create_sample_image(m3)
        self._create_sample_image(c3)

        # Pair 4: Delete Both
        m4 = mem_dir / "p4-main.jpg"
        c4 = mem_dir / "p4-composite.jpg"
        self._create_sample_image(m4)
        self._create_sample_image(c4)

        raw_items = [
            {"item_type": "pair", "source": "memories", "media_type": "pairs", "rel_path": "memories/p1-main.jpg", "main_rel_path": "memories/p1-main.jpg", "composite_rel_path": "memories/p1-composite.jpg"},
            {"item_type": "pair", "source": "memories", "media_type": "pairs", "rel_path": "memories/p2-main.jpg", "main_rel_path": "memories/p2-main.jpg", "composite_rel_path": "memories/p2-composite.jpg"},
            {"item_type": "pair", "source": "memories", "media_type": "pairs", "rel_path": "memories/p3-main.jpg", "main_rel_path": "memories/p3-main.jpg", "composite_rel_path": "memories/p3-composite.jpg"},
            {"item_type": "pair", "source": "memories", "media_type": "pairs", "rel_path": "memories/p4-main.jpg", "main_rel_path": "memories/p4-main.jpg", "composite_rel_path": "memories/p4-composite.jpg"},
        ]
        mgr = rog.UnifiedReviewManager(self.out_dir, raw_items)
        mgr.set_choice(0, "both")
        mgr.set_choice(1, "without_overlay")
        mgr.set_choice(2, "with_overlay")
        mgr.set_choice(3, "delete_both")

        res = mgr.apply_deletions()
        self.assertEqual(res["pairs_total"], 4)
        self.assertEqual(res["pairs_kept_both"], 1)
        self.assertEqual(res["pairs_kept_without_overlay"], 1)
        self.assertEqual(res["pairs_kept_with_overlay"], 1)
        self.assertEqual(res["pairs_deleted_both"], 1)

        # Pair 1: both exist
        self.assertTrue(m1.is_file())
        self.assertTrue(c1.is_file())

        # Pair 2: main kept, composite deleted
        self.assertTrue(m2.is_file())
        self.assertFalse(c2.exists())

        # Pair 3: main deleted, composite kept
        self.assertFalse(m3.exists())
        self.assertTrue(c3.is_file())

        # Pair 4: both deleted
        self.assertFalse(m4.exists())
        self.assertFalse(c4.exists())

    def test_unified_review_manager_bulk_selection_and_deletion(self) -> None:
        chat_dir = self.out_dir / "chat_media"
        chat_dir.mkdir(parents=True, exist_ok=True)
        t1 = chat_dir / "t1-thumb.jpg"
        t2 = chat_dir / "t2-thumb.jpg"
        p1 = chat_dir / "p1.jpg"
        p2 = chat_dir / "p2.jpg"
        self._create_sample_image(t1)
        self._create_sample_image(t2)
        self._create_sample_image(p1)
        self._create_sample_image(p2)

        items = [
            {"item_type": "single", "source": "chat_media", "media_type": "thumbnails", "rel_path": "chat_media/t1-thumb.jpg"},
            {"item_type": "single", "source": "chat_media", "media_type": "thumbnails", "rel_path": "chat_media/t2-thumb.jpg"},
            {"item_type": "single", "source": "chat_media", "media_type": "photos", "rel_path": "chat_media/p1.jpg"},
            {"item_type": "single", "source": "chat_media", "media_type": "photos", "rel_path": "chat_media/p2.jpg"},
        ]
        mgr = rog.UnifiedReviewManager(self.out_dir, items)

        # Test bulk setting
        mgr.set_choices_bulk([0, 1], "delete")
        self.assertEqual(mgr.items[0].choice, "delete")
        self.assertEqual(mgr.items[1].choice, "delete")
        self.assertEqual(mgr.items[2].choice, "keep")

        # Test group deletion
        del_res = mgr.delete_group("thumbnails", source="chat_media")
        self.assertEqual(del_res["deleted"], 2)
        self.assertFalse(t1.exists())
        self.assertFalse(t2.exists())
        self.assertTrue(p1.is_file())
        self.assertTrue(p2.is_file())

    def test_unified_gui_web_flow(self) -> None:
        mem_dir = self.out_dir / "memories"
        chat_dir = self.out_dir / "chat_media"
        mem_dir.mkdir(parents=True, exist_ok=True)
        chat_dir.mkdir(parents=True, exist_ok=True)

        m1 = mem_dir / "2023-01-01_11111111-1111-1111-1111-111111111111-main.jpg"
        c1 = mem_dir / "2023-01-01_11111111-1111-1111-1111-111111111111-composite.jpg"
        ch1 = chat_dir / "chat1.jpg"
        self._create_sample_image(m1)
        self._create_sample_image(c1)
        self._create_sample_image(ch1)

        result_holder: list[dict[str, Any]] = []

        def _run_server() -> None:
            res = rog.launch_review_gui(
                self.out_dir,
                port=18948,
                open_browser=False,
            )
            result_holder.append(res)

        server_thread = threading.Thread(target=_run_server, daemon=True)
        server_thread.start()

        import time
        time.sleep(0.5)

        base_url = "http://127.0.0.1:18948"

        with urllib.request.urlopen(f"{base_url}/api/items") as resp:
            self.assertEqual(resp.status, 200)
            items = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(len(items), 2)  # 1 pair + 1 chat item

        pair_idx = next(it["index"] for it in items if it["item_type"] == "pair")
        single_idx = next(it["index"] for it in items if it["item_type"] == "single")

        # Set pair choice to with_overlay (delete main)
        set_req = urllib.request.Request(
            f"{base_url}/api/set_choice",
            data=json.dumps({"index": pair_idx, "choice": "with_overlay"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(set_req) as resp:
            self.assertEqual(resp.status, 200)

        # Bulk set chat item to keep
        bulk_req = urllib.request.Request(
            f"{base_url}/api/set_bulk",
            data=json.dumps({"indices": [single_idx], "choice": "keep"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(bulk_req) as resp:
            self.assertEqual(resp.status, 200)

        # Apply deletions
        apply_req = urllib.request.Request(
            f"{base_url}/api/apply",
            data=json.dumps({"choices": {str(pair_idx): "with_overlay", str(single_idx): "keep"}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(apply_req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))

        server_thread.join(timeout=3)
        self.assertEqual(len(result_holder), 1)
        self.assertFalse(m1.exists(), "Pair main should be deleted")
        self.assertTrue(c1.exists(), "Pair composite should be retained")
        self.assertTrue(ch1.exists(), "Chat item should be kept")


if __name__ == "__main__":
    unittest.main()
