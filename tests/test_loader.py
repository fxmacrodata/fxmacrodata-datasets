import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fxmacrodata_datasets as fxd  # noqa: E402
from fxmacrodata_datasets import _loader  # noqa: E402

CSV = (
    "currency,decision_date,value,previous_value,announcement_datetime_utc,release_time_assumed,note\n"
    "EUR,2025-06-05,2,2.25,2025-06-05T12:15:00Z,false,\n"
    "USD,2025-06-18,4.5,,2025-06-18T18:00:00Z,true,held\n"
).encode()
SHA = hashlib.sha256(CSV).hexdigest()


def published_registry(sha=SHA):
    return {
        "version": "2025.06.30",
        "tag": "data-v2025.06.30",
        "status": "published",
        "datasets": {
            "g10_policy_rates": {
                "path": "datasets/g10_policy_rates.csv",
                "sha256": sha,
                "fields": [
                    ["currency", "string"],
                    ["decision_date", "date"],
                    ["value", "number"],
                    ["previous_value", "number"],
                    ["announcement_datetime_utc", "datetime"],
                    ["release_time_assumed", "boolean"],
                    ["note", "string"],
                ],
            }
        },
    }


class Downloader:
    def __init__(self, payload=CSV):
        self.payload = payload
        self.urls = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        return self.payload


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name)
        patcher = mock.patch.object(_loader, "_registry", published_registry)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_url_is_pinned_to_tag(self):
        down = Downloader()
        fxd.fetch("g10_policy_rates", cache=self.cache, downloader=down)
        self.assertEqual(
            down.urls,
            ["https://raw.githubusercontent.com/fxmacrodata/fxmacrodata-datasets/data-v2025.06.30/datasets/g10_policy_rates.csv"],
        )

    def test_cache_hit_skips_download(self):
        down = Downloader()
        first = fxd.fetch("g10_policy_rates", cache=self.cache, downloader=down)
        second = fxd.fetch("g10_policy_rates", cache=self.cache, downloader=down)
        self.assertEqual(first, second)
        self.assertEqual(len(down.urls), 1)
        self.assertEqual(first.read_bytes(), CSV)

    def test_corrupt_cache_is_replaced(self):
        down = Downloader()
        path = fxd.fetch("g10_policy_rates", cache=self.cache, downloader=down)
        path.write_bytes(b"tampered")
        fxd.fetch("g10_policy_rates", cache=self.cache, downloader=down)
        self.assertEqual(len(down.urls), 2)
        self.assertEqual(path.read_bytes(), CSV)

    def test_checksum_mismatch_raises_and_writes_nothing(self):
        down = Downloader(payload=CSV + b"extra\n")
        with self.assertRaises(fxd.ChecksumError):
            fxd.fetch("g10_policy_rates", cache=self.cache, downloader=down)
        self.assertEqual(list(self.cache.rglob("*.csv")), [])

    def test_unknown_dataset(self):
        with self.assertRaises(KeyError):
            fxd.fetch("nope", cache=self.cache, downloader=Downloader())

    def test_rows_are_typed(self):
        rows = fxd.load("g10_policy_rates", as_frame=False, cache=self.cache, downloader=Downloader())
        self.assertEqual(rows[0]["value"], 2.0)
        self.assertIsNone(rows[1]["previous_value"])
        self.assertIs(rows[1]["release_time_assumed"], True)
        self.assertIs(rows[0]["release_time_assumed"], False)
        self.assertIsNone(rows[0]["note"])
        self.assertEqual(rows[1]["decision_date"], "2025-06-18")

    @unittest.skipUnless(__import__("importlib").util.find_spec("pandas"), "pandas not installed")
    def test_frame(self):
        frame = fxd.load("g10_policy_rates", cache=self.cache, downloader=Downloader())
        self.assertEqual(len(frame), 2)
        self.assertEqual(str(frame["announcement_datetime_utc"].dt.tz), "UTC")
        self.assertEqual(frame["value"].tolist(), [2.0, 4.5])
        self.assertTrue(frame["previous_value"].isna().iloc[1])
        self.assertEqual(frame.attrs["attribution"], fxd.ATTRIBUTION)

    def test_non_https_refused(self):
        with self.assertRaises(ValueError):
            _loader._download("http://example.com/x.csv", 1)

    def test_list_and_version(self):
        self.assertEqual(fxd.list_datasets(), ["g10_policy_rates"])
        self.assertEqual(fxd.version(), "2025.06.30")


class PendingRegistry(unittest.TestCase):
    def test_pending_registry_raises_clear_error(self):
        pending = published_registry(sha=None)
        pending["status"] = "pending"
        pending["tag"] = None
        with mock.patch.object(_loader, "_registry", lambda: pending):
            with self.assertRaises(fxd.DataPendingError):
                fxd.fetch("g10_policy_rates", downloader=Downloader())

    def test_shipped_registry_lists_all_datasets(self):
        self.assertEqual(
            fxd.list_datasets(), ["g10_policy_rates", "release_calendar_sample", "us_macro_releases"]
        )

    def test_cache_dir_env_override(self):
        with mock.patch.dict("os.environ", {_loader.CACHE_ENV: "/tmp/x"}):
            self.assertEqual(fxd.cache_dir(), Path("/tmp/x"))


if __name__ == "__main__":
    unittest.main()
