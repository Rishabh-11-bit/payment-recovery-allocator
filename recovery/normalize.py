"""Normalize: raw payment entity -> `(method, source, step, reason)`.

Extraction only. This module makes no judgement about what a key *means* --
that is the classifier's job, and the mapping is hand-authored in
`config/classifier.yaml`.

Two things it does enforce, because both are cheap here and expensive later:

* **The value space is method-partitioned.** Legal `source` values differ per
  method. A `source` outside its method's set is not coerced into one that fits;
  it is flagged, and the classifier routes it to the fallback. Two cases this
  catches: there is no `razorpay` source (it is `internal`), and there is no
  bare `bank` source except for emandate.
* **Missing components are recorded.** A key with three of four parts still
  classifies -- absent parts are wildcards -- but a classification made on a
  thin key should not look as authoritative as one made on a full key.

Aliases are config-driven and empty by default. Rewriting an observed value is
exactly the kind of silent coercion that makes a classifier untrustworthy, so
each alias needs a documented reason and every application is recorded.
"""

from __future__ import annotations

from typing import Any, Mapping

from recovery.models import NormalizedFailure, PaymentSnapshot

# Payment entity field -> key component.
FIELD_MAP = {
    "method": "method",
    "error_source": "source",
    "error_step": "step",
    "error_reason": "reason",
}


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def normalize_entity(
    entity: Mapping[str, Any],
    *,
    source_space: Mapping[str, frozenset[str]] | None = None,
    source_aliases: Mapping[str, str] | None = None,
) -> NormalizedFailure:
    """Extract the classifier key from a raw payment entity.

    `source_space` and `source_aliases` come from the classifier config. When
    omitted, no validation is performed and the key is taken as-is -- used by
    tests that care only about extraction.
    """
    parts = {component: _clean(entity.get(field)) for field, component in FIELD_MAP.items()}
    missing = tuple(sorted(name for name, value in parts.items() if value is None))

    aliases_applied: list[tuple[str, str]] = []
    if source_aliases and parts["source"] in source_aliases:
        original = parts["source"]
        parts["source"] = source_aliases[original]
        aliases_applied.append((original, parts["source"]))

    source_valid = True
    if source_space is not None and parts["method"] and parts["source"]:
        allowed = source_space.get(parts["method"])
        if allowed is not None:
            source_valid = parts["source"] in allowed
        else:
            # Method we have no value space for. Not a source problem; the key
            # simply will not match a rule and falls through to the fallback.
            source_valid = True

    return NormalizedFailure(
        method=parts["method"],
        source=parts["source"],
        step=parts["step"],
        reason=parts["reason"],
        missing=missing,
        source_valid_for_method=source_valid,
        aliases_applied=tuple(aliases_applied),
    )


def normalize_snapshot(
    snapshot: PaymentSnapshot,
    *,
    source_space: Mapping[str, frozenset[str]] | None = None,
    source_aliases: Mapping[str, str] | None = None,
) -> NormalizedFailure:
    """Normalize from authoritative state.

    Prefer this over `normalize_entity` on a webhook body: the snapshot is what
    a decision is allowed to read.
    """
    entity = {
        "method": snapshot.method,
        "error_source": snapshot.error_source,
        "error_step": snapshot.error_step,
        "error_reason": snapshot.error_reason,
    }
    return normalize_entity(entity, source_space=source_space, source_aliases=source_aliases)
