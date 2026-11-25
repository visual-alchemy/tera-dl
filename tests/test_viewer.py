import unittest
from unittest import mock

import tera.viewer as viewer


class TestViewerSelection(unittest.TestCase):
    def setUp(self):
        self.popen = mock.patch("tera.viewer.subprocess.Popen").start()
        self.addCleanup(mock.patch.stopall)

    def test_available_prefers_termux_then_mpv(self):
        def which(name):
            return "/bin/" + name if name in {"mpv", "xdg-open"} else None

        with mock.patch("tera.viewer.shutil.which", which):
            self.assertEqual(viewer.available(), "mpv")
            self.assertIsNotNone(viewer.available())

    def test_linux_without_mpv_uses_xdg_open(self):
        """Desktop Linux/macOS must not dead-end when mpv is missing."""
        def which(name):
            return "/usr/bin/xdg-open" if name == "xdg-open" else None

        with mock.patch("tera.viewer.shutil.which", which):
            used = viewer.open_target("https://example.test/x.mp4", title="x")

        self.assertEqual(used, "xdg-open")
        self.popen.assert_called_once()
        self.assertEqual(self.popen.call_args[0][0], ["/usr/bin/xdg-open", "https://example.test/x.mp4"])

    def test_macos_open_is_used(self):
        def which(name):
            return "/usr/bin/open" if name == "open" else None

        with mock.patch("tera.viewer.shutil.which", which):
            self.assertEqual(viewer.open_target("https://example.test/y.mp4"), "open")

    def test_no_opener_returns_none(self):
        with mock.patch("tera.viewer.shutil.which", lambda name: None):
            self.assertIsNone(viewer.open_target("https://example.test/z.mp4"))

    def test_mpv_gets_auth_headers(self):
        def which(name):
            return "/usr/bin/mpv" if name == "mpv" else None

        with mock.patch("tera.viewer.shutil.which", which):
            used = viewer.open_target(
                "https://example.test/a.mp4", title="a", cookie="ndus=1", ua="UA"
            )

        self.assertEqual(used, "mpv")
        args = self.popen.call_args[0][0]
        self.assertIn("--force-media-title=a", args)
        self.assertTrue(any(a.startswith("--http-header-fields=") for a in args))

    def test_image_prefers_external_over_mpv(self):
        def which(name):
            return "/usr/bin/" + name if name in {"mpv", "xdg-open"} else None

        with mock.patch("tera.viewer.shutil.which", which):
            used = viewer.open_target("https://example.test/p.png", prefer_external=True)

        self.assertEqual(used, "xdg-open")

    def test_termux_path_uses_am_then_termux_open(self):
        calls = []

        def which(name):
            return "/bin/termux-open" if name == "termux-open" else None

        def run(args, **kwargs):
            calls.append(args)
            return mock.Mock(returncode=0)

        with mock.patch("tera.viewer.shutil.which", which), \
                mock.patch("tera.viewer.subprocess.run", run):
            used = viewer.open_target("/tmp/a.mp4", local=True)

        self.assertEqual(used, "mpv-android")
        self.assertIn("file:///tmp/a.mp4", calls[0])

    def test_termux_falls_back_to_termux_open(self):
        def which(name):
            return "/bin/termux-open" if name == "termux-open" else None

        with mock.patch("tera.viewer.shutil.which", which), \
                mock.patch("tera.viewer.subprocess.run", side_effect=OSError("no am")):
            used = viewer.open_target("https://example.test/a.mp4")

        self.assertEqual(used, "termux-open")
        self.assertEqual(self.popen.call_args[0][0], ["termux-open", "--view", "https://example.test/a.mp4"])


class TestViewerTuiIntegration(unittest.TestCase):
    """The TUI hands its viewer choice to run_tui() after the app exits."""

    def _run(self, patched_available):
        import asyncio
        try:
            from tera.tui import TeraBoxTUI
        except ImportError:
            self.skipTest("textual not installed")

        files = [
            {"server_filename": "pic.png", "isdir": 0, "size": 5, "path": "/pic.png", "server_mtime": 0},
            {"server_filename": "vid.mp4", "isdir": 0, "size": 9, "path": "/vid.mp4", "server_mtime": 0},
        ]

        class FakeClient:
            def list_files(self, path, num=1000):
                return files

            def get_download_link(self, path):
                return f"https://example.test{path}"

        results = {}

        async def run():
            app = TeraBoxTUI(FakeClient(), Config())
            async with app.run_test(size=(100, 26)) as pilot:
                await pilot.pause()
                await pilot.press("tab")
                await pilot.pause()
                pane = app._get_pane("remote")
                for i, ext in ((0, "png"), (1, "mp4")):
                    app._pending_view = None
                    app._do_view(pane.backend, pane.entries[i])
                    await pilot.pause()
                    results[ext] = app._pending_view

        from tera.config import Config

        with mock.patch("tera.viewer.available", return_value=patched_available):
            asyncio.run(run())
        return results

    def test_images_prefer_external_on_termux(self):
        res = self._run("termux-open")
        self.assertTrue(res["png"]["prefer_external"])
        self.assertFalse(res["mp4"]["prefer_external"])

    def test_desktop_images_stay_on_mpv(self):
        """open/xdg-open drop the cookie, so desktop must not delegate images."""
        res = self._run("mpv")
        self.assertFalse(res["png"]["prefer_external"])
        self.assertFalse(res["mp4"]["prefer_external"])


if __name__ == "__main__":
    unittest.main()
