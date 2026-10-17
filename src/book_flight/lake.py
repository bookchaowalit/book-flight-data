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
from . import quality


def _load_shared():
    """Import the shared product adapter.

    Uses the installed ``solo-empire-data-lake`` package (the ``[lake]`` extra).
    Falls back to a data-lake checkout named by ``SOLO_DATA_LAKE_ROOT``.
    """
    if importlib.util.find_spec("data_lake") is None and config.SOLO_DATA_LAKE_ROOT:
        scripts = (
            Path(config.SOLO_DATA_LAKE_ROOT).expanduser().resolve()
            / "infra"
            / "scripts"
        )
        if not (scripts / "data_lake" / "product_adapter.py").is_file():
            raise ImportError(
                f"SOLO_DATA_LAKE_ROOT={config.SOLO_DATA_LAKE_ROOT} has no "
                "infra/scripts/data_lake/product_adapter.py"
            )
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
    try:
        from data_lake import product_adapter as pa  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "shared data_lake runtime not found: install the [lake] extra "
            "(pip install -e .[lake]) or set SOLO_DATA_LAKE_ROOT to a "
            "data-lake checkout"
        ) from exc
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
        lineage_filename=config.LINEAGE_FILE,
        datasets=(config.LAKE_DATASET_PRICES, config.LAKE_DATASET_HISTORY),
    )


def utc_now_iso() -> str:
    return _pa_mod().utc_now_iso()


def shared_runtime_available() -> bool:
    """Return True when the shared ``data_lake`` runtime can be imported.

    Covers the installed ``[lake]`` extra (``solo-empire-data-lake``) and the
    ``SOLO_DATA_LAKE_ROOT`` sibling-checkout fallback.
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
    # SHA-1 only derives a stable record ID (not a security control); changing it would change stored IDs.
    # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
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


def _non_negative_or_blank(value: Any) -> Any:
    """Keep finite non-negative numbers as given; blank NaN/inf/negative/text."""
    if value in ("", None):
        return value
    return value if quality.non_negative_number(value) is not None else ""


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
    departure_date = (
        departure[:10] if departure else str(flight.get("departure_date") or "")
    )
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
        "duration_hours": _non_negative_or_blank(flight.get("duration_hours", 0)),
        "stops": _non_negative_or_blank(flight.get("stops", 0)),
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
        # A fare must be a finite, positive number (NaN/inf/0/negative/text dropped).
        price = quality.finite_number(rec["price"])
        if price is None or price <= 0:
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
