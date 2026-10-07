# FXMacroData open sample datasets

Small, openly licensed snapshots of official macroeconomic releases and central
bank decisions, each row carrying the UTC time the number was made public. They
are sized for tutorials, notebooks and test suites: every file is a plain CSV
under 5 MB with a [Frictionless Data](https://frictionlessdata.io/) `datapackage.json`
describing its schema, units, sources, licence, version and cutoff.

> **Status: data pending.** The schemas, loader and build script are in place;
> the first snapshot has not been published yet. `datasets/datapackage.json`
> lists the schemas, and `load()` raises `DataPendingError` until the first
> data tag exists.

Data: FXMacroData, https://fxmacrodata.com. Licensed CC BY 4.0.

## Datasets

| Name | What it holds | Good for |
| --- | --- | --- |
| `us_macro_releases` | US CPI, core CPI, nonfarm payrolls change, unemployment rate, real GDP growth (QoQ SAAR), PCE and core PCE inflation, retail sales, from 2000 | Event studies around release times, look-ahead-safe backtests, nowcasting demos |
| `g10_policy_rates` | Policy rate decisions for USD, EUR, GBP, JPY, CHF, CAD, AUD, NZD, SEK, NOK, from 2000 | Rate differential and carry examples, regime labels, decision-day event windows |
| `release_calendar_sample` | One calendar quarter of scheduled G10 releases joined to the time each value was actually announced | Scheduled vs actual timing, building event windows from a calendar |

### `us_macro_releases`

| Column | Type | Notes |
| --- | --- | --- |
| `reference_period` | date | Period end the value refers to |
| `indicator` | string | FXMacroData indicator slug (`inflation`, `core_inflation`, `non_farm_payrolls_change`, `unemployment`, `gdp`, `pce`, `core_pce`, `retail_sales`) |
| `name` | string | Indicator name |
| `value` | number | Latest published vintage |
| `unit` | string | `%YoY`, `%MoM`, `%`, `Thousands`, `% QoQ SAAR` |
| `series_options` | string | Query options behind the series, for example `annualization=saar;basis=real;frequency=qoq` for GDP |
| `announcement_datetime_utc` | datetime | When the value was published |
| `release_time_assumed` | boolean | `true` when the time was derived from the series' typical publication lag rather than captured from a release calendar |
| `source`, `source_url` | string | Official publisher and link to the publication |

### `g10_policy_rates`

`currency`, `decision_date`, `rate_name`, `value`, `unit`, `previous_value`,
`change_from_previous` (percentage points), `announcement_datetime_utc`,
`announcement_datetime_local` (central bank's own timezone),
`release_time_assumed`, `central_bank`, `source_url`.

### `release_calendar_sample`

`currency`, `indicator`, `name`, `reference_period`, `scheduled_datetime_utc`,
`release_date_confirmed`, `time_announced`, `event_importance`,
`announcement_datetime_utc`, `seconds_from_schedule`, `release_time_assumed`,
`value`, `calendar_source_url`, `source_url`. Rows with no matching published
value keep the schedule and leave the announcement columns empty.

The full schema with descriptions is in [`datasets/datapackage.json`](datasets/datapackage.json).

## Using the data

With the loader (standard library only; pandas optional):

```python
from fxmacrodata_datasets import load

rates = load("g10_policy_rates")                  # pandas DataFrame, typed columns
rows = load("us_macro_releases", as_frame=False)  # list of dicts, no pandas needed
```

Files are downloaded once from this repository at the data tag pinned in the
package, cached under `~/.cache/fxmacrodata-datasets` (override with
`FXMACRODATA_DATASETS_CACHE`), and checked against the SHA-256 recorded for
that tag. A file that fails the check is never written to the cache.

Without the loader, read the CSV straight from a tag:

```python
import pandas as pd

url = ("https://raw.githubusercontent.com/fxmacrodata/fxmacrodata-datasets/"
       "<tag>/datasets/g10_policy_rates.csv")
rates = pd.read_csv(url, parse_dates=["decision_date"])
```

### Point-in-time notes

* Every row in a snapshot was public by the end of the cutoff date (UTC). Rows
  announced later are excluded even if their reference period is earlier.
* `value` is the latest vintage as of the build, not the first print. Release
  timestamps are the original publication times, so the timing is safe for
  event studies; for strict real-time value backtests, treat revised series
  (GDP, payrolls, retail sales) with care.
* Filter on `release_time_assumed` when you need captured timestamps only.

## Licence and attribution

* Data in `datasets/`: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
  See [LICENSE-DATA](LICENSE-DATA). Required attribution:
  **Data: FXMacroData, https://fxmacrodata.com**
* Code: MIT, see [LICENSE](LICENSE).

If you ship or download these files in a library, keep the attribution line in
the dataset docstring or card. Citation metadata is in [CITATION.cff](CITATION.cff).

## Cutoff and versions

Each snapshot is tagged `data-vYYYY.MM.DD` after its cutoff date, which is at
least 90 days before the build. The cutoff and row counts are in
`datapackage.json` (`x_snapshot_cutoff`, `x_rows`).

## Live and updated data

These snapshots stop at their cutoff. Current releases, full history for 22
currencies, real-time release timestamps, release calendars and forecasts are
available from the [FXMacroData API](https://fxmacrodata.com/?utm_source=github&utm_medium=referral&utm_campaign=fxmacrodata-datasets&utm_content=readme).

## Rebuilding a snapshot

`scripts/build_snapshot.py` rebuilds every file from the FXMacroData REST API.
It needs an API key in `FXMACRODATA_API_KEY`, sends it only in the `X-API-Key`
header, refuses redirects, pages through results 100 rows at a time, and writes
sorted, byte-stable CSVs plus `datapackage.json` with row counts and SHA-256
hashes.

```bash
python scripts/build_snapshot.py                       # default cutoff
python scripts/build_snapshot.py --cutoff 2026-06-30   # explicit cutoff
python scripts/build_snapshot.py --stub                # schema-only metadata, no network
```

Tests use mocked HTTP and need nothing beyond the standard library:

```bash
python -m unittest discover -s tests
```
