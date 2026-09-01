# IoT Telemetry Backend

FastAPI service for IoT temperature telemetry:

* Devices live in a DynamoDB registry (`iot-devices`) with a per-device
  temperature threshold.
* Readings are stored in DynamoDB (`iot-readings`), partitioned by `device_id`
  and sorted by ISO-8601 `timestamp`.
* Any reading strictly above the device threshold publishes a JSON alert to the
  SNS topic `iot-temperature-alerts`.

## Endpoints

| Method | Path                                | Purpose                                      |
|--------|-------------------------------------|----------------------------------------------|
| GET    | `/health`                           | Liveness/readiness probe                     |
| POST   | `/devices`                          | Register a device (id, name, location, threshold) |
| GET    | `/devices`                          | List devices (`?limit=`)                     |
| GET    | `/devices/{device_id}`              | Fetch one device record                      |
| PUT    | `/devices/{device_id}/threshold`    | Set the alert threshold                      |
| POST   | `/readings`                         | Ingest a reading (+ SNS alert if over limit) |
| GET    | `/devices/{device_id}/readings`     | List readings (`?start=&end=&limit=`)        |
| GET    | `/devices/{device_id}/stats/daily`  | Daily min/max/avg/count (`?date=YYYY-MM-DD`) |

## Configuration

| Variable                   | Default                    | Meaning                                |
|----------------------------|----------------------------|----------------------------------------|
| `AWS_ENDPOINT_URL`         | *(unset)*                  | LocalStack endpoint, e.g. `http://localhost:4566` |
| `AWS_DEFAULT_REGION`       | `us-east-1`                | AWS region                             |
| `DEVICES_TABLE`            | `iot-devices`              | Device registry table                  |
| `READINGS_TABLE`           | `iot-readings`             | Readings table                         |
| `ALERTS_TOPIC_NAME`        | `iot-temperature-alerts`   | SNS topic name (ARN resolved at runtime) |
| `ALERTS_TOPIC_ARN`         | *(unset)*                  | Explicit topic ARN (skips lookup)      |
| `DEFAULT_THRESHOLD_CELSIUS`| `30`                       | Threshold used when registration omits one |
| `REPOSITORY_BACKEND`       | `dynamodb`                 | Set to `memory` to run without AWS     |
| `HOST` / `PORT`            | all interfaces / `8000`    | Bind address for the built-in runner   |

## Running

```bash
python -m pip install -r requirements.txt

# against LocalStack
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test

uvicorn app:app --host 0.0.0.0 --port 8000
```

Quick smoke test:

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/devices \
  -H 'content-type: application/json' \
  -d '{"device_id":"dev-1","name":"freezer","threshold_celsius":8}'
curl -s -X POST localhost:8000/readings \
  -H 'content-type: application/json' \
  -d '{"device_id":"dev-1","temperature_celsius":12.4}'
curl -s 'localhost:8000/devices/dev-1/stats/daily'
```

## Tests

```bash
python -m pytest -q
```

The suite is fully offline: the HTTP layer uses an in-memory repository and the
DynamoDB/SNS code paths run against fake boto3 objects, so no AWS or LocalStack
instance is needed.
