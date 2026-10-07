from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
import urllib.parse
import urllib.request
from importlib import resources
from pathlib import Path
from typing import Any, Callable

ATTRIBUTION = "Data: FXMacroData, https://fxmacrodata.com"
RAW_BASE_URL = "https://raw.githubusercontent.com/fxmacrodata/fxmacrodata-datasets"
CACHE_ENV = "FXMACRODATA_DATASETS_CACHE"
MAX_BYTES = 50 * 1024 * 1024

Downloader = Callable[[str, float], bytes]


class DataPendingError(RuntimeError):
    """The pinned registry has no published data for this dataset yet."""


class ChecksumError(RuntimeError):
    """A downloaded file did not match the SHA-256 pinned in the registry."""


def _registry() -> dict[str, Any]:
    text = resources.files(__package__).joinpath("registry.json").read_text(encoding="utf-8")
    return json.loads(text)


def version() -> str:
    """Snapshot version this package is pinned to."""
    return str(_registry()["version"])


def list_datasets() -> list[str]:
    return sorted(_registry()["datasets"])


def cache_dir() -> Path:
    override = os.environ.get(CACHE_ENV)
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "fxmacrodata-datasets"


def _download(url: str, timeout: float) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("refusing a non-https download URL")
    request = urllib.request.Request(url, headers={"User-Agent": "fxmacrodata-datasets"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("download exceeds the size limit")
    return data


def _entry(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    reg = _registry()
    datasets = reg["datasets"]
    if name not in datasets:
        raise KeyError(f"unknown dataset {name!r}; available: {', '.join(sorted(datasets))}")
    entry = datasets[name]
    if reg.get("status") != "published" or not entry.get("sha256") or not reg.get("tag"):
        raise DataPendingError(
            f"{name} has not been published for this package version yet; "
            "see https://github.com/fxmacrodata/fxmacrodata-datasets"
        )
    return reg, entry


def fetch(
    name: str,
    *,
    cache: Path | str | None = None,
    base_url: str = RAW_BASE_URL,
    downloader: Downloader | None = None,
    timeout: float = 60.0,
) -> Path:
    """Return a local path to the verified CSV for ``name``, downloading if needed."""
    reg, entry = _entry(name)
    expected = entry["sha256"].lower()
    root = Path(cache) if cache is not None else cache_dir()
    target = root / reg["tag"] / Path(entry["path"]).name
    if target.is_file() and _sha256(target.read_bytes()) == expected:
        return target

    url = f"{base_url.rstrip('/')}/{urllib.parse.quote(reg['tag'])}/{entry['path']}"
    data = (downloader or _download)(url, timeout)
    actual = _sha256(data)
    if actual != expected:
        raise ChecksumError(f"{name}: sha256 {actual} does not match pinned {expected}")

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=".csv")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return target


def load(name: str, as_frame: bool = True, **kwargs: Any):
    """Load a dataset as a pandas DataFrame, or a list of typed dicts.

    Keyword arguments are passed to :func:`fetch` (``cache``, ``base_url``,
    ``downloader``, ``timeout``).
    """
    path = fetch(name, **kwargs)
    _, entry = _entry(name)
    types = {field: ftype for field, ftype in entry["fields"]}
    if as_frame:
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ImportError("as_frame=True needs pandas; install it or pass as_frame=False") from exc
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        for field, ftype in types.items():
            if field not in frame:
                continue
            column = frame[field].replace("", None)
            if ftype in ("number", "integer"):
                frame[field] = pd.to_numeric(column)
            elif ftype == "date":
                frame[field] = pd.to_datetime(column, format="%Y-%m-%d")
            elif ftype == "datetime":
                frame[field] = pd.to_datetime(column, format="%Y-%m-%dT%H:%M:%SZ", utc=True)
            elif ftype == "boolean":
                frame[field] = column.map({"true": True, "false": False}).astype("boolean")
        frame.attrs["attribution"] = ATTRIBUTION
        frame.attrs["version"] = version()
        return frame
    with path.open(encoding="utf-8", newline="") as handle:
        return [{k: _convert(v, types.get(k, "string")) for k, v in row.items()} for row in csv.DictReader(handle)]


def _convert(value: str, ftype: str) -> Any:
    if value == "":
        return None
    if ftype == "integer":
        return int(value)
    if ftype == "number":
        return float(value)
    if ftype == "boolean":
        return value == "true"
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
