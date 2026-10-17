"""Read-only store for book-flight-data (Bronze via shared product_store)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

from . import config
from . import lake


def _load_product_store():
    cur = config.PROJECT_ROOT.resolve()
    for parent in [cur, *cur.parents]:
        scripts = parent / "infra" / "scripts"
        if (scripts / "data_lake" / "product_store.py").is_file():
            if str(scripts) not in sys.path:
                sys.path.insert(0, str(scripts))
            break
    from data_lake import product_store as ps  # type: ignore

    return ps


_ps = None


def _ps_mod():
    global _ps
    if _ps is None:
        _ps = _load_product_store()
    return _ps


def storage_model_for_source_kind(source_kind: Any) -> str:
    return _ps_mod().storage_model_for_source_kind(source_kind, product="flight")


def _contract():
    from data_lake.product_adapter import LakeProductContract  # type: ignore

    return LakeProductContract(
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
    return _ps_mod().utc_now_iso()


def make_record_id(row: dict[str, Any]) -> str:
    return _ps_mod().make_record_id(
        row, id_fields=config.ID_FIELDS, id_sep=config.ID_SEP
    )


def flight_item_from_bronze(
    row: dict[str, Any],
    *,
    history: bool = False,
    history_idx: int = 0,
) -> dict[str, Any]:
    payload = lake.parse_payload_json(row)
    event_time = str(row.get("event_time") or payload.get("event_time") or "")
    item: dict[str, Any] = {
        "provider": str(payload.get("provider") or ""),
        "origin": str(payload.get("origin") or ""),
        "destination": str(payload.get("destination") or ""),
        "departure_date": str(payload.get("departure_date") or ""),
        "departure": str(payload.get("departure") or ""),
        "return": str(payload.get("return") or ""),
        "airline": str(payload.get("airline") or ""),
        "price_thb": str(payload.get("price_thb", payload.get("price", ""))),
        "currency": str(payload.get("currency") or "THB"),
        "duration_hours": str(payload.get("duration_hours", "")),
        "stops": str(payload.get("stops", "")),
        "url": str(payload.get("url") or ""),
        "source": str(payload.get("source_label") or payload.get("provider") or ""),
        "itinerary_id": str(payload.get("itinerary_id") or ""),
        "observed_at": str(payload.get("observed_at") or event_time),
        "scraped_at": event_time,
        "updated_at": event_time,
        "event_time": event_time,
        "ingest_run_id": str(row.get("ingest_run_id", "")),
        "source_record_id": str(row.get("source_record_id", "")),
        "raw_object_key": str(row.get("raw_object_key", "")),
        "content_type": str(
            row.get("content_type") or payload.get("content_type_hint") or ""
        ),
    }
    if history:
        item["record_id"] = make_record_id(item) + f"#h{history_idx}"
    else:
        item["record_id"] = make_record_id(item)
    return item


def _lake_uri() -> Optional[str]:
    if config.DATA_LAKE_URI.strip():
        return config.DATA_LAKE_URI.strip()
    return lake.default_data_lake_uri()


def load_records(
    data_dir: Optional[Path] = None,
    *,
    data_lake_uri: Optional[str] = None,
) -> dict[str, Any]:
    del data_dir
    return _ps_mod().load_bronze_dataset(
        _contract(),
        config.LAKE_DATASET_PRICES,
        data_lake_uri=data_lake_uri or _lake_uri(),
        latest_only=True,
        id_fields=config.ID_FIELDS,
        id_sep=config.ID_SEP,
        stale_after_hours=config.STALE_AFTER_HOURS,
        item_builder=flight_item_from_bronze,
        read_mode=config.LAKE_READ_MODE,
        read_fallback=config.LAKE_READ_FALLBACK,
    )


def load_history(
    data_dir: Optional[Path] = None,
    *,
    data_lake_uri: Optional[str] = None,
) -> dict[str, Any]:
    del data_dir
    return _ps_mod().load_bronze_dataset(
        _contract(),
        config.LAKE_DATASET_HISTORY,
        data_lake_uri=data_lake_uri or _lake_uri(),
        latest_only=False,
        id_fields=config.ID_FIELDS,
        id_sep=config.ID_SEP,
        stale_after_hours=config.STALE_AFTER_HOURS,
        item_builder=flight_item_from_bronze,
        read_mode=config.LAKE_READ_MODE,
        read_fallback=config.LAKE_READ_FALLBACK,
    )


def get_record(
    record_id: str,
    data_dir: Optional[Path] = None,
    *,
    data_lake_uri: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    payload = load_records(data_dir, data_lake_uri=data_lake_uri)
    return _ps_mod().get_record_from_payload(record_id, payload)


def paginate(
    items: list[dict[str, Any]],
    *,
    limit: int = 50,
    cursor: Optional[str] = None,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    return _ps_mod().paginate(items, limit=limit, cursor=cursor)


def envelope(
    *,
    items: list[dict[str, Any]],
    data_status: str,
    next_cursor: Optional[str] = None,
    retrieved_at: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return _ps_mod().envelope(
        schema_version=config.SCHEMA_VERSION,
        source=config.REPO_NAME,
        items=items,
        data_status=data_status,
        next_cursor=next_cursor,
        retrieved_at=retrieved_at,
        extra=extra,
    )


def load_csv_projection(
    *,
    kind: str = "records",
    data_dir: Optional[Path] = None,
) -> dict[str, Any]:
    data_dir = Path(data_dir or config.DATA_DIR)
    path = data_dir / (
        config.HISTORY_FILE if kind == "history" else config.RECORDS_FILE
    )
    return _ps_mod().load_csv_projection(
        path=path,
        id_fields=["origin", "destination"],
        id_sep="-",
        history=(kind == "history"),
        stale_after_hours=config.STALE_AFTER_HOURS,
    )


def seed_fixtures(data_dir: Optional[Path] = None) -> Path:
    data_dir = Path(data_dir or config.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    src_records = config.FIXTURES_DIR / config.RECORDS_FILE
    src_history = config.FIXTURES_DIR / config.HISTORY_FILE
    if src_records.exists():
        (data_dir / config.RECORDS_FILE).write_text(
            src_records.read_text(encoding="utf-8"), encoding="utf-8"
        )
    if src_history.exists():
        (data_dir / config.HISTORY_FILE).write_text(
            src_history.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return data_dir


def seed_lake_from_provider_captures(
    captures: list[dict[str, Any]],
    *,
    data_lake_uri: str,
    data_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Test helper: land each provider capture separately then optional history.

    Each capture: {provider, raw: bytes|str, flights: list, content_type?, input_format?}
    """
    results = []
    for cap in captures:
        provider = cap["provider"]
        raw = cap["raw"]
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        result = lake.ingest_provider_payload(
            provider=provider,
            raw=raw,
            flights=cap.get("flights") or [],
            dataset=config.LAKE_DATASET_PRICES,
            content_type=cap.get("content_type") or "application/json",
            input_format=cap.get("input_format") or "json",
            data_lake_uri=data_lake_uri,
            metadata=cap.get("metadata") or {"seed": "sample"},
            observed_at=cap.get("observed_at"),
            currency=cap.get("currency") or "THB",
        )
        if data_dir is not None:
            lake.write_lineage(
                result,
                dataset=config.LAKE_DATASET_PRICES,
                data_dir=data_dir,
                provider=provider,
            )
        results.append(result)

        # History dataset also per-provider (separate batch identity).
        hist_raw = json.dumps(
            {
                "provider": provider,
                "dataset": config.LAKE_DATASET_HISTORY,
                "parent_run_id": result.get("run_id"),
                "flights": cap.get("flights") or [],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        hist = lake.ingest_provider_payload(
            provider=provider,
            raw=hist_raw,
            flights=cap.get("flights") or [],
            dataset=config.LAKE_DATASET_HISTORY,
            content_type="application/json",
            input_format="json",
            data_lake_uri=data_lake_uri,
            metadata={"seed": "sample", "dataset_role": "history"},
            observed_at=cap.get("observed_at"),
        )
        if data_dir is not None:
            lake.write_lineage(
                hist,
                dataset=config.LAKE_DATASET_HISTORY,
                data_dir=data_dir,
                provider=provider,
            )
        results.append(hist)
    return results
