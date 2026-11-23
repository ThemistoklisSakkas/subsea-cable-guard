"""Pseudonymous vessel identifiers for every displayed or published output.

A keyed hash (HMAC-SHA256) with a secret salt maps an MMSI to a short stable ID.
Without the salt nobody can recompute the mapping, even by hashing all ~1 billion
possible MMSIs - which a plain unsalted hash would allow in minutes.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path

import polars as pl

from cableguard.config import ROOT

log = logging.getLogger(__name__)


def vessel_id(mmsi: int, salt: str, prefix: str = "V") -> str:
    digest = hmac.new(salt.encode(), str(int(mmsi)).encode(), hashlib.sha256).hexdigest()
    return f"{prefix}-{digest[:8]}"


def vessel_ids(mmsi: pl.Series, salt: str, prefix: str = "V") -> pl.Series:
    return pl.Series([vessel_id(m, salt, prefix) for m in mmsi.to_list()], dtype=pl.Utf8)


def get_salt(root: Path = ROOT) -> str:
    """MMSI_HASH_SALT from .env / environment; otherwise a random salt kept in data/.

    The fallback file lives in the git-ignored data folder, so IDs stay stable between
    runs on this machine but are never published.
    """
    from dotenv import load_dotenv
    load_dotenv(root / ".env", override=False)
    salt = os.environ.get("MMSI_HASH_SALT", "").strip()
    if salt:
        return salt
    path = root / "data" / "mmsi_salt.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_hex(32)
    path.write_text(salt, encoding="utf-8")
    log.warning("MMSI_HASH_SALT not set: created a private random salt in %s", path)
    return salt
