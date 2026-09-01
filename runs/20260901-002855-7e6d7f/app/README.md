# Expense Tracker API

A small FastAPI service for recording personal expenses in DynamoDB. Expenses can
be created, fetched, updated, deleted, listed (filtered by category and/or month)
and summarised per category for a given month.

## Endpoints

| Method | Path                     | Description                                              |
| ------ | ------------------------ | -------------------------------------------------------- |
| GET    | `/health`                | Liveness probe; reports DynamoDB reachability             |
| POST   | `/expenses`              | Create an expense (`amount`, `category`, `date`, ...)     |
| GET    | `/expenses`              | List expenses (`?user_id=&category=&month=&limit=&cursor=`) |
| GET    | `/expenses/summary`      | Per-category totals for `?month=YYYY-MM` (required)       |
| GET    | `/expenses/{expense_id}` | Fetch one expense                                         |
| PUT    | `/expenses/{expense_id}` | Update amount / category / date / description / currency  |
| DELETE | `/expenses/{expense_id}` | Delete an expense                                         |

## Configuration

| Variable                   | Default              | Purpose                                   |
| -------------------------- | -------------------- | ----------------------------------------- |
| `AWS_ENDPOINT_URL`         | _unset_              | Point boto3 at LocalStack, e.g. `http://localhost:4566` |
| `AWS_DEFAULT_REGION`       | `us-east-1`          | AWS region                                |
| `EXPENSES_TABLE`           | `expenses`           | DynamoDB table name                       |
| `EXPENSES_MONTH_INDEX`     | `month-date-index`   | GSI (`month` hash, `date` range)          |
| `EXPENSES_CATEGORY_INDEX`  | `category-date-index`| GSI (`category` hash, `date` range)       |
| `DEFAULT_USER_ID`          | `default`            | Partition used when no user is supplied   |
| `DEFAULT_CURRENCY`         | `USD`                | Currency applied to new expenses          |

The DynamoDB table uses `user_id` (hash) and `expense_id` (range) keys.

## Running locally

```bash
pip install -r requirements.txt
export AWS_ENDPOINT_URL=http://localhost:4566   # optional, for LocalStack
export AWS_DEFAULT_REGION=us-east-1
uvicorn app:app --host 0.0.0.0 --port 8000
```

Example request:

```bash
curl -X POST http://localhost:8000/expenses \
  -H 'Content-Type: application/json' \
  -d '{"amount": 12.50, "category": "groceries", "date": "2024-03-04"}'

curl 'http://localhost:8000/expenses/summary?month=2024-03'
```

## Tests

The suite is fully offline — every AWS call is stubbed with an in-memory
repository or a fake boto3 table.

```bash
pytest -q
```
