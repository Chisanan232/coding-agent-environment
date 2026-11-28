"""Validate the existing frontend qualification receipt before releasing report bytes."""
import hashlib
import json


# Stable IDs for the qualification's trace, observer, composite and closing findings.
FINAL_ITEMS = frozenset({"A", "B", "C", "Closing"})


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate review field: {key!r}")
        result[key] = value
    return result


def _object(data):
    value = json.loads(data, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def approved_bytes(host, artifact, receipt, *, stage, reviewer, trusted_receipts,
                   source_index, sources, reviewed_items):
    """Return the exact artifact or None. Trust inputs come from the separate review.

    reviewed_items is the item-ID to assessment-text mapping for this artifact
    from its independently approved record, not a map supplied by the candidate.
    The first-pass spelling exists only for replaying the archived draft decision.
    """
    if not receipt or _sha(receipt) not in trusted_receipts or host not in {"claude", "codex"}:
        return None
    if stage == "final":
        path_field, directory, required = "file", "final", FINAL_ITEMS
    elif stage == "first-pass":
        path_field, directory = "draft", "drafts"
        required = (FINAL_ITEMS - {"Closing"}) | {"Closing summary"}
    else:
        return None

    try:
        record = _object(receipt)
        if (record["stage"] != stage or record["independent"] is not True
                or not reviewer or record["reviewer"] != reviewer
                or not isinstance(record["reviewer_role"], str) or not record["reviewer_role"].strip()):
            return None
        report = record["reports"][host]
        if (report["decision"] != "APPROVED" or report[path_field] != f"{directory}/{host}.md"
                or report["sha256"] != _sha(artifact)):
            return None

        items = report["claim_assessments"]
        if not isinstance(items, dict) or items.keys() != required:
            return None
        if any(not isinstance(value, str) or not value.strip() for value in items.values()):
            return None
        if items != reviewed_items:
            return None

        if record["source_index_sha256"] != _sha(source_index):
            return None
        index = _object(source_index)
        if not index or any(_sha(sources[name]) != expected for name, expected in index.items()):
            return None
    except (KeyError, TypeError, ValueError, RecursionError):
        return None
    return artifact
