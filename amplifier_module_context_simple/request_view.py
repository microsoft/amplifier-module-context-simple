"""Execution views of canonical history, shared with summary/count consumers.

Public originals and copy lineage are history facts, never model context. The
history-only flag omits its row from execution; it does not delete the original.
Projection copies changed metadata and leaves canonical rows untouched. Other
metadata retains its existing behavior. No envelope parsing or storage lives here.
"""

from typing import Any


_HISTORY_METADATA = frozenset({
    "amplifier_public_message",
    "amplifier_public_copy_source",
    "amplifier_public_reference_only",
})


def request_view(
    messages: list[dict[str, Any]], *, strip_sequence: bool = False
) -> list[dict[str, Any]]:
    """Project history for execution; keep sequence IDs until fitting is done."""
    excluded = _HISTORY_METADATA | {"_seq"} if strip_sequence else _HISTORY_METADATA
    result = []
    for message in messages:
        metadata = message.get("metadata")
        if not isinstance(metadata, dict):
            result.append(message)
            continue
        if metadata.get("amplifier_public_reference_only") is True:
            continue
        if excluded.isdisjoint(metadata):
            result.append(message)
            continue
        result.append({
            **message,
            "metadata": {key: value for key, value in metadata.items()
                         if key not in excluded},
        })
    return result
