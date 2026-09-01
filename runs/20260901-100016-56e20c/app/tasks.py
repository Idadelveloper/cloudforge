"""Pure compute tasks executed by the Lambda worker.

Keeping the compute logic free of any AWS dependency makes it trivially
testable and re-usable from both the worker and local tooling.
"""

from typing import Any, Dict, Tuple

SUPPORTED_JOB_TYPES: Tuple[str, ...] = (
    "sum",
    "multiply",
    "uppercase",
    "fibonacci",
    "echo",
)

MAX_FIBONACCI_N = 1000


def _numbers(payload: Dict[str, Any]) -> list:
    values = payload.get("values")
    if not isinstance(values, list) or not values:
        raise ValueError("payload.values must be a non-empty list of numbers")
    numbers = []
    for value in values:
        try:
            numbers.append(float(value))
        except (TypeError, ValueError):
            raise ValueError("payload.values must contain numbers only")
    return numbers


def _fibonacci(n: int) -> int:
    first, second = 0, 1
    for _ in range(n):
        first, second = second, first + second
    return first


def execute_job(job_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a compute job and return its result document.

    Raises ValueError for unsupported job types or invalid payloads.
    """
    payload = payload or {}
    if job_type == "sum":
        return {"sum": sum(_numbers(payload))}
    if job_type == "multiply":
        product = 1.0
        for value in _numbers(payload):
            product *= value
        return {"product": product}
    if job_type == "uppercase":
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("payload.text must be a non-empty string")
        return {"text": text.upper(), "length": len(text)}
    if job_type == "fibonacci":
        try:
            n = int(payload.get("n"))
        except (TypeError, ValueError):
            raise ValueError("payload.n must be an integer")
        if n < 0 or n > MAX_FIBONACCI_N:
            raise ValueError("payload.n must be between 0 and %d" % MAX_FIBONACCI_N)
        return {"n": n, "value": str(_fibonacci(n))}
    if job_type == "echo":
        return {"echo": payload}
    raise ValueError("unsupported job_type '%s'" % job_type)
