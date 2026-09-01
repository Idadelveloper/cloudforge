"""FastAPI application for the CloudForge IoT telemetry backend.

Endpoints:
    POST   /devices                        register a device
    GET    /devices                        list registered devices
    GET    /devices/{device_id}            fetch a single device
    PUT    /devices/{device_id}/threshold  update the alert threshold
    POST   /readings                       ingest a temperature reading
    GET    /devices/{device_id}/readings   list raw readings
    GET    /devices/{device_id}/stats/daily  daily min/max/avg/count
    GET    /health                         liveness probe
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel

from storage import DynamoTelemetryStore, TelemetryStore

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("iot_telemetry.app")


def default_threshold() -> float:
    """Default alert threshold applied when a device omits one."""
    try:
        return float(os.environ.get("DEFAULT_THRESHOLD_CELSIUS", "30.0"))
    except ValueError:
        return 30.0


app = FastAPI(
    title="IoT Telemetry Backend",
    version="1.0.0",
    description="Device registry, temperature ingest with SNS alerting and daily aggregates.",
)


@lru_cache(maxsize=1)
def get_store() -> TelemetryStore:
    """Return the process-wide DynamoDB/SNS backed store."""
    return DynamoTelemetryStore()


class DeviceCreate(BaseModel):
    """Payload used to register a device."""

    device_id: str
    name: Optional[str] = None
    location: Optional[str] = None
    threshold_celsius: Optional[float] = None


class ThresholdUpdate(BaseModel):
    """Payload used to change a device threshold."""

    threshold_celsius: float


class ReadingIn(BaseModel):
    """Payload used to ingest a temperature reading."""

    device_id: str
    temperature_celsius: float
    timestamp: Optional[str] = None


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_timestamp(value: datetime) -> str:
    """Render a UTC datetime in a lexicographically sortable canonical form."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def utc_now_iso() -> str:
    """Current UTC time in canonical ISO-8601 form."""
    return canonical_timestamp(datetime.now(timezone.utc))


def _parse_timestamp_or_400(value: str, field: str) -> datetime:
    try:
        return parse_timestamp(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field} must be an ISO-8601 timestamp")


def _parse_day_or_400(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be formatted as YYYY-MM-DD")


def _compute_stats(device_id: str, day: str, readings: List[Dict[str, Any]]) -> Dict[str, Any]:
    values = [
        float(item["temperature_celsius"])
        for item in readings
        if item.get("temperature_celsius") is not None
    ]
    if not values:
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
        "min_celsius": min(values),
        "max_celsius": max(values),
        "avg_celsius": round(sum(values) / len(values), 4),
        "count": len(values),
    }


@app.get("/health")
def health() -> Dict[str, Any]:
    """Liveness/readiness probe."""
    return {"status": "ok", "service": "iot_telemetry_backend", "time": utc_now_iso()}


@app.post("/devices", status_code=201)
def register_device(
    payload: DeviceCreate,
    store: TelemetryStore = Depends(get_store),
) -> Dict[str, Any]:
    """Register a new device in the registry."""
    device_id = payload.device_id.strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id must not be empty")
    if store.get_device(device_id) is not None:
        raise HTTPException(status_code=409, detail=f"device '{device_id}' is already registered")

    threshold = payload.threshold_celsius
    if threshold is None:
        threshold = default_threshold()
    now = utc_now_iso()
    device = {
        "device_id": device_id,
        "name": payload.name,
        "location": payload.location,
        "threshold_celsius": float(threshold),
        "registered_at": now,
        "updated_at": now,
    }
    store.put_device(device)
    LOGGER.info("registered device %s with threshold %.2f", device_id, device["threshold_celsius"])
    return device


@app.get("/devices")
def list_devices(store: TelemetryStore = Depends(get_store)) -> Dict[str, Any]:
    """List every registered device."""
    devices = store.list_devices()
    devices.sort(key=lambda item: str(item.get("device_id", "")))
    return {"devices": devices, "count": len(devices)}


@app.get("/devices/{device_id}")
def get_device(device_id: str, store: TelemetryStore = Depends(get_store)) -> Dict[str, Any]:
    """Fetch a single device record."""
    device = store.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail=f"device '{device_id}' not found")
    return device


