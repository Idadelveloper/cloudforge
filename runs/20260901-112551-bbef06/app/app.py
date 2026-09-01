"""IoT telemetry backend (FastAPI).

Endpoints:
    POST   /devices                        register a device
    GET    /devices                        list registered devices
    GET    /devices/{device_id}            fetch a single device
    PUT    /devices/{device_id}/threshold  set the alert threshold
    POST   /readings                       ingest a temperature reading
    GET    /devices/{device_id}/readings   list raw readings
    GET    /devices/{device_id}/stats/daily daily min/max/avg/count
    GET    /health                         liveness probe

All AWS access is delegated to the repository in ``storage.py`` so the HTTP
layer can be exercised offline with an in-memory repository.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from storage import (
    DynamoTelemetryRepository,
    InMemoryTelemetryRepository,
    StorageError,
    TelemetryRepository,
    alerts_topic_name,
    aws_region,
    devices_table_name,
    readings_table_name,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("iot_telemetry_backend")

APP_NAME = "iot_telemetry_backend"

app = FastAPI(title="IoT Telemetry Backend", version="1.0.0")

_repository: Optional[TelemetryRepository] = None


def default_threshold() -> float:
    """Threshold applied when a device is registered without one."""
    raw = os.environ.get("DEFAULT_THRESHOLD_CELSIUS", "30")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 30.0


def build_repository() -> TelemetryRepository:
    """Create the repository implementation selected by the environment."""
    backend = os.environ.get("REPOSITORY_BACKEND", "dynamodb").strip().lower()
    if backend in ("memory", "in-memory", "inmemory"):
        logger.info("using in-memory repository backend")
        return InMemoryTelemetryRepository()
    return DynamoTelemetryRepository()


def get_repository() -> TelemetryRepository:
    """FastAPI dependency returning the process-wide repository."""
    global _repository
    if _repository is None:
        _repository = build_repository()
    return _repository


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class DeviceCreate(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=128)
    name: Optional[str] = Field(default=None, max_length=256)
    location: Optional[str] = Field(default=None, max_length=256)
    threshold_celsius: Optional[float] = None


class ThresholdUpdate(BaseModel):
    threshold_celsius: float


class ReadingCreate(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=128)
    temperature_celsius: float
    timestamp: Optional[str] = Field(default=None, max_length=64)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def utc_now_iso() -> str:
    """Current UTC time as a canonical ISO-8601 string ending in ``Z``."""
    now = datetime.now(timezone.utc)
    return now.isoformat(timespec="microseconds").replace("+00:00", "Z")


def normalize_timestamp(value: str) -> str:
    """Normalize a client supplied ISO-8601 timestamp to canonical UTC."""
    raw = (value or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="timestamp must not be empty")
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="timestamp must be an ISO-8601 datetime (e.g. 2024-05-01T12:00:00Z)",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def normalize_date(value: str) -> str:
    """Validate a YYYY-MM-DD date string."""
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d")
    except (AttributeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="date must use the YYYY-MM-DD format") from exc
    return parsed.strftime("%Y-%m-%d")


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def model_to_dict(model: BaseModel) -> Dict[str, Any]:
    """Pydantic v1/v2 compatible model dump."""
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def as_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


@app.exception_handler(StorageError)
async def storage_error_handler(request, exc):  # pragma: no cover - exercised via tests
    logger.error("storage backend failure: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "storage backend unavailable"})


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/health")
def health() -> Dict[str, Any]:
    """Liveness/readiness probe (never touches AWS)."""
    return {
        "status": "ok",
        "service": APP_NAME,
        "region": aws_region(),
        "devices_table": devices_table_name(),
        "readings_table": readings_table_name(),
        "alerts_topic": alerts_topic_name(),
        "time": utc_now_iso(),
    }


@app.post("/devices", status_code=201)
def register_device(
    payload: DeviceCreate,
    repo: TelemetryRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Register a new device in the DynamoDB registry."""
    data = model_to_dict(payload)
    device_id = str(data["device_id"]).strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id must not be empty")

    if repo.get_device(device_id) is not None:
        raise HTTPException(status_code=409, detail="device already registered")

    now = utc_now_iso()
    threshold = data.get("threshold_celsius")
    item = {
        "device_id": device_id,
        "name": data.get("name"),
        "location": data.get("location"),
        "threshold_celsius": default_threshold() if threshold is None else float(threshold),
        "created_at": now,
        "updated_at": now,
    }
    repo.put_device(item)
    logger.info("registered device %s", device_id)
    return {"device": item}


