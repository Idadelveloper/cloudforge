# expense_tracker_api

A small FastAPI service for personal expense tracking. Records are persisted in a
single DynamoDB table (partition key `user_id`, sort key `sk = "<date>#<expense_id>"`)
with a category global secondary index (`gsi1pk = "<user_id>#<category>"`).

## Endpoints

| Method | Path                    | Purpose                                              |
| ------ | ----------------------- | ---------------------------------------------------- |
| GET    | `/health`               | Liveness probe; also verifies the table is reachable |
| POST   | `/expenses`             | Create an expense (`amount`, `category`, `date`, ...) |
| GET    | `/expenses`             | List expenses, filter with `?category=` / `?month=`  |
| GET    | `/expenses/{id}`        | Fetch one expense                                    |
| PUT    | `/expenses/{id}`        | Update amount / category / date / description        |
| DELETE | `/expenses/{id}`        | Delete one expense                                   |
| GET    | `/summary?month=YYYY-MM`| Total spend per category for a month                 |

The caller identity is taken from the optional `X-User-Id` header and defaults to
`default`. Amounts are handled as `Decimal` and rounded to two decimal places.
List responses are newest-first with an opaque `next_cursor` for pagination
(`?limit=` 1..200, default 50).

## Configuration

| Variable                   | Default                  | Meaning                                |
| -------------------------- | ------------------------ | -------------------------------------- |
| `AWS_ENDPOINT_URL`         | _(unset)_                | Point boto3 at LocalStack when set     |
| `AWS_REGION`               | `us-east-1`              | AWS region                             |
| `EXPENSES_TABLE`           | `expenses`               | DynamoDB table name                    |
| `EXPENSES_CATEGORY_INDEX`  | `expenses-gsi-category`  | Category GSI name                      |
| `DEFAULT_USER_ID`          | `default`                | Fallback tenant when no header is sent |
| `DEFAULT_CURRENCY`         | `USD`                    | Currency assumed for new expenses      |
| `HOST` / `PORT`            | `127.0.0.1` / `8000`     | Bind address for the built-in runner   |
| `LOG_LEVEL`                | `INFO`                   | Logging verbosity                      |

## Running

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566   # LocalStack
export AWS_REGION=us-east-1
export EXPENSES_TABLE=expenses
uvicorn app:app --host 127.0.0.1 --port 8000
```

Example request:

```bash
curl -X POST http://127.0.0.1:8000/expenses \
  -H 'Content-Type: application/json' -H 'X-User-Id: alice' \
  -d '{"amount": "12.50", "category": "groceries", "date": "2024-03-05"}'

curl 'http://127.0.0.1:8000/summary?month=2024-03' -H 'X-User-Id: alice'
```

## Tests

```bash
python -m pytest
```

The test suite injects an in-memory repository and stubs boto3, so it runs fully
offline with no AWS or LocalStack dependency.