@app.put("/devices/{device_id}/threshold")
def set_threshold(
    device_id: str,
    payload: ThresholdUpdate,
    store: TelemetryStore = Depends(get_store),
) -> Dict[str, Any]:
    """Set or update a device's temperature alert threshold."""
    device = store.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail=f"device '{device_id}' not found")
    device["threshold_celsius"] = float(payload.threshold_celsius)
    device["updated_at"] = utc_now_iso()
    store.put_device(device)
    LOGGER.info("device %s threshold set to %.2f", device_id, device["threshold_celsius"])
    return device


@app.post("/readings", status_code=201)
def ingest_reading(
    payload: ReadingIn,
    store: TelemetryStore = Depends(get_store),
) -> Dict[str, Any]:
    """Store a reading and publish an SNS alert when the threshold is exceeded."""
    device_id = payload.device_id.strip()
    device = store.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail=f"device '{device_id}' not found")

    if payload.timestamp:
        moment = _parse_timestamp_or_400(payload.timestamp, "timestamp")
    else:
        moment = datetime.now(timezone.utc)

    timestamp = canonical_timestamp(moment)
    threshold = float(device.get("threshold_celsius") or default_threshold())
    temperature = float(payload.temperature_celsius)
    alert_triggered = temperature > threshold

    reading = {
        "device_id": device_id,
        "timestamp": timestamp,
        "temperature_celsius": temperature,
        "day": moment.astimezone(timezone.utc).strftime("%Y-%m-%d"),
        "alert_triggered": alert_triggered,
        "threshold_at_ingest": threshold,
    }
    store.put_reading(reading)

    alert_published = False
    if alert_triggered:
        message = {
            "device_id": device_id,
            "timestamp": timestamp,
            "temperature_celsius": temperature,
            "threshold_celsius": threshold,
            "message": (
                f"Device {device_id} reported {temperature} C which exceeds "
                f"its threshold of {threshold} C"
            ),
        }
        alert_published = store.publish_alert(f"IoT temperature alert: {device_id}", message)
        LOGGER.warning("alert for device %s (%.2f > %.2f) published=%s",
                       device_id, temperature, threshold, alert_published)

    return {
        "reading": reading,
        "alert_triggered": alert_triggered,
        "alert_published": alert_published,
    }


@app.get("/devices/{device_id}/readings")
def list_readings(
    device_id: str,
    start: Optional[str] = Query(default=None, description="ISO-8601 inclusive lower bound"),
    end: Optional[str] = Query(default=None, description="ISO-8601 inclusive upper bound"),
    limit: int = Query(default=200, ge=1, le=1000),
    store: TelemetryStore = Depends(get_store),
) -> Dict[str, Any]:
    """List raw readings for a device, optionally filtered by a time range."""
    if store.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail=f"device '{device_id}' not found")

    start_key = canonical_timestamp(_parse_timestamp_or_400(start, "start")) if start else None
    end_key = canonical_timestamp(_parse_timestamp_or_400(end, "end")) if end else None
    readings = store.query_readings(device_id, start=start_key, end=end_key, limit=limit)
    return {
        "device_id": device_id,
        "start": start_key,
        "end": end_key,
        "readings": readings,
        "count": len(readings),
    }


@app.get("/devices/{device_id}/stats/daily")
def daily_stats(
    device_id: str,
    date: Optional[str] = Query(default=None, description="UTC day formatted YYYY-MM-DD"),
    store: TelemetryStore = Depends(get_store),
) -> Dict[str, Any]:
    """Return the daily minimum, maximum, average and count for a device."""
    if store.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail=f"device '{device_id}' not found")

    day = _parse_day_or_400(date) if date else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_key = f"{day}T00:00:00.000000Z"
    end_key = f"{day}T23:59:59.999999Z"
    readings = store.query_readings(device_id, start=start_key, end=end_key)
    readings = [item for item in readings if start_key <= str(item.get("timestamp", "")) <= end_key]
    return _compute_stats(device_id, day, readings)


def main() -> None:  # pragma: no cover - manual entrypoint
    """Run the service with uvicorn."""
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
