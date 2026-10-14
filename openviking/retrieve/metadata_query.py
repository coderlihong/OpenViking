"""Resolve explicit business constraints inside the existing query snapshot."""
from __future__ import annotations

import asyncio
import json

from openviking.indexing.business_meta import AuthoritativeMeta, MetaLimits, validate_meta_key
from openviking.indexing.metadata_integration import TOKEN_COLUMN, field_token, value_token
from openviking.indexing.publication import _view, configured_store
from openviking.indexing.record_meta import canonical_field_name
from openviking.retrieve.meta_dictionary import BusinessDictionary, MetaFilter, MetaHint, resolve_query
from openviking_cli.exceptions import FailedPreconditionError, InvalidArgumentError


def parse_constraints(filters):
    if not isinstance(filters, (list, tuple)) or len(filters) > 16:
        raise InvalidArgumentError("meta_filters must contain at most 16 field constraints")
    parsed = []
    for item in filters:
        if not isinstance(item, dict) or set(item) - {"field", "values", "op", "include_unknown"}:
            raise InvalidArgumentError("Invalid business metadata filter")
        try:
            # Resolved before validation: an authored Chinese key is the same field
            # as its canonical spelling, and ``document_id`` only becomes the legal
            # ``source_document_id`` once that alias is applied.
            field = validate_meta_key(canonical_field_name(item.get("field")), MetaLimits())
        except ValueError as exc:
            raise InvalidArgumentError(str(exc)) from exc
        values = item.get("values")
        if not isinstance(values, list) or not 1 <= len(values) <= 32 or any(not isinstance(value, str) or not value.strip() or len(value) > 512 for value in values):
            raise InvalidArgumentError("Business metadata filter values require 1 to 32 bounded strings")
        if not isinstance(item.get("include_unknown", False), bool):
            raise InvalidArgumentError("include_unknown must be a boolean")
        parsed.append(MetaFilter(field, tuple(values), item.get("op", "exact"), item.get("include_unknown", False)))
    return parsed


def token_filter(resolution):
    if resolution.is_empty():
        return {"op": "must", "field": TOKEN_COLUMN, "conds": ["__no_metadata_candidate__"]}
    clauses = []
    for field, values in resolution.hard_fields.items():
        branches = []
        if values:
            branches.append({"op": "must", "field": TOKEN_COLUMN, "conds": [value_token(field, value) for value in values]})
        if field in resolution.include_unknown_fields:
            branches.append({"op": "must_not", "field": TOKEN_COLUMN, "conds": [field_token(field)]})
        clauses.append(branches[0] if len(branches) == 1 else {"op": "or", "conds": branches})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"op": "and", "conds": clauses}


async def resolve_metadata(fs, ctx, target_uri, meta_filters, meta_hints=()):
    registry = configured_store()
    view = _view.get()
    if registry is None or view is None:
        raise FailedPreconditionError("Business metadata queries require a publication snapshot")
    lease, manifest = await asyncio.to_thread(view.read, ctx.account_id, registry)
    targets = [target_uri] if isinstance(target_uri, str) and target_uri else (target_uri or [])
    uris = [uri for uri in manifest if not targets or any(uri == root.rstrip("/") or uri.startswith(root.rstrip("/") + "/") for root in targets)]
    permitted = await fs._can_access_many(uris, ctx)
    rows = await asyncio.to_thread(registry.metadata_read, ctx.account_id, lease, [uri for uri in uris if permitted.get(uri, False)])
    dependencies = list({ref["uri"] for row in rows for ref in row["dependencies"] if ref["sha256"] is not None})
    dependency_rights = await fs._can_access_many(dependencies, ctx) if dependencies else {}
    rows = [row for row in rows if all(ref["sha256"] is None or dependency_rights.get(ref["uri"], False) for ref in row["dependencies"])]
    dictionary = BusinessDictionary("authorized-snapshot")
    for row in rows:
        if row["payload"]:
            dictionary.add_meta(AuthoritativeMeta.from_payload(row["payload"]))
    filters = parse_constraints(meta_filters)
    hints = []
    if not isinstance(meta_hints, (list, tuple)) or len(meta_hints) > 16:
        raise InvalidArgumentError("meta_hints must contain at most 16 bounded entries")
    for item in meta_hints:
        if not isinstance(item, dict) or set(item) - {"field", "value", "op"}:
            raise InvalidArgumentError("Invalid business metadata hint")
        try:
            field = validate_meta_key(canonical_field_name(item.get("field")), MetaLimits())
        except ValueError as exc:
            raise InvalidArgumentError(str(exc)) from exc
        value = item.get("value")
        if not isinstance(value, str) or len(value) > 512:
            raise InvalidArgumentError("Business metadata hint must contain a bounded string")
        hints.append(MetaHint(field, value, item.get("op", "fuzzy")))
    try:
        resolution = resolve_query(dictionary, meta_filters=filters, meta_hints=hints)
    except ValueError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    predicate = token_filter(resolution)
    # Exclude metadata contributors which the current user cannot read before
    # any recall or rerank, even when a source document itself is permissive.
    authorized = {"op": "must", "field": "uri", "conds": [row["uri"] for row in rows] or ["viking://__no_authorized_metadata_source__"]}
    predicate = {"op": "and", "conds": [authorized, predicate]} if predicate else authorized
    trace = {"hard_fields": {field: list(values) for field, values in resolution.hard_fields.items()},
             "include_unknown_fields": list(resolution.include_unknown_fields), "unsatisfied": list(resolution.unsatisfied),
             "boost_candidates": {field: list(values) for field, values in resolution.boosts.items()},
             "explanations": list(resolution.explanations), "dictionary_cardinality": dictionary.cardinality(),
             "authorized_documents": len(rows), "scan_volume": resolution.scan_volume,
             "hints_policy": "candidate_explanation_only; ranking_boost_pending"}
    trace["_unknown_by_uri"] = {
        row["uri"]: [field for field in resolution.include_unknown_fields
                     if not row["payload"] or not AuthoritativeMeta.from_payload(row["payload"]).canonical(field)]
        for row in rows
    }
    return predicate, trace


class MetadataResult:
    def __init__(self, result, trace):
        self.result, self.trace = result, trace

    def __getattr__(self, name):
        return getattr(self.result, name)

    def to_dict(self, include_provenance=False):
        payload = self.result.to_dict(include_provenance=include_provenance) if hasattr(self.result, "to_dict") else dict(self.result)
        for category in ("resources", "memories", "skills"):
            for row in payload.get(category, []):
                unknown = self.trace.get("_unknown_by_uri", {}).get(row.get("source_uri", row.get("uri")), [])
                if unknown:
                    row["metadata_unknown_fields"] = unknown
        payload["metadata_query"] = {key: value for key, value in self.trace.items() if not key.startswith("_")}
        return payload
