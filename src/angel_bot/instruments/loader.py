"""Auto-downloader for the Angel One scrip master JSON.

Angel publishes a daily-refreshed file at:
    https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPI_ScripMaster.json

This module:
  * Downloads the file (using ``httpx`` with the same async client style as
    the rest of the bot).
  * Caches it on disk so repeated process restarts do not hit the broker.
  * Considers the cache "fresh" if it was modified less than
    ``INSTRUMENT_MASTER_MAX_AGE_HOURS`` hours ago.
  * Returns an ``InstrumentMaster`` ready to use.
  * Reports cache age + path so the dashboard can show "loaded N hours ago".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

from angel_bot.config import Settings, get_settings
from angel_bot.instruments.master import InstrumentMaster

log = structlog.get_logger(__name__)


@dataclass
class MasterStatus:
    path: str
    bytes: int
    last_modified_iso: str | None
    age_seconds: float | None
    is_fresh: bool
    source: str  # "cache" | "downloaded" | "missing"
    instruments: int = 0


def _file_age_seconds(p: Path) -> float | None:
    if not p.exists():
        return None
    return max(0.0, time.time() - p.stat().st_mtime)


def _ensure_parent(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def _path_for(settings: Settings) -> Path:
    raw = settings.instrument_master_path or settings.instrument_master_csv
    if not raw:
        raw = "./data/angel_scrip_master.json"
    return Path(raw)


def status(settings: Settings | None = None) -> MasterStatus:
    from datetime import UTC, datetime

    s = settings or get_settings()
    p = _path_for(s)
    age = _file_age_seconds(p)
    fresh = age is not None and age <= s.instrument_master_max_age_hours * 3600
    last_iso = (
        datetime.fromtimestamp(p.stat().st_mtime, UTC).isoformat() if p.exists() else None
    )
    return MasterStatus(
        path=str(p),
        bytes=p.stat().st_size if p.exists() else 0,
        last_modified_iso=last_iso,
        age_seconds=age,
        is_fresh=fresh,
        source="cache" if p.exists() else "missing",
    )


async def _download(url: str, dest: Path, timeout: float = 60.0) -> int:
    _ensure_parent(dest)
    log.info("instrument_master_download_start", url=url)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url)
        r.raise_for_status()
        body = r.content
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(body)
    tmp.replace(dest)
    log.info("instrument_master_download_done", path=str(dest), bytes=len(body))
    return len(body)


async def ensure_local_master(
    settings: Settings | None = None,
    *,
    force: bool = False,
) -> tuple[InstrumentMaster, MasterStatus]:
    """Make sure a fresh instrument master exists on disk and return both
    the parsed ``InstrumentMaster`` and a ``MasterStatus`` report.

    If ``INSTRUMENT_MASTER_AUTO_DOWNLOAD`` is true (default) and the cached
    file is missing or older than ``INSTRUMENT_MASTER_MAX_AGE_HOURS`` hours,
    we re-download from ``INSTRUMENT_MASTER_URL``.
    """
    s = settings or get_settings()
    p = _path_for(s)
    age = _file_age_seconds(p)
    is_fresh = age is not None and age <= s.instrument_master_max_age_hours * 3600

    source = "cache"
    if force or not is_fresh:
        if s.instrument_master_auto_download:
            try:
                await _download(s.instrument_master_url, p)
                source = "downloaded"
            except Exception as e:  # noqa: BLE001
                log.warning("instrument_master_download_failed", error=str(e))
                if not p.exists():
                    raise
        elif not p.exists():
            raise FileNotFoundError(
                f"Instrument master not found at {p} and AUTO_DOWNLOAD is off."
            )

    master = InstrumentMaster.from_path(p)
    st = status(s)
    st.source = source
    st.instruments = len(master)
    return master, st


def load_local_master_strict(settings: Settings | None = None) -> InstrumentMaster:
    """Synchronous strict load — fails if the file is not on disk."""
    s = settings or get_settings()
    p = _path_for(s)
    if not p.exists():
        raise FileNotFoundError(
            f"Instrument master not found at {p}. Trigger /api/instruments/refresh "
            f"or set INSTRUMENT_MASTER_AUTO_DOWNLOAD=true."
        )
    return InstrumentMaster.from_path(p)
