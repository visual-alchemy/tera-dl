import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tera.config as config_mod
import tera.downloader as downloader_mod
import tera.uploader as uploader_mod
from tera.client import TeraBoxClient
from tera.config import AuthConfig, Config


class TestParseShareUrl(unittest.TestCase):
    def _cases(self):
        return [
            ("https://terabox.com/s/1abcDEF_", "1abcDEF_"),
            ("https://www.terabox.com/s/xyz123", "xyz123"),
            ("https://1024terabox.com/s/xyz", "xyz"),
            ("https://teraboxshare.com/s/xyz", "xyz"),
            ("https://teraboxlink.com/s/xyz", "xyz"),
            ("https://terasharefile.com/s/xyz", "xyz"),
            ("https://terafileshare.com/s/xyz", "xyz"),
            ("https://terasharelink.com/s/xyz", "xyz"),
            ("https://terabox.app/s/xyz", "xyz"),
            ("http://x.test/share?url=https%3A%2F%2F1024terabox.com%2Fs%2Fabc&surl=zzz", "zzz"),
            ("not a link", None),
            ("", None),
        ]

    def test_all_domains(self):
        for url, want in self._cases():
            self.assertEqual(TeraBoxClient.parse_share_url(url), want, url)

    def test_23char_shorturl_strips_leading_one(self):
        short = "1" + "a" * 22
        self.assertEqual(
            TeraBoxClient.parse_share_url(f"https://terabox.com/s/{short}"),
            "a" * 22,
        )

    def test_23char_without_leading_one_kept(self):
        short = "b" + "a" * 22
        self.assertEqual(
            TeraBoxClient.parse_share_url(f"https://terabox.com/s/{short}"),
            short,
        )


class _FakeResp:
    def __init__(self, status_code, data=b"", filesize=None):
        self.status_code = status_code
        self._data = data
        self._filesize = filesize

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=8192):
        data = self._data
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]


class _FakeProgress:
    def update(self, task_id, **kwargs):
        self.calls = getattr(self, "calls", [])
        self.calls.append((task_id, kwargs))


