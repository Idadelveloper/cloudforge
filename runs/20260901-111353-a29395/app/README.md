# IoT Telemetry Backend

FastAPI service that keeps an IoT device registry in DynamoDB, ingests temperature
readings, and publishes an SNS alert whenever a reading exceeds the device's
configured threshold. Daily aggregates (min / max / average / count) are computed
on read from the readings table.

## Requirements

```bash
pip install -r requirements.txt
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AWS_ENDPOINT_URL` | _unset_ | Point every AWS client at LocalStack, e.g. `http://localhost:4566` |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `DEVICES_TABLE` | `iot-devices` | DynamoDB device registry table |
| `READINGS_TABLE` | `iot-readings` | DynamoDB readings table (`device_id` + `timestamp`) |
| `ALERTS_TOPIC_NAME` | `iot-telemetry-alerts` | SNS topic name (resolved via `create_topic`) |
| `ALERTS_TOPIC_ARN` | _unset_ | Pre-resolved SNS topic ARN (skips lookup) |
| `DEFAULT_THRESHOLD_CELSIUS` | `30.0` | Threshold applied when a device registers without one |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address for the built-in runner |

## Running

```bash
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
uvicorn app:app --host 127.0.0.1 --port 8000
```

or simply `python app.py`.

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| GET | `/health` | Liveness probe |
| POST | `/devices` | Register a device (`device_id`, optional `name`, `location`, `threshold_celsius`) |
| GET | `/devices` | List registered devices |
| GET | `/devices/{device_id}` | Fetch one device |
| PUT | `/devices/{device_id}/threshold` | Update the alert threshold |
| POST | `/readings` | Ingest `{device_id, temperature_celsius, timestamp?}` |
| GET | `/devices/{device_id}/readings` | Raw readings, optional `start` / `end` / `limit` |
| GET | `/devices/{device_id}/stats/daily` | Daily min/max/avg/count, optional `date=YYYY-MM-DD` |

### Example

```bash
curl -X POST localhost:8000/devices \
  -H 'content-type: application/json' \
  -d '{"device_id": "sensor-1", "location": "lab", "threshold_celsius": 25}'

curl -X POST localhost:8000/readings \
  -H 'content-type: application/json' \
  -d '{"device_id": "sensor-1", "temperature_celsius": 31.5}'

curl 'localhost:8000/devices/sensor-1/stats/daily'
```

## Tests

```bash
pytest
```

The suite is fully offline: the HTTP layer runs against an in-memory store and the
DynamoDB/SNS code paths are exercised with hand written fakes.
