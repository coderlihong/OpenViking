"""Revision-bound business sidecars shared by existing index producers."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json

from openviking.indexing.batch import producer_batch_context
from openviking.indexing.business_meta import AuthoritativeMeta, build_meta, merge_meta, normalize_key
from openviking_cli.exceptions import InvalidArgumentError, NotFoundError

SIDECAR = ".business_meta.json"
TOKEN_COLUMN = "business_meta_tokens"
PAYLOAD_COLUMN = "business_meta_payload"
FINGERPRINT_COLUMN = "business_meta_fingerprint"
SOURCE_FINGERPRINT_COLUMN = "business_meta_source_fingerprint"


def field_token(field):
    return "p:" + hashlib.sha256(normalize_key(field).encode()).hexdigest()


def value_token(field, value):
    return "v:" + hashlib.sha256(json.dumps([normalize_key(field), value], ensure_ascii=False).encode()).hexdigest()


def project_meta(payload):
    meta = AuthoritativeMeta.from_payload(payload)
    tokens = []
    for field, value in sorted(meta.values.items()):
        if value.canonical:
            tokens.extend([field_token(field), value_token(field, value.canonical)])
    return {TOKEN_COLUMN: tokens, PAYLOAD_COLUMN: json.dumps(meta.to_payload(), ensure_ascii=False, sort_keys=True),
            FINGERPRINT_COLUMN: meta.fingerprint(),
            SOURCE_FINGERPRINT_COLUMN: hashlib.sha256(json.dumps(
                [meta.normalizer_version, [[field, value.raw, value.canonical, value.source, value.normalizer_version]
                 for field, value in sorted(meta.values.items())]], sort_keys=True, ensure_ascii=False).encode()).hexdigest()}


def strict_json(raw):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate business metadata key")
            result[key] = value
        return result
    result = json.loads(raw.decode("utf-8"), object_pairs_hook=object_pairs,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite metadata value")))
    if not isinstance(result, dict):
        raise ValueError("Business metadata sidecar must contain only a JSON object of business values")
    build_meta(result, document_id="validation", content_revision="validation")
    return result


async def load_business_meta(fs, uri, ctx, *, is_directory=False):
    """Nearest explicit directory sidecar wins, within the current namespace.

    A directory sidecar applies to its entire subtree unless a descendant has
    its own sidecar. Absence dependencies prevent a newly created inner sidecar
    from changing the chosen source halfway through a publication batch.
    """
    from openviking.indexing.publication import configured_store
    from openviking.indexing.content import draft_sources

    registry = configured_store()
    if registry is None:
        return None
    root = "viking://" + uri.removeprefix("viking://").split("/", 1)[0]
    registered = await asyncio.to_thread(registry.namespace_list, ctx.account_id)
    matches = [entry["root_uri"].rstrip("/") for entry in registered
               if uri == entry["root_uri"].rstrip("/") or uri.startswith(entry["root_uri"].rstrip("/") + "/")]
    if matches:
        root = max(matches, key=len)
    directory = uri.rstrip("/") if is_directory else uri.rsplit("/", 1)[0]
    batch = producer_batch_context()
    if batch is not None and not hasattr(batch, "business_meta_inputs"):
        batch.business_meta_inputs = {}
    cache = batch.business_meta_inputs if batch is not None else {}
    key = (ctx.account_id, directory, root)
    if key in cache:
        return cache[key]
    dependencies, values, source = [], None, "absent"
    while directory == root or directory.startswith(root + "/"):
        sidecar = directory + "/" + SIDECAR
        epoch = await asyncio.to_thread(registry.observe_draft, ctx.account_id, sidecar)
        with draft_sources():
            try:
                raw = await fs.read_file_bytes(sidecar, ctx=ctx)
            except (NotFoundError, FileNotFoundError):
                raw = None
        if raw is not None and len(raw) > 64 * 1024:
            raise InvalidArgumentError("Business metadata sidecar exceeds 64 KiB")
        sha = hashlib.sha256(raw).hexdigest() if raw is not None else None
        dependency = {"uri": sidecar, "draft_epoch": epoch, "sha256": sha}
        dependency_key = (ctx.account_id, sidecar)
        if dependency_key in cache and cache[dependency_key] != dependency:
            raise RuntimeError("Business metadata changed between batch producers")
        cache[dependency_key] = dependency
        dependencies.append(dependency)
        if raw is not None:
            try:
                values = strict_json(raw)
            except (ValueError, TypeError, UnicodeError) as exc:
                raise InvalidArgumentError("Invalid business metadata sidecar: " + str(exc)) from exc
            source = sidecar + "@sha256:" + sha
            break
        if directory == root:
            break
        directory = directory.rsplit("/", 1)[0]
    fingerprint = hashlib.sha256(json.dumps([dependencies, values], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    result = {"values": values, "source": source, "dependencies": dependencies, "fingerprint": fingerprint}
    cache[key] = result
    return result


def prepare_meta(base, registry, document_id, content_revision):
    bundle = base.pop("_business_meta_input", None)
    if bundle is None:
        return None
    old = registry.metadata_latest(base["account_id"], base["uri"])
    existing = AuthoritativeMeta.from_payload(old["payload"]) if old and old["payload"] else None
    meta = merge_meta(existing, bundle["values"], document_id=document_id,
                      content_revision=content_revision, source=bundle["source"])
    return {"meta": meta, "bundle": bundle,
            "fingerprint": hashlib.sha256(json.dumps([meta.to_payload(), bundle["fingerprint"]], sort_keys=True, ensure_ascii=False).encode()).hexdigest()}


def bind_meta(prepared, generation):
    if prepared is None:
        return {}
    meta = replace(prepared["meta"], index_generation=generation)
    columns = project_meta(meta.to_payload())
    return columns


def write_meta(registry, generation, prepared):
    if prepared is not None:
        payload = replace(prepared["meta"], index_generation=generation).to_payload()
        registry.metadata_write(generation, payload, prepared["bundle"]["dependencies"])
