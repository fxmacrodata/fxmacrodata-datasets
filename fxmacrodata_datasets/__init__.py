"""Loaders for the FXMacroData open sample datasets.

    >>> from fxmacrodata_datasets import load
    >>> df = load("g10_policy_rates")            # pandas DataFrame
    >>> rows = load("g10_policy_rates", as_frame=False)  # list of dicts

Files are downloaded from the GitHub release tag pinned in ``registry.json``,
cached locally, and checked against the SHA-256 recorded for that tag.

Data: FXMacroData, https://fxmacrodata.com (CC BY 4.0).
"""

from ._loader import (
    ATTRIBUTION,
    ChecksumError,
    DataPendingError,
    cache_dir,
    fetch,
    list_datasets,
    load,
    version,
)

__all__ = [
    "ATTRIBUTION",
    "ChecksumError",
    "DataPendingError",
    "cache_dir",
    "fetch",
    "list_datasets",
    "load",
    "version",
]
__version__ = "0.1.0"
