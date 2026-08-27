"""Normalize: raw payment entity -> `(method, source, step, reason)`.

Extraction only. This module makes no judgement about what a key *means* --
that is the classifier's job, and the mapping is hand-authored in
`config/classifier.yaml`.

Two things it does enforce, because both are cheap here and expensive later:

* **The value space is method-partitioned, and it is a lower bound.** Legal
  `source` values differ per method. A `source` outside its method's documented
  set is not coerced into one that fits, and it is not rejected either -- it is
  flagged for review and classification proceeds normally.

  The lower-bound framing is not a design preference, it is a correction. A real
  test-mode netbanking failure returns `source: bank`, which the error-parameters
  reference lists only for emandate. Treating the documented set as an
  enumeration made the classifier reject an ordinary production payload. The
  documentation is a subset of reality; a check derived from it must surface,
  not reject. See CHALLENGES 007.
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
#
# **Flat fields, deliberately.** A `payment.failed` webhook carries `error_code`,
# `error_source`, `error_step` and `error_reason` as flat fields directly on the
# payment entity. The nested `error: {...}` object is the shape of an *API error
# response*, not of a webhook payload, and is never what arrives here.
#
# The captured fixtures came from an API *fetch*, which returns the same flat
# shape on the payment entity -- so they could not have revealed a difference
# between the two sources if one existed. `has_nested_error_object` exists so a
# nested payload is loud rather than silently degrading to an empty key.
FIELD_MAP = {
    "method": "method",
    "error_source": "source",
    "error_step": "step",
    "error_reason": "reason",
}


def has_nested_error_object(entity: Mapping[str, Any]) -> bool:
    """True if the payload nests its error fields instead of flattening them.

    Should never fire. If it does, the shape assumption above is wrong and every
    key would otherwise normalise to `-/-/-/-`, classify as unmapped, and land in
    the LOW row -- safe, and completely silent. Detecting it is the difference
    between "we are handling an unknown key" and "we are misreading every key".
    """
    nested = entity.get("error")
    if not isinstance(nested, Mapping):
        return False
    return any(field in nested for field in ("source", "step", "reason", "code"))


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

    source_documented = True
    if source_space is not None and parts["method"] and parts["source"]:
        allowed = source_space.get(parts["method"])
        if allowed is not None:
            source_documented = parts["source"] in allowed
        else:
            # A method we have no value space for at all -- `wallet` appears in
            # real captures and is absent from the reference the space was built
            # from. Nothing to check against, so nothing to surface.
            source_documented = True

    return NormalizedFailure(
        method=parts["method"],
        source=parts["source"],
        step=parts["step"],
        reason=parts["reason"],
        missing=missing,
        source_in_documented_space=source_documented,
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