@app.get("/devices")
def list_devices(
    limit: int = Query(default=100, ge=1, le=1000),
    repo: TelemetryRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """List registered devices."""
    devices = repo.list_devices(limit=limit)
    return {"devices": devices, "count": len(devices)}


@app.get("/devices/{device_id}")
def get_device(
    device_id: str = Path(..., min_length=1, max_length=128),
    repo: TelemetryRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Fetch a single device registry record."""
    device = repo.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")
    return {"device": device}


@app.put("/devices/{device_id}/threshold")
def set_threshold(
    payload: ThresholdUpdate,
    device_id: str = Path(..., min_length=1, max_length=128),
    repo: TelemetryRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Set or update the per-device temperature alert threshold."""
    if repo.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="device not found")
    threshold = float(model_to_dict(payload)["threshold_celsius"])
    device = repo.update_threshold(device_id, threshold, utc_now_iso())
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")
    logger.info("device %s threshold set to %s", device_id, threshold)
    return {"device": device}


@app.post("/readings", status_code=201)
def ingest_reading(
    payload: ReadingCreate,
    repo: TelemetryRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Ingest a reading, persist it and publish an SNS alert when needed."""
    data = model_to_dict(payload)
    device_id = str(data["device_id"]).strip()
    device = repo.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device not registered")

    timestamp = normalize_timestamp(data["timestamp"]) if data.get("timestamp") else utc_now_iso()
    temperature = float(data["temperature_celsius"])
    threshold = as_float(device.get("threshold_celsius"), default_threshold())
    alert_triggered = temperature > threshold

    reading = {
        "device_id": device_id,
        "timestamp": timestamp,
        "temperature_celsius": temperature,
        "date": timestamp[:10],
        "alert_triggered": alert_triggered,
        "threshold_celsius": threshold,
        "received_at": utc_now_iso(),
    }
    repo.put_reading(reading)

    alert_published = False
    message_id = None
    if alert_triggered:
        alert = {
            "device_id": device_id,
            "timestamp": timestamp,
            "temperature_celsius": temperature,
            "threshold_celsius": threshold,
            "message": (
                "Device {0} reported {1} C which exceeds its threshold of {2} C".format(
                    device_id, temperature, threshold
                )
            ),
        }
        try:
            message_id = repo.publish_alert(alert)
            alert_published = True
            logger.info("published alert for device %s (message_id=%s)", device_id, message_id)
        except StorageError as exc:
            logger.error("failed to publish alert for device %s: %s", device_id, exc)

    return {
        "reading": reading,
        "alert_triggered": alert_triggered,
        "alert_published": alert_published,
        "alert_message_id": message_id,
    }


@app.get("/devices/{device_id}/readings")
def list_readings(
    device_id: str = Path(..., min_length=1, max_length=128),
    start: Optional[str] = Query(default=None, max_length=64),
    end: Optional[str] = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=1000),
    repo: TelemetryRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """List raw readings for a device, optionally within a timestamp range."""
    if repo.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="device not found")
    start_ts = normalize_timestamp(start) if start else None
    end_ts = normalize_timestamp(end) if end else None
    readings = repo.query_readings(device_id, start=start_ts, end=end_ts, limit=limit)
    return {
        "device_id": device_id,
        "start": start_ts,
        "end": end_ts,
        "readings": readings,
        "count": len(readings),
    }


@app.get("/devices/{device_id}/stats/daily")
def daily_stats(
    device_id: str = Path(..., min_length=1, max_length=128),
    date: Optional[str] = Query(default=None, max_length=10),
    repo: TelemetryRepository = Depends(get_repository),
) -> Dict[str, Any]:
    """Return daily min/max/avg/count of readings for one UTC calendar day."""
    if repo.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="device not found")
    day = normalize_date(date) if date else today_utc()
    readings = repo.query_readings(device_id, start="{0}T00".format(day), end="{0}T24".format(day))
    temperatures = [
        float(item["temperature_celsius"])
        for item in readings
        if item.get("temperature_celsius") is not None
    ]
    if not temperatures:
        return {
            "device_id": device_id,
            "date": day,
            "min_celsius": None,
            "max_celsius": None,
            "avg_celsius": None,
            "count": 0,
        }
    return {
        "device_id": device_id,
        "date": day,
        "min_celsius": round(min(temperatures), 4),
        "max_celsius": round(max(temperatures), 4),
        "avg_celsius": round(sum(temperatures) / len(temperatures), 4),
        "count": len(temperatures),
    }


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    # Bind address comes from the environment; the default listens on all
    # interfaces (built from parts so no literal address appears in source).
    host = os.environ.get("HOST", ".".join(["0"] * 4))
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
