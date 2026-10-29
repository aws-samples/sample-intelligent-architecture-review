# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared DynamoDB access helpers.

The only thing here is `scan_all`, and it exists because a bare `table.scan()`
is a correctness trap this codebase hit repeatedly.

DynamoDB caps a single scan response at 1MB and reports the cut-off only via
`LastEvaluatedKey` — no exception, no flag on the items. So an unpaginated scan
silently returns a PREFIX of the table once it outgrows a page. Two consequences
bit us:

  * `list_standards` feeds the validator. A truncated rule set means rules
    silently stop being enforced and a document is reported compliant against a
    rulebook that was quietly cut short — the worst failure mode a compliance
    tool has, because it is confidently wrong.
  * A `FilterExpression` is applied AFTER the page is read, not before. So a
    filtered scan looking for one row can return zero matches purely because the
    matching row sat on page two. That turned the validation cache into a
    false-miss generator (duplicate Bedrock spend) and broke the in-flight
    dedupe that stops two identical runs racing.

Pagination is the floor, not the fix. These tables want indexes: a GSI on
`validations.document_id` and on `standards.status` turns the hot paths into
queries. `scan_all` keeps the answers correct until then.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Safety valve. A scan that needs more pages than this means the table has grown
# far past what full-table access suits, and we would rather log loudly than
# spend minutes walking it on a request path.
MAX_SCAN_PAGES = 100


def scan_all(table, *, page_limit: int = MAX_SCAN_PAGES, **scan_kwargs) -> list[dict]:
    """Scan a whole table, following `LastEvaluatedKey`.

    Accepts the same kwargs as `table.scan` (`FilterExpression`,
    `ProjectionExpression`, ...) and returns every matching item rather than
    just the first page.

    Exceptions propagate: callers already wrap store access in their own
    `except ClientError` and decide how to degrade. Swallowing here would
    reintroduce the silent-truncation bug in a new shape.
    """
    items: list[dict] = []
    start_key: Optional[dict] = None
    pages = 0

    while True:
        kwargs = dict(scan_kwargs)
        if start_key is not None:
            kwargs["ExclusiveStartKey"] = start_key
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        pages += 1

        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            break
        if pages >= page_limit:
            # Truncated — but say so. Silence is what made this a bug.
            logger.error(
                "scan_all stopped after %d pages on %s with more data remaining; "
                "results are INCOMPLETE. This table needs an index or a query "
                "access pattern.",
                pages, getattr(table, "name", "<table>"),
            )
            break

    return items
