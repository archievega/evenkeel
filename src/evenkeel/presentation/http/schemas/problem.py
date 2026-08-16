"""The error contract, declared rather than merely emitted.

`problem()` in `http/errors.py` has always returned RFC 9457 documents. Nothing
said so in the OpenAPI schema: every operation advertised `200` and `422` and no
error shape, so a generated client could not handle a single one of the statuses
this API actually returns — 401, 404, 409, 429, 503 all arrived as surprises.

Declaring them is not documentation polish. An undeclared status is an
undeclared branch in every consumer.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Problem(BaseModel):
    """RFC 9457 `application/problem+json`."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "type": "about:blank#wallet_insufficient_funds",
                    "title": "Wallet insufficient funds",
                    "status": 409,
                    "code": "WALLET_INSUFFICIENT_FUNDS",
                    "instance": "/v1/wallets/01a0084a-d15c-7122-bc8c-a81b38e8a2f4/withdrawals",
                    "correlation_id": "489b1a95-1123-43ef-b6f6-03bbb5e15248",
                    "details": {"balance": "70.00", "requested": "1000.00"},
                }
            ]
        }
    )

    type: str = Field(description="Stable URI reference identifying the problem kind.")
    title: str = Field(description="Short human-readable summary. Safe to display.")
    status: int = Field(
        description="HTTP status code, repeated for clients that lose it."
    )
    code: str = Field(
        description=(
            "Machine-readable code. Branch on this, never on `title` — titles are "
            "prose and may be reworded; codes are part of the contract."
        )
    )
    instance: str = Field(description="Path of the request that produced the problem.")
    correlation_id: str | None = Field(
        default=None,
        description=(
            "Quote this when reporting the failure; it is the key to the server-side "
            "log line. Present on every response this API produces."
        ),
    )
    details: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Structured context, shape depending on `code`. Never contains the "
            "rejected value or any server internals."
        ),
    )
