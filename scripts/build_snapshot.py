#!/usr/bin/env python3
"""Build the FXMacroData open sample datasets from the public REST API.

Usage:
    FXMACRODATA_API_KEY=... python scripts/build_snapshot.py [--cutoff YYYY-MM-DD]
    python scripts/build_snapshot.py --stub      # schema-only datapackage, no network

The key is read from the FXMACRODATA_API_KEY environment variable, sent only in
the X-API-Key header, and never printed. Redirects are refused so the header
cannot be forwarded to another host.

Output is deterministic for a given cutoff and API state: rows are sorted,
numbers use the shortest round-trip representation, timestamps are ISO 8601
UTC with a trailing Z, files use LF line endings, and datapackage.json carries
no build-time timestamp.

Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

DEFAULT_BASE_URL = "https://api.fxmacrodata.com/v1"
ALLOWED_HOST = "api.fxmacrodata.com"
API_KEY_ENV = "FXMACRODATA_API_KEY"
PAGE_LIMIT = 100
MIN_CUTOFF_AGE_DAYS = 90
DEFAULT_HISTORY_START = "2000-01-01"
MAX_PAGES = 5000
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
USER_AGENT = "fxmacrodata-datasets-build/1.0"

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "datasets"
REGISTRY_PATH = REPO_ROOT / "fxmacrodata_datasets" / "registry.json"

SITE_URL = (
    "https://fxmacrodata.com/?utm_source=github&utm_medium=referral"
    "&utm_campaign=fxmacrodata-datasets&utm_content=datapackage"
)
ATTRIBUTION = "Data: FXMacroData, https://fxmacrodata.com"

# (indicator slug, extra query parameters, unit override when the parameters
# change the unit the catalogue reports for the default series)
US_MACRO_SERIES: list[tuple[str, dict[str, str], str | None]] = [
    ("inflation", {}, None),
    ("core_inflation", {}, None),
    ("non_farm_payrolls_change", {}, None),
    ("unemployment", {}, None),
    ("gdp", {"basis": "real", "frequency": "qoq", "annualization": "saar"}, "% QoQ SAAR"),
    ("pce", {}, None),
    ("core_pce", {}, None),
    ("retail_sales", {}, None),
]

G10_CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SEK", "NOK"]


def _field(name: str, ftype: str, description: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "type": ftype, "description": description}
    out.update(extra)
    return out


DATASETS: dict[str, dict[str, Any]] = {
    "us_macro_releases": {
        "path": "us_macro_releases.csv",
        "title": "US macro releases",
        "description": (
            "Headline US releases (CPI, core CPI, nonfarm payrolls change, unemployment rate, "
            "real GDP growth, PCE and core PCE inflation, retail sales) with the UTC time each "
            "value was announced. Values are the latest published vintage; timestamps are the "
            "original release times."
        ),
        "primaryKey": ["indicator", "reference_period"],
        "fields": [
            _field("reference_period", "date", "Period the value refers to (period end date)."),
            _field("indicator", "string", "FXMacroData indicator slug."),
            _field("name", "string", "Indicator name."),
            _field("value", "number", "Released value, latest vintage."),
            _field("unit", "string", "Unit of value."),
            _field("series_options", "string", "API query options used for the series, empty for the default series."),
            _field("announcement_datetime_utc", "datetime", "When the value was published, UTC."),
            _field("release_time_assumed", "boolean", "True when the release time was derived from the series' typical publication lag rather than captured from a release calendar."),
            _field("source", "string", "Official publisher."),
            _field("source_url", "string", "Link to the official publication."),
        ],
    },
    "g10_policy_rates": {
        "path": "g10_policy_rates.csv",
        "title": "G10 policy rate decisions",
        "description": (
            "Policy rate decisions for USD, EUR, GBP, JPY, CHF, CAD, AUD, NZD, SEK and NOK "
            "with the UTC announcement time of each decision."
        ),
        "primaryKey": ["currency", "decision_date"],
        "fields": [
            _field("currency", "string", "ISO 4217 currency code."),
            _field("decision_date", "date", "Date of the decision as published by the central bank."),
            _field("rate_name", "string", "Name of the policy rate."),
            _field("value", "number", "Policy rate after the decision."),
            _field("unit", "string", "Unit of value."),
            _field("previous_value", "number", "Policy rate before the decision."),
            _field("change_from_previous", "number", "value minus previous_value, percentage points."),
            _field("announcement_datetime_utc", "datetime", "When the decision was announced, UTC."),
            _field("announcement_datetime_local", "string", "Announcement time in the central bank's own timezone, ISO 8601 with offset."),
            _field("release_time_assumed", "boolean", "True when the announcement time was derived rather than captured."),
            _field("central_bank", "string", "Publishing central bank."),
            _field("source_url", "string", "Link to the official publication."),
        ],
    },
    "release_calendar_sample": {
        "path": "release_calendar_sample.csv",
        "title": "Release calendar sample, scheduled vs announced",
        "description": (
            "One calendar quarter of scheduled G10 macro releases joined to the time each "
            "value was actually announced. Built for event-study and timing demos."
        ),
        "primaryKey": ["currency", "indicator", "reference_period", "scheduled_datetime_utc"],
        "fields": [
            _field("currency", "string", "ISO 4217 currency code."),
            _field("indicator", "string", "FXMacroData indicator slug."),
            _field("name", "string", "Release name."),
            _field("reference_period", "date", "Period the release refers to."),
            _field("scheduled_datetime_utc", "datetime", "Scheduled release time from the official calendar, UTC."),
            _field("release_date_confirmed", "boolean", "True when the publisher confirmed the date."),
            _field("time_announced", "boolean", "True when the publisher announced a time of day."),
            _field("event_importance", "string", "low, medium or high."),
            _field("announcement_datetime_utc", "datetime", "When the value was announced, UTC. Empty when no matching release was found."),
            _field("seconds_from_schedule", "integer", "announcement_datetime_utc minus scheduled_datetime_utc, seconds."),
            _field("release_time_assumed", "boolean", "True when the announcement time was derived rather than captured."),
            _field("value", "number", "Released value, latest vintage."),
            _field("calendar_source_url", "string", "Official calendar the schedule came from."),
            _field("source_url", "string", "Link to the official publication."),
        ],
    },
}


class ApiError(RuntimeError):
    """Raised for any response that should stop the build."""


class NotFound(ApiError):
    """Raised for a 404, which some callers treat as an empty series."""


Transport = Callable[[str, dict[str, str], float], tuple[int, bytes, dict[str, str]]]


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # urllib then raises HTTPError with the 3xx status


def urllib_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes, dict[str, str]]:
    opener = urllib.request.build_opener(_RefuseRedirects)
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read() or b""
        except Exception:  # pragma: no cover - defensive
            pass
        return exc.code, body, dict((exc.headers or {}).items())


class Client:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 60.0,
        pause: float = 0.25,
    ) -> None:
        if not api_key:
            raise ApiError(f"{API_KEY_ENV} is not set")
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
            raise ApiError(f"base URL must be https://{ALLOWED_HOST}/...")
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._transport = transport or urllib_transport
        self._sleep = sleep
        self._timeout = timeout
        self._pause = pause
        self.requests = 0

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = urllib.parse.urlencode(sorted((k, str(v)) for k, v in (params or {}).items()))
        url = f"{self._base}{path}" + (f"?{query}" if query else "")
        headers = {"X-API-Key": self._key, "Accept": "application/json", "User-Agent": USER_AGENT}
        attempt = 0
        while True:
            attempt += 1
            if self._pause:
                self._sleep(self._pause)
            try:
                status, body, resp_headers = self._transport(url, headers, self._timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt <= MAX_RETRIES:
                    self._sleep(min(2 ** attempt, 30))
                    continue
                raise ApiError(self._redact(f"GET {path}: network error {type(exc).__name__}")) from None
            self.requests += 1
            if status in RETRY_STATUSES and attempt <= MAX_RETRIES:
                self._sleep(_retry_delay(resp_headers, attempt))
                continue
            if 300 <= status < 400:
                raise ApiError(f"GET {path}: refused redirect (HTTP {status})")
            payload = _decode(body)
            code = _error_code(payload)
            if status == 404:
                raise NotFound(self._redact(f"GET {path}: not found ({code or 'no detail'})"))
            if status in (401, 403):
                raise ApiError(
                    f"GET {path}: HTTP {status} {code or ''}".rstrip()
                    + f". Check that {API_KEY_ENV} holds a valid, active key."
                )
            if status != 200:
                raise ApiError(self._redact(f"GET {path}: HTTP {status} {code or ''}".rstrip()))
            if not isinstance(payload, dict):
                raise ApiError(f"GET {path}: response is not a JSON object")
            if code:
                raise ApiError(self._redact(f"GET {path}: error in 200 response ({code})"))
            return payload

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        offset = 0
        for _ in range(MAX_PAGES):
            page_params = dict(params or {})
            page_params.update({"limit": PAGE_LIMIT, "offset": offset})
            payload = self.get_json(path, page_params)
            window = payload.get("freemium_window") or {}
            if isinstance(window, dict) and window.get("applied"):
                raise ApiError(
                    f"GET {path}: response is limited to the anonymous window; the key was not accepted"
                )
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise ApiError(f"GET {path}: response has no data list")
            for row in rows:
                if not isinstance(row, dict):
                    raise ApiError(f"GET {path}: non-object row")
                yield row
            pagination = payload.get("pagination") or {}
            if not pagination.get("has_more"):
                return
            next_offset = pagination.get("next_offset")
            if next_offset is None:
                next_offset = offset + len(rows)
            if not isinstance(next_offset, int) or next_offset <= offset or not rows:
                raise ApiError(f"GET {path}: pagination did not advance at offset {offset}")
            offset = next_offset
        raise ApiError(f"GET {path}: more than {MAX_PAGES} pages")


def _decode(body: bytes) -> Any:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _error_code(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("error", "code"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value[:80]
        if isinstance(value, dict):
            inner = value.get("code") or value.get("type") or "error"
            return str(inner)[:80]
    return None


def _retry_delay(headers: dict[str, str], attempt: int) -> float:
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return max(0.0, min(float(value), 60.0))
            except ValueError:
                break
    return float(min(2 ** attempt, 30))


# ---------------------------------------------------------------- formatting


def fmt_num(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    if isinstance(value, str):
        try:
            return fmt_num(float(value))
        except ValueError:
            return ""
    return ""


def fmt_dt(epoch: Any) -> str:
    if epoch is None or isinstance(epoch, bool):
        return ""
    try:
        seconds = int(epoch)
    except (TypeError, ValueError):
        return ""
    return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_bool(value: Any) -> str:
    if value is None:
        return ""
    return "true" if value is True else "false"


def _epoch(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_utc_to_epoch(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp())


def _date(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        return dt.date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return ""


def end_of_day_epoch(day: dt.date) -> int:
    return int(dt.datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp())


def default_cutoff(today: dt.date) -> dt.date:
    """Last calendar quarter end at least MIN_CUTOFF_AGE_DAYS before today."""
    limit = today - dt.timedelta(days=MIN_CUTOFF_AGE_DAYS)
    quarter_start_month = 3 * ((limit.month - 1) // 3) + 1
    quarter_end = _quarter_end(limit.year, quarter_start_month)
    if quarter_end <= limit:
        return quarter_end
    return dt.date(limit.year, quarter_start_month, 1) - dt.timedelta(days=1)


def _quarter_end(year: int, quarter_start_month: int) -> dt.date:
    end_month = quarter_start_month + 2
    if end_month == 12:
        return dt.date(year, 12, 31)
    return dt.date(year, end_month + 1, 1) - dt.timedelta(days=1)


def quarter_window(cutoff: dt.date) -> tuple[dt.date, dt.date]:
    """The calendar quarter that ends on or immediately before the cutoff."""
    start_month = 3 * ((cutoff.month - 1) // 3) + 1
    end = _quarter_end(cutoff.year, start_month)
    if end > cutoff:
        end = dt.date(cutoff.year, start_month, 1) - dt.timedelta(days=1)
        start_month = 3 * ((end.month - 1) // 3) + 1
        return dt.date(end.year, start_month, 1), end
    return dt.date(cutoff.year, start_month, 1), end


def write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, str]]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buffer.getvalue().encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- builders


def _catalogue_entry(client: Client, currency: str, indicator: str, cache: dict[str, dict]) -> dict[str, Any]:
    key = currency.lower()
    if key not in cache:
        cache[key] = client.get_json(f"/data_catalogue/{key}")
    entry = cache[key].get(indicator)
    return entry if isinstance(entry, dict) else {}


def build_us_macro_releases(client: Client, start: dt.date, cutoff: dt.date, catalogue: dict) -> list[dict[str, str]]:
    cutoff_epoch = end_of_day_epoch(cutoff)
    out: dict[tuple[str, str], dict[str, str]] = {}
    for indicator, options, unit_override in US_MACRO_SERIES:
        meta = _catalogue_entry(client, "usd", indicator, catalogue)
        params = {"start_date": start.isoformat(), "end_date": cutoff.isoformat(), **options}
        kept = 0
        for row in client.paginate(f"/announcements/usd/{indicator}", params):
            period = _date(row.get("date"))
            announced = _epoch(row.get("announcement_datetime"))
            if not period or period > cutoff.isoformat():
                continue
            if announced is not None and announced > cutoff_epoch:
                continue
            value = fmt_num(row.get("val"))
            if value == "":
                continue
            key = (indicator, period)
            if key in out:
                continue  # rows arrive most recent first; keep the first seen
            out[key] = {
                "reference_period": period,
                "indicator": indicator,
                "name": str(meta.get("name") or ""),
                "value": value,
                "unit": unit_override or str(meta.get("unit") or ""),
                "series_options": ";".join(f"{k}={v}" for k, v in sorted(options.items())),
                "announcement_datetime_utc": fmt_dt(announced),
                "release_time_assumed": fmt_bool(row.get("release_time_assumed") is True),
                "source": str(row.get("source") or meta.get("source") or ""),
                "source_url": str(row.get("source_url") or ""),
            }
            kept += 1
        _log(f"us_macro_releases {indicator}: {kept} rows")
    return [out[k] for k in sorted(out)]


def build_g10_policy_rates(client: Client, start: dt.date, cutoff: dt.date, catalogue: dict) -> list[dict[str, str]]:
    cutoff_epoch = end_of_day_epoch(cutoff)
    out: dict[tuple[str, str], dict[str, str]] = {}
    for currency in G10_CURRENCIES:
        meta = _catalogue_entry(client, currency, "policy_rate", catalogue)
        params = {"start_date": start.isoformat(), "end_date": cutoff.isoformat()}
        kept = 0
        for row in client.paginate(f"/announcements/{currency.lower()}/policy_rate", params):
            decision = _date(row.get("date"))
            announced = _epoch(row.get("announcement_datetime"))
            if not decision or decision > cutoff.isoformat():
                continue
            if announced is not None and announced > cutoff_epoch:
                continue
            value = fmt_num(row.get("val"))
            if value == "":
                continue
            key = (currency, decision)
            if key in out:
                continue
            local = row.get("announcement_datetime_local")
            out[key] = {
                "currency": currency,
                "decision_date": decision,
                "rate_name": str(meta.get("name") or ""),
                "value": value,
                "unit": str(meta.get("unit") or ""),
                "previous_value": fmt_num(row.get("previous_value")),
                "change_from_previous": fmt_num(row.get("change_from_previous")),
                "announcement_datetime_utc": fmt_dt(announced),
                "announcement_datetime_local": local if isinstance(local, str) else "",
                "release_time_assumed": fmt_bool(row.get("release_time_assumed") is True),
                "central_bank": str(row.get("source") or meta.get("source") or ""),
                "source_url": str(row.get("source_url") or ""),
            }
            kept += 1
        _log(f"g10_policy_rates {currency}: {kept} rows")
    return [out[k] for k in sorted(out)]


def build_release_calendar_sample(
    client: Client, window: tuple[dt.date, dt.date], cutoff: dt.date, notes: list[str]
) -> list[dict[str, str]]:
    start, end = window
    cutoff_epoch = end_of_day_epoch(cutoff)
    lookback = start - dt.timedelta(days=550)
    out: dict[tuple[str, str, str, str], dict[str, str]] = {}
    for currency in G10_CURRENCIES:
        payload = client.get_json(
            f"/calendar/{currency.lower()}", {"start_date": start.isoformat(), "end_date": end.isoformat()}
        )
        if payload.get("history_truncated"):
            notes.append(f"{currency} calendar history starts {payload.get('history_start_date')}")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ApiError(f"calendar {currency}: response has no data list")
        announcements: dict[str, dict[str, dict]] = {}
        kept = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            indicator = row.get("release")
            scheduled = _epoch(row.get("announcement_datetime"))
            if scheduled is None:
                scheduled = _iso_utc_to_epoch(row.get("announcement_datetime_utc"))
            period = _date(row.get("date"))
            if not isinstance(indicator, str) or not indicator or scheduled is None:
                continue
            if scheduled > cutoff_epoch:
                continue
            if indicator not in announcements:
                announcements[indicator] = _announcements_by_period(client, currency, indicator, lookback, end)
            released = announcements[indicator].get(period, {})
            actual = _epoch(released.get("announcement_datetime"))
            if actual is not None and actual > cutoff_epoch:
                released, actual = {}, None
            key = (fmt_dt(scheduled), currency, indicator, period)
            if key in out:
                continue
            out[key] = {
                "currency": currency,
                "indicator": indicator,
                "name": str(row.get("name") or ""),
                "reference_period": period,
                "scheduled_datetime_utc": fmt_dt(scheduled),
                "release_date_confirmed": fmt_bool(row.get("release_date_confirmed") if isinstance(row.get("release_date_confirmed"), bool) else None),
                "time_announced": fmt_bool(row.get("time_announced") if isinstance(row.get("time_announced"), bool) else None),
                "event_importance": str(row.get("event_importance") or ""),
                "announcement_datetime_utc": fmt_dt(actual),
                "seconds_from_schedule": str(actual - scheduled) if actual is not None else "",
                "release_time_assumed": fmt_bool(released.get("release_time_assumed") is True) if released else "",
                "value": fmt_num(released.get("val")) if released else "",
                "calendar_source_url": str(row.get("source_url") or ""),
                "source_url": str(released.get("source_url") or "") if released else "",
            }
            kept += 1
        _log(f"release_calendar_sample {currency}: {kept} rows")
    return [out[k] for k in sorted(out)]


def _announcements_by_period(
    client: Client, currency: str, indicator: str, start: dt.date, end: dt.date
) -> dict[str, dict]:
    by_period: dict[str, dict] = {}
    try:
        rows = list(
            client.paginate(
                f"/announcements/{currency.lower()}/{urllib.parse.quote(indicator, safe='')}",
                {"start_date": start.isoformat(), "end_date": end.isoformat()},
            )
        )
    except NotFound:
        return by_period
    for row in rows:
        period = _date(row.get("date"))
        if period and period not in by_period:
            by_period[period] = row
    return by_period


# ---------------------------------------------------------------- packaging


def datapackage(
    version: str,
    cutoff: dt.date | None,
    resources: dict[str, dict[str, Any]],
    status: str,
    notes: list[str],
) -> dict[str, Any]:
    package: dict[str, Any] = {
        "profile": "tabular-data-package",
        "name": "fxmacrodata-datasets",
        "title": "FXMacroData open sample datasets",
        "description": (
            "Small, openly licensed historical snapshots of official macroeconomic releases "
            "and central bank decisions with release timestamps, for tutorials, event studies "
            "and look-ahead-safe backtests."
        ),
        "version": version,
        "x_status": status,
        "x_snapshot_cutoff": cutoff.isoformat() if cutoff else None,
        "x_attribution": ATTRIBUTION,
        "homepage": "https://github.com/fxmacrodata/fxmacrodata-datasets",
        "licenses": [
            {
                "name": "CC-BY-4.0",
                "title": "Creative Commons Attribution 4.0 International",
                "path": "https://creativecommons.org/licenses/by/4.0/",
            }
        ],
        "sources": [{"title": "FXMacroData", "path": SITE_URL}],
        "contributors": [{"title": "FXMacroData", "role": "publisher", "email": "info@fxmacrodata.com"}],
        "resources": [],
    }
    if notes:
        package["x_notes"] = sorted(set(notes))
    for name, spec in DATASETS.items():
        resource: dict[str, Any] = {
            "name": name,
            "path": spec["path"],
            "title": spec["title"],
            "description": spec["description"],
            "format": "csv",
            "mediatype": "text/csv",
            "encoding": "utf-8",
            "dialect": {"delimiter": ",", "lineTerminator": "\n", "header": True},
            "licenses": package["licenses"],
            "schema": {"fields": spec["fields"], "primaryKey": spec["primaryKey"]},
        }
        resource.update(resources.get(name, {}))
        package["resources"].append(resource)
    return package


def registry(version: str, status: str, resources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    datasets = {}
    for name, spec in DATASETS.items():
        info = resources.get(name, {})
        datasets[name] = {
            "path": f"datasets/{spec['path']}",
            "sha256": (info.get("hash") or "").removeprefix("sha256:") or None,
            "rows": info.get("x_rows"),
            "bytes": info.get("bytes"),
            "fields": [[f["name"], f["type"]] for f in spec["fields"]],
        }
    return {
        "version": version,
        "tag": f"data-v{version}" if status == "published" else None,
        "status": status,
        "attribution": ATTRIBUTION,
        "license": "CC-BY-4.0",
        "datasets": datasets,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n").encode("utf-8"))


def _log(message: str) -> None:
    print(message, file=sys.stderr)


def run(argv: list[str] | None = None, transport: Transport | None = None, today: dt.date | None = None,
        sleep: Callable[[float], None] = time.sleep) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cutoff", help="Snapshot cutoff date YYYY-MM-DD (default: last quarter end at least 90 days ago)")
    parser.add_argument("--start", default=DEFAULT_HISTORY_START, help="History start date for the long datasets")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--registry", default=str(REGISTRY_PATH))
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--stub", action="store_true", help="Write schema-only metadata without calling the API")
    parser.add_argument("--allow-recent-cutoff", action="store_true",
                        help=f"Allow a cutoff less than {MIN_CUTOFF_AGE_DAYS} days old")
    args = parser.parse_args(argv)

    today = today or dt.datetime.now(dt.timezone.utc).date()
    out_dir = Path(args.out_dir)
    registry_path = Path(args.registry)

    if args.stub:
        write_json(out_dir / "datapackage.json", datapackage("0.0.0", None, {}, "pending", []))
        write_json(registry_path, registry("0.0.0", "pending", {}))
        _log("wrote schema-only datapackage.json and registry (status: pending)")
        return 0

    try:
        cutoff = dt.date.fromisoformat(args.cutoff) if args.cutoff else default_cutoff(today)
        start = dt.date.fromisoformat(args.start)
    except ValueError as exc:
        parser.error(str(exc))
    if (today - cutoff).days < MIN_CUTOFF_AGE_DAYS and not args.allow_recent_cutoff:
        parser.error(f"cutoff {cutoff} is less than {MIN_CUTOFF_AGE_DAYS} days old")
    if start >= cutoff:
        parser.error("--start must be before the cutoff")

    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        _log(f"{API_KEY_ENV} is not set; nothing fetched. Use --stub for schema-only output.")
        return 2

    try:
        client = Client(api_key, base_url=args.base_url, transport=transport, sleep=sleep)
        catalogue: dict[str, dict] = {}
        notes: list[str] = []
        window = quarter_window(cutoff)
        tables = {
            "us_macro_releases": build_us_macro_releases(client, start, cutoff, catalogue),
            "g10_policy_rates": build_g10_policy_rates(client, start, cutoff, catalogue),
            "release_calendar_sample": build_release_calendar_sample(client, window, cutoff, notes),
        }
    except ApiError as exc:
        _log(f"build failed: {exc}")
        return 1

    notes.append(f"release_calendar_sample window {window[0].isoformat()} to {window[1].isoformat()}")
    resources: dict[str, dict[str, Any]] = {}
    for name, rows in tables.items():
        if not rows:
            _log(f"build failed: {name} has no rows")
            return 1
        spec = DATASETS[name]
        path = out_dir / spec["path"]
        write_csv(path, [f["name"] for f in spec["fields"]], rows)
        resources[name] = {"bytes": path.stat().st_size, "hash": f"sha256:{sha256_file(path)}", "x_rows": len(rows)}
        _log(f"{name}: {len(rows)} rows, {resources[name]['bytes']} bytes")

    version = cutoff.strftime("%Y.%m.%d")
    write_json(out_dir / "datapackage.json", datapackage(version, cutoff, resources, "published", notes))
    write_json(registry_path, registry(version, "published", resources))
    _log(f"done: version {version}, {client.requests} API requests")
    return 0


if __name__ == "__main__":
    sys.exit(run())
