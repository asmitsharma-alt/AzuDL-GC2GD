import contextlib
import http.server
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from yt_dlp import YoutubeDL

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("azudl", ROOT / "Code [BETA 1.4.20 GUI].py")
azu = importlib.util.module_from_spec(SPEC)
colab = types.ModuleType("google.colab")
colab.drive = object()
with patch.dict(sys.modules, {"google.colab": colab}), patch("shutil.which", return_value="installed"):
    SPEC.loader.exec_module(azu)


class FileServer(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def copyfile(self, source, target):
        if self.path != "/resume.bin":
            return super().copyfile(source, target)
        try:
            while block := source.read(64 * 1024):
                target.write(block)
                if self.server.slow:
                    time.sleep(0.04)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def send_head(self):
        if self.path == "/resume.bin" and self.headers.get("Range"):
            start = int(self.headers["Range"].removeprefix("bytes=").split("-")[0])
            source = Path(self.directory, "resume.bin").open("rb")
            total = os.fstat(source.fileno()).st_size
            source.seek(start)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
            self.send_header("Content-Length", str(total - start))
            self.end_headers()
            return source
        return super().send_head()


class DriveEditionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        with patch.dict(os.environ, {"AZUDL_HOSTED_COLAB": "1"}):
            self.app = azu.AzuDlGC2GD()
        self.app.my_drive_path = self.root / "drive"
        self.app.my_drive_path.mkdir()
        for name in ("direct", "youtube", "github"):
            setattr(self.app, name + "_dir", self.app.my_drive_path / name)
        for name in ("force_drive_sync", "save_history", "print_status", "print_kv",
                     "print_section", "print_subsection", "print_drive_visibility_note"):
            setattr(self.app, name, MagicMock())
        self.app.get_github_token = MagicMock(return_value="")
        self.sources = self.root / "sources"
        self.sources.mkdir()

    @contextlib.contextmanager
    def server(self):
        def handler(*args, **kwargs):
            return FileServer(*args, directory=self.sources, **kwargs)
        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            server.slow = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield server, f"http://127.0.0.1:{server.server_port}"
            finally:
                server.shutdown()
                thread.join()

    def test_hosted_controls_keep_existing_gui(self):
        self.assertEqual(self.app.aria2_session_file.name, "aria2-http.session")
        with self.assertRaisesRegex(ValueError, "completed HTTPS"):
            self.app.download_magnet("magnet:?xt=urn:btih:example")
        with self.assertRaisesRegex(ValueError, "completed HTTPS"):
            self.app.download_torrent_file("sample.torrent")
        with self.assertRaisesRegex(ValueError, "completed HTTPS"):
            self.app.download_direct("https://example.org/test.torrent?key=abc")
        with patch.object(azu.AzuDlGC2GDGUI, "refresh_storage"):
            gui = azu.AzuDlGC2GDGUI(self.app)
        self.assertIsInstance(gui.build_torrent_tab(), azu.widgets.Box)

    def test_downloads_are_visible_on_launch_and_navigation_keeps_inputs(self):
        gui = azu.AzuDlGC2GDGUI(self.app)
        default_panel, = gui.section_content.children
        self.assertEqual(gui.section_picker.value, "Direct")
        self.assertIn(gui.direct_url, default_panel.children)
        self.assertIn(gui.direct_start, default_panel.children)
        gui.direct_url.value = "https://example.org/file.zip"
        gui.section_picker.value = "YouTube"
        self.assertIn(gui.youtube_url, gui.section_content.children[0].children)
        gui.section_picker.value = "Direct"
        self.assertIs(gui.section_content.children[0], default_panel)
        self.assertEqual(gui.direct_url.value, "https://example.org/file.zip")

    def test_direct_quota_and_existing_checkpoint(self):
        self.app.add_aria2_download = MagicMock(return_value="1")
        for name in ("monitor_aria2", "print_aria2_item_paths", "verify_aria2_status_files", "verify_download_outputs"):
            setattr(self.app, name, MagicMock())
        with patch.object(azu.requests, "head") as head, patch.object(azu.shutil, "disk_usage") as usage:
            head.return_value.__enter__.return_value.headers = {"Content-Length": "100"}
            usage.return_value = types.SimpleNamespace(total=200, free=70)
            with self.assertRaisesRegex(RuntimeError, "Google Drive reports"):
                self.app.download_direct("https://example.org/file")
            self.app.add_aria2_download.assert_not_called()
            save_dir = self.app.add_aria2_download
            destination = self.app.direct_dir / "Direct"
            destination.mkdir(exist_ok=True, parents=True)
            (destination / "file.aria2").touch()
            self.app.download_direct("https://example.org/file")
            self.assertEqual(save_dir.call_args.args[1], destination)
            self.assertEqual(save_dir.call_args.args[3]["follow-torrent"], "false")
            self.assertEqual(head.call_count, 1)

    @unittest.skipUnless(shutil.which("aria2c"), "aria2 is required")
    def test_real_aria2_resumes_to_drive_folder(self):
        data = bytes(range(256)) * 32768
        (self.sources / "resume.bin").write_bytes(data)
        destination = self.app.drive_folder(self.app.direct_dir / "test")
        with self.server() as (server, base):
            server.slow = True
            command = ["aria2c", "--dir=" + str(destination), "--continue=true",
                       "--file-allocation=none", "--summary-interval=0", base + "/resume.bin"]
            subprocess.run([*command[:-1], "--stop=1", "--auto-save-interval=1", command[-1]],
                           capture_output=True, timeout=15)
            self.assertTrue((destination / "resume.bin.aria2").is_file())
            server.slow = False
            subprocess.run(command, check=True, capture_output=True, timeout=15)
        self.assertEqual((destination / "resume.bin").read_bytes(), data)

    def test_github_stream_and_collision(self):
        with patch.object(azu.requests, "get") as get:
            response = get.return_value.__enter__.return_value
            response.headers = {"Content-Length": "6"}
            response.iter_content.return_value = [b"abc", b"def"]
            target = self.app.github_dir / "assets" / "archive.zip"
            url = "https://github.com/example/repo/releases/download/v1/archive.zip"
            self.assertEqual(self.app.download_file_requests(url, target), target)
            self.assertEqual(self.app.download_file_requests(url, target).name, "archive_1.zip")
            self.assertEqual(target.read_bytes(), b"abcdef")
            self.assertFalse((target.parent / azu.DRIVE_STAGE).exists())
            with self.assertRaisesRegex(ValueError, "GitHub HTTPS"):
                self.app.download_file_requests("https://example.org/download", target)

    def test_interrupted_github_file_is_never_published(self):
        def chunks():
            yield b"abc"
            raise azu.requests.ConnectionError("sensitive URL")
        with patch.object(azu.requests, "get") as get:
            response = get.return_value.__enter__.return_value
            response.headers = {"Content-Length": "6"}
            response.iter_content.return_value = chunks()
            target = self.app.github_dir / "archive.zip"
            with self.assertRaisesRegex(RuntimeError, "transfer interrupted"):
                self.app.download_file_requests("https://github.com/example/repo/archive.zip", target)
        self.assertFalse(target.exists())
        partial, = (target.parent / azu.DRIVE_STAGE).glob("*.part")
        self.assertEqual(partial.read_bytes(), b"abc")
        self.assertFalse(self.app.collect_final_files(target.parent))

    def test_notebook_compiles(self):
        notebook = json.loads((ROOT / "AzuDL_Drive.ipynb").read_text(encoding="utf-8"))
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                compile("".join(cell["source"]), cell["id"], "exec")

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_ytdlp_merge_playlist_and_mp3_stay_on_drive(self):
        for name, source in (("video.mp4", "color=size=128x96:rate=10"),
                             ("audio.m4a", "sine=frequency=440")):
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", source,
                            "-t", "0.5", str(self.sources / name)], check=True)
        app = self.app
        with self.server() as (_, base):
            def info(identifier):
                return {
                    "id": identifier, "title": identifier, "extractor": "test", "extractor_key": "Test",
                    "formats": [
                        {"format_id": "v", "url": base + "/video.mp4", "ext": "mp4",
                         "height": 96, "vcodec": "avc1", "acodec": "none",
                         "filesize": (self.sources / "video.mp4").stat().st_size},
                        {"format_id": "a", "url": base + "/audio.m4a", "ext": "m4a",
                         "vcodec": "none", "acodec": "mp4a",
                         "filesize": (self.sources / "audio.m4a").stat().st_size},
                    ],
                }

            class FixtureDownloader(YoutubeDL):
                def download(self, urls):
                    test.assertTrue(Path(self.params["paths"]["temp"]).is_relative_to(app.my_drive_path))
                    test.assertTrue(Path(self.params["outtmpl"]["default"]).is_relative_to(app.my_drive_path))
                    if urls[0].endswith("playlist"):
                        self.process_ie_result({"_type": "playlist", "id": "p", "title": "Test",
                                                "extractor": "test", "extractor_key": "Test",
                                                "entries": [info("first"), info("second")]}, download=True)
                    else:
                        self.process_ie_result(info("clip"), download=True)
                    return 0

            test = self
            with patch.object(azu, "YoutubeDL", FixtureDownloader), contextlib.redirect_stdout(io.StringIO()):
                app.download_youtube("https://example.org/playlist", folder_name="merge")
                app.download_youtube("https://example.org/video", folder_name="audio", audio_only=True)
            self.assertEqual(len(list((app.youtube_dir / "merge").glob("*.mp4"))), 2)
            self.assertEqual(len(list((app.youtube_dir / "audio").glob("*.mp3"))), 1)
            merged = next((app.youtube_dir / "merge").glob("*.mp4"))
            checked = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                                               "-of", "csv=p=0", str(merged)], text=True)
            self.assertIn("audio", checked)
            self.assertIn("video", checked)
            self.assertFalse(list(app.youtube_dir.rglob(azu.DRIVE_STAGE)))


if __name__ == "__main__":
    unittest.main()
