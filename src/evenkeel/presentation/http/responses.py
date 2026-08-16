"""Named sets of the error responses each operation can actually produce.

Written as a lookup rather than spelled out per route so the schema cannot drift
from the code by copy-paste, and so adding a status to a family is one edit.

Every entry here is a status this API genuinely returns. The temptation with a
helper like this is to attach a generous superset to everything — which is the
same lie as declaring too few, told in the other direction. A client that
handles 429 on an endpoint with no rate limit has written dead code because we
misled it.
"""

from typing import Any

from evenkeel.presentation.http.schemas.problem import Problem

Responses = dict[int | str, dict[str, Any]]

_PROBLEM = {"model": Problem, "content": {"application/problem+json": {}}}


def _problem(description: str) -> dict[str, Any]:
    return {**_PROBLEM, "description": description}


UNAUTHENTICATED: Responses = {
    401: _problem("No credential was supplied, or it was not a valid owner id.")
}

INVALID_REQUEST: Responses = {
    422: _problem(
        "The request failed validation, or violated a domain rule that is not a "
        "conflict — mixing currencies, for example. `details.errors` names the "
        "offending fields but never echoes their values."
    )
}

WALLET_ABSENT: Responses = {
    404: _problem(
        "No such wallet for this owner. A wallet that exists but belongs to "
        "someone else is reported as absent, so the status cannot be used to "
        "probe which ids are real."
    )
}

MOVEMENT_FORBIDDEN: Responses = {
    403: _problem(
        "Risk assessment refused the movement. Nothing changed, and retrying "
        "with the same payload will be refused again. `details.reference` is "
        "the provider's id for the decision, for a support conversation."
    )
}

MOVEMENT_REFUSED: Responses = {
    409: _problem(
        "The movement was refused and nothing changed: insufficient funds, the "
        "wallet is closed, another writer holds it, a concurrent write won, or "
        "the idempotency key was reused with a different payload. `code` "
        "distinguishes them."
    )
}

RATE_LIMITED: Responses = {
    429: _problem(
        "Too many movements for this owner. `details.retry_after_seconds` says "
        "how long to wait. Nothing was applied."
    )
}

DEPENDENCY_UNAVAILABLE: Responses = {
    503: _problem(
        "A dependency the operation needs is unreachable. Transient; retry with "
        "the same idempotency key."
    )
}


def read_responses() -> Responses:
    """Reads of a specific wallet."""
    return {**UNAUTHENTICATED, **WALLET_ABSENT, **INVALID_REQUEST}


def collection_responses() -> Responses:
    """Reads that cannot 404 because an empty page is a valid answer."""
    return {**UNAUTHENTICATED, **INVALID_REQUEST}


def movement_responses() -> Responses:
    """Everything a balance change can answer with."""
    return {
        **UNAUTHENTICATED,
        **WALLET_ABSENT,
        **MOVEMENT_FORBIDDEN,
        **MOVEMENT_REFUSED,
        **INVALID_REQUEST,
        **RATE_LIMITED,
        **DEPENDENCY_UNAVAILABLE,
    }