class TestDownloadChunkResume(unittest.TestCase):
    def _call(self, part_file: Path, resp: _FakeResp):
        with mock.patch("requests.get", return_value=resp) as get:
            ok = downloader_mod.download_chunk(
                url="https://cdn.test/f",
                headers={},
                start=0,
                end=9,
                part_file_path=part_file,
                progress=_FakeProgress(),
                rich_task_id=0,
            )
        return ok, get

    def test_partial_part_appends_206(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.part.0"
            p.write_bytes(b"XXXX")
            ok, get = self._call(p, _FakeResp(206, b"abcdef"))
            self.assertTrue(ok)
            self.assertEqual(p.read_bytes(), b"XXXXabcdef")
            get.assert_called_once()
            self.assertIn("Range", get.call_args.kwargs["headers"])
            self.assertEqual(get.call_args.kwargs["headers"]["Range"], "bytes=4-9")

    def test_short_206_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.part.0"
            p.write_bytes(b"XXXX")
            ok, get = self._call(p, _FakeResp(206, b"ab"))
            self.assertFalse(ok)
            self.assertEqual(p.read_bytes(), b"XXXXab")

    def test_complete_part_skips_request(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.part.0"
            p.write_bytes(b"0123456789")
            ok, get = self._call(p, _FakeResp(206))
            self.assertTrue(ok)
            get.assert_not_called()
            self.assertEqual(p.read_bytes(), b"0123456789")

    def test_oversized_part_restarts(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.part.0"
            p.write_bytes(b"0" * 20)
            ok, get = self._call(p, _FakeResp(206, b"abcdefghij"))
            self.assertTrue(ok)
            self.assertEqual(p.read_bytes(), b"abcdefghij")

    def test_server_ignores_range_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.part.0"
            p.write_bytes(b"XXXX")
            ok, get = self._call(p, _FakeResp(200, b"FULLCONTENT"))
            self.assertTrue(ok)
            self.assertEqual(p.read_bytes(), b"FULLCONTENT")


class TestBlockHashes(unittest.TestCase):
    def test_multiblock(self):
        data = b"0123456789ab"  # 12 bytes
        old = uploader_mod.CHUNK_SIZE
        uploader_mod.CHUNK_SIZE = 5
        try:
            with tempfile.NamedTemporaryFile(delete=False) as f:
                f.write(data)
                path = f.name
            try:
                hashes = uploader_mod.compute_block_hashes(path)
            finally:
                os.unlink(path)
        finally:
            uploader_mod.CHUNK_SIZE = old
        expected = [
            hashlib.md5(data[0:5]).hexdigest(),
            hashlib.md5(data[5:10]).hexdigest(),
            hashlib.md5(data[10:12]).hexdigest(),
        ]
        self.assertEqual(hashes, expected)
        self.assertEqual(uploader_mod.md5_of_bytes(data), hashlib.md5(data).hexdigest())


class TestConfigPermissions(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._rmtree)
        self._patch_dirs(self.tmp)
        self.auth = AuthConfig(
            ndus="2:ndus", bduss="bduss", js_token="j", bdstoken="b",
            tokens_refreshed_at="2026-01-01T00:00:00",
        )

    def _rmtree(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch_dirs(self, tmp):
        patchers = [
            mock.patch.object(config_mod, "CONFIG_DIR", tmp),
            mock.patch.object(config_mod, "CONFIG_FILE", tmp / "config.json"),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_save_sets_0600(self):
        cfg = Config(auth=self.auth)
        cfg.save()
        mode = stat.S_IMODE(config_mod.CONFIG_FILE.stat().st_mode)
        self.assertEqual(mode, 0o600)
        mode_dir = stat.S_IMODE(config_mod.CONFIG_DIR.stat().st_mode)
        self.assertEqual(mode_dir, 0o700)

    def test_roundtrip(self):
        cfg = Config(auth=self.auth, workers=7)
        cfg.save()
        loaded = Config.load()
        self.assertEqual(loaded.auth.ndus, "2:ndus")
        self.assertEqual(loaded.auth.tokens_refreshed_at, "2026-01-01T00:00:00")
        self.assertEqual(loaded.workers, 7)

    def test_load_warns_and_fixes_permissive(self):
        cfg = Config(auth=self.auth)
        cfg.save()
        os.chmod(config_mod.CONFIG_FILE, 0o644)
        stderr = sys.stderr
        with tempfile.TemporaryFile(mode="w+") as buf:
            sys.stderr = buf
            try:
                loaded = Config.load()
            finally:
                sys.stderr = stderr
            buf.seek(0)
            self.assertIn("security", buf.read())
        self.assertEqual(loaded.auth.ndus, "2:ndus")
        self.assertEqual(stat.S_IMODE(config_mod.CONFIG_FILE.stat().st_mode), 0o600)


class TestTokenFreshness(unittest.TestCase):
    def test_freshness_window(self):
        cfg = Config()
        client = TeraBoxClient(cfg)  # must not touch network at construction
        self.assertFalse(client._tokens_fresh())

        from datetime import datetime, timedelta
        cfg.auth.tokens_refreshed_at = datetime.now().isoformat()
        self.assertTrue(client._tokens_fresh())

        cfg.auth.tokens_refreshed_at = (datetime.now() - timedelta(minutes=10)).isoformat()
        self.assertFalse(client._tokens_fresh())

        cfg.auth.tokens_refreshed_at = "garbage"
        self.assertFalse(client._tokens_fresh())


class TestHandleMatching(unittest.TestCase):
    FOLDERS = [
        "adelinaagraisha", "agatha_chelsea", "dhivaskz", "gheaindrawari", "livyrenata",
        "Kinandaputriii", "vindaazizah_", "yorikooangln_", "namasayagwen",
        "ptrcia_ao", "nikendalusi", "nylaasla", "danniasalsabila",
        "safirasalbila_", "Nadine Abigail", "melati sesilia", "nyimas yasmin",
        "risaatjan", "fathbayy", "p", "ayu", "anyaer", "anyageraldine", "viinsatr",
        "iniyesika", "angiemstwn", "ecasreveirelav", "michelleeeck99",
        "trslsabila2", "rheanne_felichia",
    ]

    def _match(self, filename):
        return uploader_mod.match_folder(uploader_mod._extract_stem(filename), self.FOLDERS)

    def test_extract_stem_cuts_at_timestamp(self):
        self.assertEqual(
            uploader_mod._extract_stem("agatha_chelsea2026_08_20_17_42_04x.jpg"),
            "agatha_chelsea",
        )
        self.assertEqual(uploader_mod._extract_stem("kinan_exclu1.mp4"), "kinan_exclu1")
        self.assertEqual(uploader_mod._extract_stem("IMG-20260802-WA0021.jpg"), "img-20260802-wa0021")

    def test_exact_and_prefix(self):
        self.assertEqual(self._match("adelinaagraisha2026_08_24_x.jpg"), "adelinaagraisha")
        self.assertEqual(self._match("dhivaskz2026_09_04_x.jpg"), "dhivaskz")
        self.assertEqual(self._match("gheaindrawari2026_09_14_x.jpg"), "gheaindrawari")
        self.assertEqual(self._match("fathbayy2026_09_11_x.jpg"), "fathbayy")
        self.assertEqual(self._match("risaatjan2026_09_17_x.jpg"), "risaatjan")

    def test_overrides(self):
        self.assertEqual(self._match("agatha_df.jpg"), "agatha_chelsea")
        self.assertEqual(self._match("patria_ao_df.jpg"), "ptrcia_ao")
        self.assertEqual(self._match("nayladumq2026_09_03_x.jpg"), "nylaasla")
        self.assertEqual(self._match("nikenandalusi2026_09_06_x.jpg"), "nikendalusi")
        self.assertEqual(self._match("kinan_exclu.mp4"), "Kinandaputriii")
        self.assertEqual(self._match("livy4youu2026_08_24_x.jpg"), "livyrenata")

    def test_short_folder_not_prefix_matched(self):
        self.assertIsNone(self._match("patagonia2026_01_01_x.jpg"))  # 'p' must not match

    def test_unknown_skipped(self):
        self.assertIsNone(self._match("youknowwhttt2026_09_15_x.jpg"))
        self.assertIsNone(self._match("clrnvt2026_08_24_x.jpg"))


if __name__ == "__main__":
    unittest.main()
