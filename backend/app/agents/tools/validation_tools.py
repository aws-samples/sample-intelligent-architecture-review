# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read-only validation tools exposed to the conversational query agent.

`record_finding` used to live here too, used by an in-agent validation loop.
That path was deleted when validation moved to the parallel fan-out
service (app/services/parallel_validator.py), so this module now only
exposes the read helper that the query agent uses to ground answers
about prior validations.
"""



from strands.tools import tool


@tool
def get_past_validations(document_id: str) -> str:
    """Get previous validation results for a document to compare against."""
    from app.services.validation_store import validation_store

    validations = validation_store.list_validations(document_id=document_id)
    if not validations:
        return "No previous validations found for this document."
    parts = []
    for v in validations:
        parts.append(
            f"- {v.validation_id}: score={v.overall_score}, "
            f"findings={len(v.findings)}, status={v.status}"
        )
    return f"{len(validations)} previous validations:\n" + "\n".join(parts)
