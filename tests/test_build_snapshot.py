import contextlib
import datetime as dt
import hashlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_snapshot as bs  # noqa: E402

KEY = "not-a-real-key"
CUTOFF = dt.date(2025, 6, 30)
TODAY = dt.date(2025, 10, 7)


def epoch(text):
    return int(dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc).timestamp())


class FakeApi:
    """Routes requests by path and paginates like the public API."""

    def __init__(self, page_size=100):
        self.page_size = page_size
        self.calls = []
        self.headers_seen = []
        self.overrides = {}
        self.series = {}
        for indicator, _, _ in bs.US_MACRO_SERIES:
            self.series[("usd", indicator)] = self._monthly(indicator, 230)
        for ccy in bs.G10_CURRENCIES:
            self.series[(ccy.lower(), "policy_rate")] = self._decisions(ccy)

    def _monthly(self, indicator, count):
        rows = []
        year, month = 2006, 1
        for i in range(count):
            period = (dt.date(year, month, 1) + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
            announced = epoch(f"{(period + dt.timedelta(days=12)).isoformat()}T12:30:00")
            rows.append({
                "date": period.isoformat(),
                "val": round(1.5 + (i % 7) * 0.1, 1),
                "announcement_datetime": announced,
                "source": "BLS",
                "source_url": f"https://example.gov/{indicator}/{period.isoformat()}",
                **({"release_time_assumed": True} if i < 3 else {}),
            })
            month += 1
            if month == 13:
                year, month = year + 1, 1
        return list(reversed(rows))  # most recent first

    def _decisions(self, ccy):
        rows = []
        for i, day in enumerate(["2024-03-20", "2024-09-18", "2025-06-18", "2025-06-30", "2025-07-30"]):
            rows.append({
                "date": day,
                "val": 4.0 - 0.25 * i,
                "previous_value": 4.25 - 0.25 * i,
                "change_from_previous": -0.25,
                "announcement_datetime": epoch(f"{day}T18:00:00") if day != "2025-06-30" else epoch("2025-07-01T01:00:00"),
                "announcement_datetime_local": f"{day}T14:00:00-04:00",
                "source": f"{ccy} central bank",
                "source_url": f"https://example.org/{ccy}/{day}",
            })
        return list(reversed(rows))

    def __call__(self, url, headers, timeout):
        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        path = parsed.path.removeprefix("/v1")
        self.calls.append((path, params))
        self.headers_seen.append(dict(headers))
        if path in self.overrides:
            result = self.overrides[path]
            if callable(result):
                return result(params)
            return result
        parts = path.strip("/").split("/")
        if parts[0] == "data_catalogue":
            body = {
                "inflation": {"name": "Inflation (CPI)", "unit": "%YoY", "source": "BLS"},
                "gdp": {"name": "GDP", "unit": "USD bn", "source": "BEA"},
                "policy_rate": {"name": f"{parts[1].upper()} Policy Rate", "unit": "%", "source": "CB"},
            }
            return 200, json.dumps(body).encode(), {}
        if parts[0] == "announcements":
            rows = self.series.get((parts[1], parts[2]))
            if rows is None:
                return 404, b'{"error":"not_found"}', {}
            limit = int(params.get("limit", 20))
            offset = int(params.get("offset", 0))
            page = rows[offset: offset + min(limit, self.page_size)]
            has_more = offset + len(page) < len(rows)
            pagination = {"limit": limit, "offset": offset, "returned_count": len(page),
                          "total_count": len(rows), "has_more": has_more}
            if has_more:
                pagination["next_offset"] = offset + len(page)
            return 200, json.dumps({"data": page, "pagination": pagination}).encode(), {}
        if parts[0] == "calendar":
            ccy = parts[1]
            data = [
                {"release": "policy_rate", "date": "2025-06-18", "announcement_datetime": epoch("2025-06-18T18:00:00"),
                 "name": "Policy rate", "release_date_confirmed": True, "time_announced": True,
                 "event_importance": "high", "source_url": "https://example.org/calendar"},
                {"release": "missing_series", "date": "2025-05-31", "announcement_datetime": epoch("2025-06-10T12:30:00"),
                 "name": "Other", "release_date_confirmed": True, "time_announced": False,
                 "event_importance": "low", "source_url": "https://example.org/calendar"},
                {"release": "policy_rate", "date": "2025-07-30", "announcement_datetime": epoch("2025-07-30T18:00:00"),
                 "name": "Policy rate", "release_date_confirmed": True, "time_announced": True,
                 "event_importance": "high", "source_url": "https://example.org/calendar"},
            ]
            return 200, json.dumps({"currency": ccy.upper(), "data": data}).encode(), {}
        return 404, b"{}", {}


def run_build(api, out_dir, argv=(), env_key=KEY):
    env = {bs.API_KEY_ENV: env_key} if env_key else {}
    stderr = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stderr(stderr):
        if not env_key:
            os.environ.pop(bs.API_KEY_ENV, None)
        code = bs.run(
            ["--out-dir", str(out_dir), "--registry", str(Path(out_dir) / "registry.json"), *argv],
            transport=api, today=TODAY, sleep=lambda s: None,
        )
    return code, stderr.getvalue()


class DateHelpers(unittest.TestCase):
    def test_default_cutoff_is_quarter_end_at_least_90_days_back(self):
        self.assertEqual(bs.default_cutoff(dt.date(2026, 10, 7)), dt.date(2026, 6, 30))
        self.assertEqual(bs.default_cutoff(dt.date(2026, 9, 28)), dt.date(2026, 6, 30))
        self.assertEqual(bs.default_cutoff(dt.date(2026, 9, 27)), dt.date(2026, 3, 31))
        self.assertEqual(bs.default_cutoff(dt.date(2026, 3, 1)), dt.date(2025, 9, 30))

    def test_quarter_window(self):
        self.assertEqual(bs.quarter_window(dt.date(2025, 6, 30)), (dt.date(2025, 4, 1), dt.date(2025, 6, 30)))
        self.assertEqual(bs.quarter_window(dt.date(2025, 5, 15)), (dt.date(2025, 1, 1), dt.date(2025, 3, 31)))
        self.assertEqual(bs.quarter_window(dt.date(2025, 1, 10)), (dt.date(2024, 10, 1), dt.date(2024, 12, 31)))

    def test_formatting_is_stable(self):
        self.assertEqual(bs.fmt_num(254.0), "254")
        self.assertEqual(bs.fmt_num(0.1 + 0.2), "0.30000000000000004")
        self.assertEqual(bs.fmt_num(3.4), "3.4")
        self.assertEqual(bs.fmt_num(None), "")
        self.assertEqual(bs.fmt_num(True), "")
        self.assertEqual(bs.fmt_num(float("nan")), "")
        self.assertEqual(bs.fmt_dt(0), "1970-01-01T00:00:00Z")
        self.assertEqual(bs.fmt_dt(None), "")


class ClientHardening(unittest.TestCase):
    def client(self, transport):
        return bs.Client(KEY, transport=transport, sleep=lambda s: None)

    def test_key_only_in_header(self):
        api = FakeApi()
        self.client(api).get_json("/data_catalogue/usd")
        self.assertEqual(api.headers_seen[0]["X-API-Key"], KEY)
        self.assertNotIn(KEY, api.calls[0][0] + json.dumps(api.calls[0][1]))

    def test_rejects_foreign_or_plain_http_base_url(self):
        for url in ("http://api.fxmacrodata.com/v1", "https://example.com/v1", "https://api.fxmacrodata.com.evil.io/v1"):
            with self.assertRaises(bs.ApiError):
                bs.Client(KEY, base_url=url)

    def test_missing_key(self):
        with self.assertRaises(bs.ApiError):
            bs.Client("")

    def test_redirect_is_refused(self):
        def transport(url, headers, timeout):
            return 302, b"", {"Location": "https://elsewhere.example/"}
        with self.assertRaisesRegex(bs.ApiError, "refused redirect"):
            self.client(transport).get_json("/x")

    def test_error_in_200_body(self):
        def transport(url, headers, timeout):
            return 200, json.dumps({"error": "api_key_required", "detail": f"bad {KEY}"}).encode(), {}
        with self.assertRaises(bs.ApiError) as ctx:
            self.client(transport).get_json("/x")
        self.assertIn("api_key_required", str(ctx.exception))
        self.assertNotIn(KEY, str(ctx.exception))

    def test_non_json_200(self):
        def transport(url, headers, timeout):
            return 200, b"<html>maintenance</html>", {}
        with self.assertRaises(bs.ApiError):
            self.client(transport).get_json("/x")

    def test_retries_429_then_succeeds(self):
        responses = [(429, b"", {"Retry-After": "2"}), (503, b"", {}), (200, b'{"data": []}', {})]
        sleeps = []

        def transport(url, headers, timeout):
            return responses.pop(0)
        client = bs.Client(KEY, transport=transport, sleep=sleeps.append, pause=0)
        self.assertEqual(client.get_json("/x"), {"data": []})
        self.assertEqual(sleeps, [2.0, 4.0])

    def test_gives_up_after_retries(self):
        client = bs.Client(KEY, transport=lambda u, h, t: (503, b"", {}), sleep=lambda s: None)
        with self.assertRaisesRegex(bs.ApiError, "HTTP 503"):
            client.get_json("/x")

    def test_unauthorised_message_has_no_key(self):
        client = self.client(lambda u, h, t: (401, b'{"error":"invalid_api_key"}', {}))
        with self.assertRaises(bs.ApiError) as ctx:
            client.get_json("/x")
        self.assertNotIn(KEY, str(ctx.exception))
        self.assertIn("invalid_api_key", str(ctx.exception))

    def test_paginates_with_limit_100_and_next_offset(self):
        api = FakeApi(page_size=100)
        rows = list(self.client(api).paginate("/announcements/usd/inflation"))
        self.assertEqual(len(rows), 230)
        offsets = [(p["limit"], p["offset"]) for _, p in api.calls]
        self.assertEqual(offsets, [("100", "0"), ("100", "100"), ("100", "200")])

    def test_short_pages_still_advance(self):
        api = FakeApi(page_size=40)
        rows = list(self.client(api).paginate("/announcements/usd/inflation"))
        self.assertEqual(len(rows), 230)
        self.assertEqual(len({r["date"] for r in rows}), 230)

    def test_stalled_pagination_is_an_error(self):
        def transport(url, headers, timeout):
            body = {"data": [{"date": "2020-01-31"}], "pagination": {"has_more": True, "next_offset": 0}}
            return 200, json.dumps(body).encode(), {}
        with self.assertRaisesRegex(bs.ApiError, "did not advance"):
            list(self.client(transport).paginate("/x"))

    def test_anonymous_window_is_an_error(self):
        def transport(url, headers, timeout):
            body = {"data": [], "freemium_window": {"applied": True}, "pagination": {"has_more": False}}
            return 200, json.dumps(body).encode(), {}
        with self.assertRaisesRegex(bs.ApiError, "anonymous window"):
            list(self.client(transport).paginate("/x"))


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    seen_keys = []

    def do_GET(self):  # noqa: N802
        self.seen_keys.append(self.headers.get("X-API-Key"))
        if self.path.startswith("/start"):
            self.send_response(302)
            self.send_header("Location", "/landed")
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


class UrllibTransport(unittest.TestCase):
    def test_does_not_follow_redirects(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            _RedirectHandler.seen_keys.clear()
            status, _, headers = bs.urllib_transport(
                f"http://127.0.0.1:{server.server_port}/start", {"X-API-Key": KEY}, 5
            )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(status, 302)
        self.assertEqual(_RedirectHandler.seen_keys, [KEY])


class FullBuild(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_writes_sorted_csvs_and_matching_datapackage(self):
        api = FakeApi()
        code, log = run_build(api, self.out, ["--cutoff", CUTOFF.isoformat()])
        self.assertEqual(code, 0, log)
        self.assertNotIn(KEY, log)

        package = json.loads((self.out / "datapackage.json").read_text())
        self.assertEqual(package["version"], "2025.06.30")
        self.assertEqual(package["x_snapshot_cutoff"], "2025-06-30")
        self.assertEqual(package["licenses"][0]["name"], "CC-BY-4.0")
        registry = json.loads((self.out / "registry.json").read_text())
        self.assertEqual(registry["tag"], "data-v2025.06.30")
        self.assertEqual(registry["status"], "published")

        for resource in package["resources"]:
            path = self.out / resource["path"]
            data = path.read_bytes()
            self.assertNotIn(b"\r\n", data)
            self.assertEqual(resource["hash"], "sha256:" + hashlib.sha256(data).hexdigest())
            self.assertEqual(resource["bytes"], len(data))
            lines = data.decode().splitlines()
            self.assertEqual(lines[0].split(","), [f["name"] for f in resource["schema"]["fields"]])
            self.assertEqual(resource["x_rows"], len(lines) - 1)
            self.assertEqual(registry["datasets"][resource["name"]]["sha256"], resource["hash"][7:])

    def test_cutoff_is_look_ahead_safe(self):
        api = FakeApi()
        run_build(api, self.out, ["--cutoff", CUTOFF.isoformat()])
        rates = (self.out / "g10_policy_rates.csv").read_text().splitlines()
        usd = [line for line in rates if line.startswith("USD,")]
        # 2025-06-30 was announced after the cutoff day ended, 2025-07-30 is past the cutoff
        self.assertEqual([line.split(",")[1] for line in usd], ["2024-03-20", "2024-09-18", "2025-06-18"])
        macro = (self.out / "us_macro_releases.csv").read_text().splitlines()[1:]
        self.assertTrue(all(line.split(",")[0] <= "2025-06-30" for line in macro))
        self.assertTrue(all(line.split(",")[6] <= "2025-06-30T23:59:59Z" for line in macro))
        # sorted by indicator then period
        keys = [tuple(line.split(",")[1::-1]) for line in macro]
        self.assertEqual(keys, sorted(keys))

    def test_series_options_and_unit_override(self):
        api = FakeApi()
        run_build(api, self.out, ["--cutoff", CUTOFF.isoformat()])
        gdp_calls = [p for path, p in api.calls if path == "/announcements/usd/gdp"]
        self.assertTrue(gdp_calls)
        self.assertEqual(gdp_calls[0]["basis"], "real")
        self.assertEqual(gdp_calls[0]["frequency"], "qoq")
        self.assertEqual(gdp_calls[0]["annualization"], "saar")
        gdp = [line for line in (self.out / "us_macro_releases.csv").read_text().splitlines() if ",gdp," in line]
        self.assertIn("% QoQ SAAR", gdp[0])
        self.assertIn("annualization=saar;basis=real;frequency=qoq", gdp[0])

    def test_release_time_assumed_column(self):
        api = FakeApi()
        run_build(api, self.out, ["--cutoff", CUTOFF.isoformat()])
        lines = (self.out / "us_macro_releases.csv").read_text().splitlines()[1:]
        flags = [line.split(",")[7] for line in lines if line.split(",")[1] == "inflation"]
        self.assertEqual(flags[:3], ["true", "true", "true"])
        self.assertTrue(all(flag == "false" for flag in flags[3:]))

    def test_calendar_join(self):
        api = FakeApi()
        run_build(api, self.out, ["--cutoff", CUTOFF.isoformat()])
        import csv
        with (self.out / "release_calendar_sample.csv").open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 20)  # two in-window rows per currency, the July row is past the cutoff
        joined = [r for r in rows if r["indicator"] == "policy_rate" and r["currency"] == "EUR"][0]
        self.assertEqual(joined["announcement_datetime_utc"], "2025-06-18T18:00:00Z")
        self.assertEqual(joined["seconds_from_schedule"], "0")
        self.assertEqual(joined["value"], "3.5")
        missing = [r for r in rows if r["indicator"] == "missing_series"][0]
        self.assertEqual(missing["announcement_datetime_utc"], "")
        self.assertEqual(missing["seconds_from_schedule"], "")
        cal_calls = [p for path, p in api.calls if path.startswith("/calendar/")]
        self.assertEqual(cal_calls[0], {"start_date": "2025-04-01", "end_date": "2025-06-30"})

    def test_output_is_deterministic(self):
        first = Path(self.tmp.name) / "a"
        second = Path(self.tmp.name) / "b"
        run_build(FakeApi(page_size=100), first, ["--cutoff", CUTOFF.isoformat()])
        run_build(FakeApi(page_size=37), second, ["--cutoff", CUTOFF.isoformat()])
        for name in ("datapackage.json", "us_macro_releases.csv", "g10_policy_rates.csv",
                     "release_calendar_sample.csv", "registry.json"):
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)

    def test_api_error_fails_build_without_leaking_key(self):
        api = FakeApi()
        api.overrides["/announcements/usd/unemployment"] = (
            200, json.dumps({"error": "upstream_unavailable", "detail": KEY}).encode(), {})
        code, log = run_build(api, self.out, ["--cutoff", CUTOFF.isoformat()])
        self.assertEqual(code, 1)
        self.assertIn("upstream_unavailable", log)
        self.assertNotIn(KEY, log)
        self.assertFalse((self.out / "datapackage.json").exists())

    def test_missing_series_for_core_dataset_fails(self):
        api = FakeApi()
        del api.series[("nok", "policy_rate")]
        code, log = run_build(api, self.out, ["--cutoff", CUTOFF.isoformat()])
        self.assertEqual(code, 1)
        self.assertIn("not found", log)

    def test_requires_key(self):
        code, log = run_build(FakeApi(), self.out, ["--cutoff", CUTOFF.isoformat()], env_key=None)
        self.assertEqual(code, 2)
        self.assertIn("not set", log)

    def test_recent_cutoff_refused(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            run_build(FakeApi(), self.out, ["--cutoff", "2025-09-30"])
        code, _ = run_build(FakeApi(), self.out, ["--cutoff", "2025-09-30", "--allow-recent-cutoff"])
        self.assertEqual(code, 0)

    def test_default_cutoff_used(self):
        api = FakeApi()
        code, _ = run_build(api, self.out)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads((self.out / "datapackage.json").read_text())["x_snapshot_cutoff"], "2025-06-30")

    def test_stub_needs_no_network(self):
        def transport(url, headers, timeout):
            raise AssertionError("stub must not call the API")
        code, _ = run_build(transport, self.out, ["--stub"], env_key=None)
        self.assertEqual(code, 0)
        package = json.loads((self.out / "datapackage.json").read_text())
        self.assertEqual(package["x_status"], "pending")
        self.assertEqual({r["name"] for r in package["resources"]}, set(bs.DATASETS))


class CommittedMetadata(unittest.TestCase):
    def test_committed_datapackage_matches_registry(self):
        package = json.loads((ROOT / "datasets" / "datapackage.json").read_text())
        registry = json.loads((ROOT / "fxmacrodata_datasets" / "registry.json").read_text())
        self.assertEqual(package["version"], registry["version"])
        self.assertEqual(package["x_status"], registry["status"])
        self.assertEqual([r["name"] for r in package["resources"]], list(bs.DATASETS))
        for resource in package["resources"]:
            spec = bs.DATASETS[resource["name"]]
            self.assertEqual(resource["schema"], {"fields": spec["fields"], "primaryKey": spec["primaryKey"]},
                             "datapackage.json is stale; rerun build_snapshot.py --stub or a full build")
            entry = registry["datasets"][resource["name"]]
            self.assertEqual(entry["fields"], [[f["name"], f["type"]] for f in resource["schema"]["fields"]])
            if registry["status"] == "published":
                data = (ROOT / "datasets" / resource["path"]).read_bytes()
                self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
                self.assertLess(len(data), 5 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
