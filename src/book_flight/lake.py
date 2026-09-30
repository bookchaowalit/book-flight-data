"""Lake-first multi-provider adapter for book-flight-data.

Each provider response is landed separately (no merge before landing). Bronze
rows keep structured flight fields plus pointer/checksum to that provider's
raw object. HTML landings use input_format=html so exact bytes stay intact.
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, Optional

from . import config


def _load_shared():
    cur = config.PROJECT_ROOT.resolve()
    candidates = [cur, *cur.parents]
    if config.SOLO_EMPIRE_ROOT:
        # Explicit parent checkout (e.g. sibling clone) wins over the walk.
        candidates.insert(0, Path(config.SOLO_EMPIRE_ROOT).expanduser().resolve())
    for parent in candidates:
        scripts = parent / "infra" / "scripts"
        if (scripts / "data_lake" / "product_adapter.py").is_file():
            if str(scripts) not in sys.path:
                sys.path.insert(0, str(scripts))
            break
    from data_lake import product_adapter as pa  # type: ignore
    return pa


_pa = None


def _pa_mod():
    global _pa
    if _pa is None:
        _pa = _load_shared()
    return _pa


class LakeUnavailable(RuntimeError):
    pass


class LakeIngestError(RuntimeError):
    pass


def _sync_exc():
    pa = _pa_mod()
    global LakeUnavailable, LakeIngestError
    LakeUnavailable = pa.LakeUnavailable  # type: ignore
    LakeIngestError = pa.LakeIngestError  # type: ignore


def _contract():
    pa = _pa_mod()
    _sync_exc()
    return pa.LakeProductContract(
        source=config.LAKE_SOURCE,
        domain=config.LAKE_DOMAIN,
        product_schema_version=config.SCHEMA_VERSION,
        privacy_class=config.LAKE_PRIVACY_CLASS,
        retention_class=config.LAKE_RETENTION_CLASS,
        bronze_schema_version=config.LAKE_BRONZE_SCHEMA_VERSION,
        project_root=config.PROJECT_ROOT,
        data_lake_uri=config.DATA_LAKE_URI,
        solo_empire_root=config.SOLO_EMPIRE_ROOT,
        lineage_filename=config.LINEAGE_FILE,
        datasets=(config.LAKE_DATASET_PRICES, config.LAKE_DATASET_HISTORY),
    )


def utc_now_iso() -> str:
    return _pa_mod().utc_now_iso()


def find_solo_empire_root(start: Optional[Path] = None) -> Optional[Path]:
    """Return the Solo Empire root, or ``None`` when the shared adapter is absent."""
    try:
        pa = _pa_mod()
    except ImportError:
        return None
    return pa.find_solo_empire_root(
        start or config.PROJECT_ROOT,
        solo_empire_root=config.SOLO_EMPIRE_ROOT,
    )


def shared_runtime_available() -> bool:
    """Return True when the shared ``data_lake`` runtime can be imported.

    Covers the installed ``[lake]`` extra (``solo-empire-data-lake``) and the
    ``SOLO_EMPIRE_ROOT`` / parent-checkout fallback used inside Solo Empire.
    """
    if importlib.util.find_spec("data_lake") is not None:
        return True
    try:
        _pa_mod()
    except ImportError:
        return False
    return True


def default_data_lake_uri(solo_root: Optional[Path] = None) -> str:
    del solo_root
    return _pa_mod().default_data_lake_uri(
        data_lake_uri=config.DATA_LAKE_URI,
        project_root=config.PROJECT_ROOT,
        solo_empire_root=config.SOLO_EMPIRE_ROOT,
    )


_SLUG = re.compile(r"[^a-zA-Z0-9._-]+")


def _slug(value: str) -> str:
    text = _SLUG.sub("-", str(value or "").strip().lower())
    return re.sub(r"-+", "-", text).strip("-.") or "unknown"


def itinerary_fingerprint(
    *,
    airline: str = "",
    departure: str = "",
    return_at: str = "",
    duration_hours: Any = "",
    stops: Any = "",
    url: str = "",
    price: Any = "",
) -> str:
    seed = "|".join(
        [
            str(airline or ""),
            str(departure or ""),
            str(return_at or ""),
            str(duration_hours or ""),
            str(stops or ""),
            str(url or ""),
            str(price or ""),
        ]
    )
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def stable_flight_id(
    *,
    provider: str,
    origin: str,
    destination: str,
    departure_date: str,
    itinerary_id: str,
) -> str:
    """Stable record id: provider + route + travel date + itinerary."""
    parts = [
        _slug(provider),
        _slug(origin).upper(),
        _slug(destination).upper(),
        _slug(departure_date) or "na",
        _slug(itinerary_id),
    ]
    return ":".join(parts)


def normalize_flight_record(
    flight: dict[str, Any],
    *,
    provider: str,
    observed_at: Optional[str] = None,
    currency: str = "THB",
) -> dict[str, Any]:
    """Normalize a provider-specific flight dict into a Bronze-ready record.

    ``event_time`` / ``observed_at`` is when the price was seen.
    ``departure_date`` is the travel date (may differ from observation time).
    """
    observed = observed_at or utc_now_iso()
    origin = str(flight.get("origin") or "").strip().upper()
    destination = str(flight.get("destination") or "").strip().upper()
    departure = str(flight.get("departure") or flight.get("departure_date") or "")
    departure_date = departure[:10] if departure else str(flight.get("departure_date") or "")
    airline = str(flight.get("airline") or "")
    return_at = str(flight.get("return") or flight.get("return_at") or "")
    price = flight.get("price_thb", flight.get("price", ""))
    itin = str(flight.get("itinerary_id") or "").strip()
    if not itin:
        itin = itinerary_fingerprint(
            airline=airline,
            departure=departure,
            return_at=return_at,
            duration_hours=flight.get("duration_hours", ""),
            stops=flight.get("stops", ""),
            url=str(flight.get("url") or ""),
            price=price,
        )
    record_id = stable_flight_id(
        provider=provider,
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        itinerary_id=itin,
    )
    return {
        "id": record_id,
        "provider": provider,
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "departure": departure,
        "return": return_at,
        "airline": airline,
        "price": price,
        "price_thb": price,
        "currency": str(flight.get("currency") or currency),
        "duration_hours": flight.get("duration_hours", 0),
        "stops": flight.get("stops", 0),
        "url": str(flight.get("url") or ""),
        "itinerary_id": itin,
        "source_label": str(flight.get("source") or provider),
        "observed_at": observed,
        # Price observation time (not travel date).
        "event_time": observed,
        "content_type_hint": str(flight.get("content_type_hint") or "application/json"),
    }


def flight_records_from_provider(
    flights: list[dict[str, Any]],
    *,
    provider: str,
    observed_at: Optional[str] = None,
    currency: str = "THB",
    dedupe: bool = True,
) -> list[dict[str, Any]]:
    observed = observed_at or utc_now_iso()
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for flight in flights:
        if not isinstance(flight, dict):
            continue
        rec = normalize_flight_record(
            flight, provider=provider, observed_at=observed, currency=currency
        )
        if not rec["origin"] or not rec["destination"]:
            continue
        rid = rec["id"]
        if dedupe:
            if rid not in by_id:
                order.append(rid)
            by_id[rid] = rec
        else:
            order.append(rid)
            by_id[f"{rid}#{len(order)}"] = rec
    if dedupe:
        return [by_id[i] for i in order]
    return [by_id[k] for k in order]


def ingest_provider_payload(
    *,
    provider: str,
    raw: bytes,
    flights: list[dict[str, Any]],
    dataset: str,
    content_type: str = "application/json",
    input_format: str = "json",
    data_lake_uri: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    observed_at: Optional[str] = None,
    currency: str = "THB",
) -> dict[str, Any]:
    """Land one provider response (exact bytes) + Bronze rows. Never merge providers."""
    _sync_exc()
    records = flight_records_from_provider(
        flights, provider=provider, observed_at=observed_at, currency=currency
    )
    if not records:
        raise LakeIngestError(f"Provider {provider!r} produced zero flight records")

    # HTML/native: keep structured bronze payload while landing exact source bytes.
    fmt = input_format
    ctype = content_type
    if ctype.startswith("text/html") or fmt in {"html", "htm"}:
        fmt = "html"
        ctype = "text/html"

    meta = {
        "provider": provider,
        "capture_mode": "per_provider",
        "content_type": ctype,
        "flight_count": len(records),
    }
    if metadata:
        meta.update(metadata)

    return _pa_mod().ingest_to_lake(
        _contract(),
        raw=raw,
        records=records,
        dataset=dataset,
        data_lake_uri=data_lake_uri,
        metadata=meta,
        content_type=ctype,
        input_format=fmt,
        provider=provider,
    )


def write_lineage(
    result: dict[str, Any],
    *,
    dataset: str,
    data_dir: Optional[Path] = None,
    provider: str = "",
) -> Path:
    # Lineage file merges by dataset; store provider run under nested key when given.
    path = _pa_mod().write_lineage(
        _contract(),
        result,
        dataset=dataset if not provider else f"{dataset}::{provider}",
        data_dir=Path(data_dir or config.DATA_DIR),
    )
    return path


def load_lineage(data_dir: Optional[Path] = None) -> Optional[dict[str, Any]]:
    return _pa_mod().load_lineage(
        Path(data_dir or config.DATA_DIR),
        filename=config.LINEAGE_FILE,
    )


def bronze_dataset_dir(
    dataset: str,
    *,
    data_lake_uri: Optional[str] = None,
) -> Path:
    return _pa_mod().bronze_dataset_dir(
        _contract(), dataset, data_lake_uri=data_lake_uri
    )


def read_bronze_rows(
    dataset: str,
    *,
    data_lake_uri: Optional[str] = None,
    sql: str | None = None,
) -> list[dict[str, Any]]:
    _sync_exc()
    return _pa_mod().read_bronze_rows(
        _contract(), dataset, data_lake_uri=data_lake_uri, sql=sql
    )


def read_iceberg_rows(
    dataset: str,
    *,
    data_lake_uri: Optional[str] = None,
    sql: str | None = None,
) -> list[dict[str, Any]]:
    _sync_exc()
    return _pa_mod().read_iceberg_rows(
        _contract(), dataset, data_lake_uri=data_lake_uri, sql=sql
    )


def parse_payload_json(row: dict[str, Any]) -> dict[str, Any]:
    return _pa_mod().parse_payload_json(row)


def select_latest_bronze_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _pa_mod().select_latest_bronze_rows(rows)


def landing_object_bytes(raw_key: str, *, data_lake_uri: Optional[str] = None) -> bytes:
    _sync_exc()
    uri = data_lake_uri or default_data_lake_uri()
    return _pa_mod().landing_object_bytes(uri, raw_key)
