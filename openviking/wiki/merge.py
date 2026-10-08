# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Private conservative multi-source candidate grouping, never entity publication.

The service loads job-owned draft receipts through the existing extractor read
and current plan/source/target/export gates. All field values, provenance and
body variants survive; contradictory business values never choose a winner.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json

from openviking.indexing.content import archive_bytes, blob_path
from openviking.indexing.enqueue import enqueue_text_plan
from openviking.wiki.identity import (IdentityPolicy, alias_ownership, canonical_json, identity_tuple,
                                      resolve_candidate_identity)
from openviking.wiki.policies import policy_coverage
from openviking.wiki.namespaces import NamespaceCatalog
from openviking.wiki.schema import SchemaBundle, SchemaError, _yaml, compile_bundle
from openviking.wiki.validation import inspect_entity
from openviking_cli.exceptions import ConflictError, NotFoundError, PermissionDeniedError

MERGE_VERSION = 'wiki-private-candidate-merge-v2'
# Group states that may enter the valid graph. Unresolved conflicts, unresolved
# identity and non-admitted aliases are retained as evidence but never admitted.
_ADMITTED_STATES = {'candidate_grouped', 'human_confirmed'}


def _reinspects(schema, uri, markdown, root):
    try:
        inspect_entity(schema, uri, markdown, namespace_root=root)
    except Exception:
        return False
    return True


def resolve_merge_revision(receipt, *, expected_revision):
    """expected_revision CAS gate for concurrent manual edits.

    A caller writing against a stale merge revision is refused with a conflict
    instead of silently overwriting a newer human/automated edit. ``None`` means
    the caller does not claim a revision and the write is not revision-protected.
    """
    if not isinstance(receipt, dict) or receipt.get('private') is not True or receipt.get('state') != 'private_merge_ready':
        raise SchemaError('Revision-protected write requires a private merge receipt')
    if expected_revision is None:
        return False
    if (not isinstance(expected_revision, str) or receipt.get('revision') != expected_revision):
        raise ConflictError('expected_revision does not match the current merge revision; concurrent edit preserved')
    return True


@dataclass(frozen=True)
class DraftSelection:
    build_id: str
    plan_digest: str
    receipts: tuple[dict, ...]


@dataclass(frozen=True)
class MergeLimits:
    selections: int = 256
    receipts: int = 2048
    candidates: int = 4096
    total_input_bytes: int = 32 * 1024 * 1024
    total_source_bytes: int = 64 * 1024 * 1024
    result_bytes: int = 32 * 1024 * 1024

    def __post_init__(self):
        if any(type(getattr(self, name)) is not int or getattr(self, name) < 1 for name in self.__dataclass_fields__):
            raise ValueError('Merge bounds must be positive integers')


def _frontmatter(markdown):
    lines = markdown.replace('\r\n', '\n').splitlines()
    if not lines or lines[0] != '---':
        raise SchemaError('Candidate frontmatter required')
    try:
        end = lines.index('---', 1)
    except ValueError as exc:
        raise SchemaError('Unterminated candidate frontmatter') from exc
    return _yaml('\n'.join(lines[1:end])), '\n'.join(lines[end + 1:])


class PrivateWikiMerger:
    def __init__(self, extractor, *, limits=None):
        self.extractor, self.planner, self.fs = extractor, extractor.planner, extractor.fs
        self.limits = limits or MergeLimits()
        self.namespaces = NamespaceCatalog(self.fs, registry=self.planner.registry)
        self._last_pin = None

    async def _pin(self, ctx, selection):
        if not isinstance(selection, DraftSelection) or not isinstance(selection.receipts, tuple):
            raise SchemaError('Merge inputs require server-owned draft selections')
        plan = await self.planner.get(ctx, selection.build_id)
        if plan.digest != selection.plan_digest:
            raise ConflictError('Merge source plan digest changed')
        body = json.loads(plan.manifest)
        pin = await self.planner.catalog.pin_collection(ctx, body['collection']['collection_id'], input_kind=body['input_kind'])
        authorization = await self.planner.authorize_inputs(ctx, selection.build_id)
        return plan, pin, authorization

    async def _read_receipt(self, ctx, plan, pin, authorization, receipt):
        # Authorized batch read: per-source current ACL/export gates bracket the
        # account/plan/hash checked internal blob reader. Entire-plan preflight
        # runs once before and after the batch, avoiding N-by-N corpus reads.
        uri = receipt.get('source_uri') if isinstance(receipt, dict) else None
        await self.namespaces.check_source_use(ctx, authorization, uri=uri, target_uri=pin.collection.target_uri)
        payload = await self.extractor._read_payload(ctx, plan, receipt)
        if payload.get('source_uri') != uri:
            raise SchemaError('Private candidate source does not match authorized receipt')
        await self.namespaces.check_source_use(ctx, authorization, uri=uri, target_uri=pin.collection.target_uri)
        return payload

    @staticmethod
    def _binding(pin):
        c = pin.collection
        return (c.namespace_id, c.target_uri, c.schema_id, c.schema_version, c.schema_digest)

    async def merge(self, ctx, selections, *, profile='draft_merge', expected_revision=None,
                    previous_receipt=None, human_confirmed=None):
        if profile != 'draft_merge':
            raise SchemaError('Only private draft_merge profile is supported; diagnostic publication is pending')
        if expected_revision is not None:
            if (not isinstance(expected_revision, str) or len(expected_revision) != 64
                    or any(c not in '0123456789abcdef' for c in expected_revision)):
                raise SchemaError('expected_revision must be a lowercase sha256 revision')
            # CAS: the caller must name the revision they read; a concurrent manual
            # edit produces a different current revision and is preserved, not lost.
            resolve_merge_revision(previous_receipt, expected_revision=expected_revision)
        if human_confirmed is not None:
            if (not isinstance(human_confirmed, dict)
                    or any(not isinstance(name, str) or not name for name in human_confirmed)):
                raise SchemaError('Human-confirmed field overrides must be a name->value mapping')
            canonical_json(human_confirmed)
        if not isinstance(selections, (list, tuple)) or not selections or len(selections) > self.limits.selections:
            raise SchemaError('Merge selection bound exceeded or no selections supplied')
        pinned = [await self._pin(ctx, selection) for selection in selections]
        binding = self._binding(pinned[0][1])
        if any(self._binding(pin) != binding for _, pin, _ in pinned):
            raise SchemaError('Candidate merge cannot cross target namespace, collection root or fixed schema')
        bundle = pinned[0][1].bundle
        policy = IdentityPolicy.compile(bundle)
        schemas = {schema.kind: schema for schema in compile_bundle(bundle)}
        groups, seen, retained, retained_keys, source_texts, count, input_bytes, source_bytes = {}, set(), [], set(), {}, 0, 0, 0
        receipts_count = sum(len(selection.receipts) for selection in selections)
        if not receipts_count or receipts_count > self.limits.receipts:
            raise SchemaError('Merge receipt bound exceeded or no draft receipts supplied')
        for selection, (plan, pin, authorization) in zip(selections, pinned):
            sources = {source.uri: source for source in plan.sources}
            for receipt in selection.receipts:
                ref = receipt.get('reference') if isinstance(receipt, dict) else None
                if (not isinstance(ref, dict) or type(ref.get('size')) is not int or ref['size'] < 0
                        or receipt.get('private') is not True or receipt.get('state') != 'draft_ready'):
                    raise SchemaError('Merge requires a private server-generated draft receipt')
                input_bytes += ref['size']
                if input_bytes > self.limits.total_input_bytes:
                    raise SchemaError('Merge input byte bound exceeded')
                # Current permissions and fixed source bytes are checked by the server.
                payload = await self._read_receipt(ctx, plan, pin, authorization, receipt)
                if (payload.get('source_uri') != receipt.get('source_uri')
                        or type(payload.get('source_chunk')) is not int
                        or payload['source_chunk'] != receipt.get('source_chunk')
                        or not isinstance(payload.get('candidates'), list)
                        or type(receipt.get('candidate_count')) is not int
                        or receipt['candidate_count'] != len(payload['candidates'])):
                    raise SchemaError('Merge draft receipt/source/candidate shape mismatch')
                origin = sources[payload['source_uri']]
                retained_key = (selection.build_id, selection.plan_digest, ref['sha256'])
                if retained_key not in retained_keys:
                    retained.append({'build_id': selection.build_id, 'plan_digest': selection.plan_digest, 'receipt': receipt})
                    retained_keys.add(retained_key)
                for candidate in payload['candidates']:
                    count += 1
                    if count > self.limits.candidates:
                        raise SchemaError('Merge candidate bound exceeded')
                    if not isinstance(candidate, dict) or candidate.get('kind') not in schemas:
                        raise SchemaError('Merge candidate kind is not declared')
                    schema = schemas[candidate['kind']]
                    markdown = candidate.get('markdown')
                    if not isinstance(markdown, str):
                        raise SchemaError('Merge candidate Markdown missing')
                    draft = inspect_entity(schema, candidate.get('uri'), markdown, namespace_root=pin.collection.target_uri)
                    if not draft.uri.startswith(pin.collection.target_uri + '/'):
                        raise SchemaError('Candidate lies outside the configured collection')
                    metadata, body = _frontmatter(markdown)
                    canonical_json(metadata)
                    if metadata.get('publication_state', 'candidate') != 'candidate' or metadata.get('validation_state', 'pending') != 'pending':
                        raise SchemaError('Merge cannot accept model self-certified entity state')
                    evidence = candidate.get('evidence')
                    if (not isinstance(evidence, list) or not evidence
                            or candidate.get('contributors') != [origin.uri]
                            or {r.uri for r in draft.references if r.expected_kind == 'source'} != {origin.uri}):
                        raise SchemaError('Merge candidate contributor/evidence mismatch')
                    # Revalidate exact quote against the fixed source, not model assertions.
                    source_key = (selection.build_id, origin.uri)
                    if source_key not in source_texts:
                        source_bytes += origin.size
                        if source_bytes > self.limits.total_source_bytes:
                            raise SchemaError('Merge source byte bound exceeded')
                        source_texts[source_key] = await self.planner._read_captured(ctx, origin, pin.collection.target_uri)
                    source_text = source_texts[source_key]
                    for proof in evidence:
                        if (not isinstance(proof, dict) or set(proof) != {'claim', 'quote', 'source_uri'}
                                or proof['source_uri'] != origin.uri or not isinstance(proof['quote'], str)
                                or not proof['quote'].strip() or proof['quote'] not in source_text
                                or not isinstance(proof['claim'], str) or not proof['claim'].strip() or proof['claim'] not in body):
                            raise SchemaError('Merge candidate lacks actual source/body quote provenance')
                    digest = hashlib.sha256(canonical_json(candidate).encode()).hexdigest()
                    replay_key = (pin.collection.namespace_id, bundle.digest, origin.uri, origin.sha256, digest)
                    if replay_key in seen:
                        continue  # Exact same source/candidate replay, not a name merge.
                    seen.add(replay_key)
                    identity = resolve_candidate_identity(policy, bundle, metadata, evidence, account_id=ctx.account_id,
                                                          namespace_id=pin.collection.namespace_id, kind=schema.kind)
                    group_id = identity['canonical_id'] or 'unresolved-' + hashlib.sha256(canonical_json(replay_key).encode()).hexdigest()
                    group = groups.setdefault(group_id, {'candidate_id': group_id, 'kind': schema.kind, 'identity': identity,
                                                       'field_observations': {}, 'body_variants': [], 'evidence': [],
                                                       'contributors': [], 'display_names': [], 'provisional_ids': [],
                                                       'source_fields': [f.name for f in schema.fields if f.target == 'source']})
                    provenance = {'source_uri': origin.uri, 'source_sha256': origin.sha256, 'source_epoch': origin.epoch,
                                  'build_id': selection.build_id, 'plan_digest': selection.plan_digest,
                                  'draft_reference': ref, 'candidate_uri': draft.uri, 'candidate_sha256': digest,
                                  'policy_receipts': [asdict(receipt) for receipt in draft.policy_receipts]}
                    for name, value in metadata.items():
                        entries = group['field_observations'].setdefault(name, [])
                        existing = next((entry for entry in entries if canonical_json(entry['value']) == canonical_json(value)), None)
                        if existing is None:
                            entries.append({'value': value, 'provenance': [provenance]})
                        else:
                            existing['provenance'].append(provenance)
                    group['body_variants'].append({'markdown': markdown, 'provenance': provenance})
                    group['evidence'].extend({'proof': proof, 'provenance': provenance} for proof in evidence)
                    if origin.uri not in group['contributors']:
                        group['contributors'].append(origin.uri)
                    for name in ('title', 'object_name'):
                        if isinstance(metadata.get(name), str) and metadata[name] not in group['display_names']:
                            group['display_names'].append(metadata[name])
                    if isinstance(metadata.get('aliases'), list):
                        for alias in metadata['aliases']:
                            if isinstance(alias, str) and alias not in group['display_names']:
                                group['display_names'].append(alias)
                    if isinstance(metadata.get('id'), str) and metadata['id'] not in group['provisional_ids']:
                        group['provisional_ids'].append(metadata['id'])
        confirmed = dict(human_confirmed) if human_confirmed else {}
        for group in groups.values():
            identity_fields = group['identity'].get('components', {})
            identity_scope = set(identity_fields.get('business_key', {})) | set(identity_fields.get('applicability_scope', {}))
            excluded = set(group['source_fields']) | {'id', 'title', 'object_name', 'aliases'} | identity_scope
            group['conflicts'] = [name for name, observations in group['field_observations'].items()
                                  if name not in excluded and len(observations) > 1]
            # Human-confirmed values are protected: model output can never overwrite
            # them. All model observations stay as evidence; the confirmation is a
            # separate, authoritative overlay resolved by a human, not a winner pick.
            human = {}
            for name, value in confirmed.items():
                if name not in {field.name for field in schemas[group['kind']].fields}:
                    raise SchemaError('Human-confirmed field is not declared: ' + name)
                if name in identity_scope:
                    # Identity-defining fields must not be silently rewritten by a hand edit.
                    continue
                human[name] = value
                # A human resolution closes the conflict without discarding either source value.
                group['conflicts'] = [conflict for conflict in group['conflicts'] if conflict != name]
            group['human_confirmed'] = human
            group['human_confirmed_fields'] = sorted(human)
            if group['human_confirmed_fields']:
                group['state'] = 'human_confirmed'
            else:
                group['state'] = ('pending_identity' if group['identity']['canonical_id'] is None
                                  else ('conflict' if group['conflicts'] else 'candidate_grouped'))
            group['alias_suggestions'] = list(group['display_names']) if group['identity']['canonical_id'] else []
            group['alias_ownership_verified'] = False
            group['diagnostic_publishable'] = False
            group['pending_checks'] = ['semantic_identity', 'semantic_fact_conflicts', 'alias_ownership',
                                       'trusted_relation_closure', 'schema_policy', 'joint_publication']
            group['in_valid_graph'] = group['state'] in _ADMITTED_STATES
        ownership = alias_ownership(groups.values())
        # Unresolved conflicts and unresolved identities never enter the valid graph.
        admitted = [group['candidate_id'] for group in groups.values() if group['in_valid_graph']]
        payload = {'version': 1, 'account_id': ctx.account_id, 'namespace_id': binding[0], 'target_uri': binding[1],
                   'schema_id': bundle.schema_id, 'schema_version': bundle.version, 'schema_digest': bundle.digest,
                   'merge_version': MERGE_VERSION, 'profile': profile, 'identity_policy': policy.describe(),
                   'identity_tuple': identity_tuple(account_id=ctx.account_id, namespace_id=binding[0],
                                                    kind=None, schema_id=bundle.schema_id,
                                                    schema_version=bundle.version, schema_digest=bundle.digest),
                   'schema_policy_coverage': policy_coverage(bundle), 'inputs': retained,
                   'groups': sorted(groups.values(), key=lambda group: group['candidate_id']),
                   'alias_ownership': ownership['aliases'], 'alias_collisions': ownership['collisions'],
                   'valid_graph': sorted(admitted),
                   'excluded_from_valid_graph': sorted(group['candidate_id'] for group in groups.values()
                                                       if not group['in_valid_graph']),
                   'state': 'private_merge_ready', 'private': True, 'diagnostic_publishable': False,
                   'human_revision_cas': ('revision_bound' if expected_revision else 'not_supplied'),
                   'expected_revision': expected_revision,
                   'derived_access_policy': 'all_contributing_plans_must_remain_authorized'}
        # Source epochs/membership/ACL/export are checked again before private retention.
        for selection in selections:
            await self.planner.authorize_inputs(ctx, selection.build_id)
        raw = canonical_json(payload).encode()
        if len(raw) > self.limits.result_bytes:
            raise SchemaError('Merge result byte bound exceeded')
        reference = await archive_bytes(self.fs, raw, ctx)
        return {'reference': reference, 'revision': reference['sha256'], 'state': 'private_merge_ready', 'private': True,
                'namespace_id': binding[0], 'schema_digest': bundle.digest, 'profile': profile,
                'group_count': len(groups), 'conflict_count': sum(group['state'] == 'conflict' for group in groups.values()),
                'pending_identity_count': sum(group['state'] == 'pending_identity' for group in groups.values()),
                'valid_graph_count': len(admitted), 'excluded_count': len(groups) - len(admitted),
                'alias_collision_count': len(ownership['collisions']),
                'human_revision_cas': payload['human_revision_cas'],
                'diagnostic_publishable': False}

    async def _plan_pages(self, ctx, payload, pin, bundle, schemas):
        """Collect admitted variants and preflight the whole batch before any write.

        One candidate node = one md. Replayed variants (identical markdown from
        different sources) collapse; two groups claiming one URI with different
        content, or a human-edited page, abort the batch before the first write.
        Every non-source relation target must close against a same-batch page of
        the expected kind or an existing page that revalidates under this schema.
        """
        root = pin.collection.target_uri
        planned, by_uri, existing_cache = [], {}, {}

        async def existing(uri):
            if uri not in existing_cache:
                try:
                    raw = await self.fs.read(uri, ctx=ctx)
                except NotFoundError:
                    existing_cache[uri] = None
                else:
                    existing_cache[uri] = raw.decode() if isinstance(raw, bytes) else raw
            return existing_cache[uri]

        for group in payload['groups']:
            if group['candidate_id'] not in payload['valid_graph'] or group['state'] not in _ADMITTED_STATES:
                continue
            schema = schemas[group['kind']]
            for variant in group['body_variants']:
                markdown, provenance = variant['markdown'], variant['provenance']
                uri = provenance['candidate_uri']
                prior = by_uri.get(uri)
                if prior is not None:
                    if prior['markdown'] != markdown:
                        raise SchemaError('Candidate URI claimed by different content across groups: ' + uri)
                    # A replayed page must keep every original contributor: the
                    # index-level derived gate revokes on any single one of them.
                    contributors = set(prior.get('contributors', [prior['provenance']['source_uri']]))
                    contributors.add(provenance['source_uri'])
                    prior['contributors'] = sorted(contributors)
                    continue
                draft = inspect_entity(schema, uri, markdown, namespace_root=root)
                entry = {'uri': uri, 'markdown': markdown, 'kind': group['kind'], 'draft': draft,
                         'candidate_id': group['candidate_id'], 'provenance': provenance,
                         'contributors': [provenance['source_uri']]}
                by_uri[uri] = entry
                planned.append(entry)
        for entry in planned:
            for reference in entry['draft'].references:
                if not reference.expected_kind or reference.expected_kind == 'source':
                    continue
                target = reference.uri
                if not target.startswith(root + '/'):
                    raise SchemaError('Entity relation target outside the collection: ' + target)
                in_batch = by_uri.get(target)
                if in_batch is not None:
                    if in_batch['kind'] != reference.expected_kind:
                        raise SchemaError('Entity relation target type mismatch: ' + target)
                    continue
                raw = await existing(target)
                if raw is None:
                    raise SchemaError('Entity relation target is missing: ' + target)
                # A fake frontmatter or renamed kind fails re-inspection; the edge
                # never closes on a mere validation claim.
                matched = [kind for kind, schema in sorted(schemas.items())
                           if _reinspects(schema, target, raw, root)]
                if len(matched) != 1 or matched[0] != reference.expected_kind:
                    raise SchemaError('Entity relation target does not validate as '
                                      + reference.expected_kind + ': ' + target)
            current = await existing(entry['uri'])
            if current is not None and current != entry['markdown']:
                raise ConflictError('Published candidate page changed by a later edit: ' + entry['uri'])
        return planned, existing

    async def _index_pages(self, ctx, pages):
        """Enqueue each freshly written page through the shared D1 text-plan path.

        Indexing starts only after every page of the batch is on disk, so a page
        is never retrievable while its relation targets are missing. Chunks cite
        the page URI; no separate retrieval path exists. An environment without
        an embedding queue (unit fixtures) skips silently and reports 0 — the
        publish result always carries the chunk count so callers can see it.
        Content publication mode enqueues only after a verified re-read of the
        on-disk page, exactly like vectorize_file.
        """
        if not pages:
            return 0
        from openviking.core.context import Context, Vectorize
        from openviking.storage.queuefs.embedding_msg_converter import EmbeddingMsgConverter

        try:
            from openviking.utils import embedding_utils

            embedding_config = embedding_utils.get_openviking_config().embedding
            manager = embedding_utils.get_queue_manager()
            queue = manager.get_queue(manager.EMBEDDING)
        except Exception:
            return 0
        chunking = getattr(embedding_config, 'chunking', None)
        publish_content = bool(chunking and chunking.enabled and chunking.publish_content)
        total = 0
        for page in pages:
            uri = page['uri']
            source_snapshot = None
            if publish_content:
                from openviking.indexing.content import archive_source
                from openviking.indexing.metadata_integration import load_business_meta

                source_snapshot, raw = await archive_source(self.fs, uri, ctx)
                content = raw.decode() if isinstance(raw, bytes) else (raw or '')
                source_snapshot['text_sha256'] = hashlib.sha256(content.encode()).hexdigest()
                # Every extraction source must remain authorized for the derived
                # page to stay visible; the registry-level gate reads this list.
                source_snapshot['wiki_contributors'] = sorted(page.get('contributors', []))
                meta_input = await load_business_meta(self.fs, uri, ctx)
            else:
                content = page['markdown']
                meta_input = None
            parent = uri.rsplit('/', 1)[0]
            context = Context(uri=uri, parent_uri=parent or None, is_leaf=True,
                              abstract=page['kind'] + ':' + uri.rsplit('/', 1)[-1],
                              context_type='resource', user=ctx.user, account_id=ctx.account_id)
            context.set_vectorize(Vectorize(text=content or ' '))
            base = EmbeddingMsgConverter.from_context(context)
            if base is None:
                continue
            if source_snapshot:
                base.context_data['_source_snapshot'] = source_snapshot
            if meta_input is not None:
                base.context_data['_business_meta_input'] = meta_input
            total += await enqueue_text_plan(base, content, embedding_config, queue)
        return total

    async def publish(self, ctx, receipt, *, expected_revision):
        """Materialize admitted private groups as real md nodes under the target tree.

        Reads back the private merge payload with full authorization rechecks,
        prefights the complete batch (relation closure, URI/content collisions,
        concurrent human edits) before writing anything, then writes every
        distinct admitted body variant to its candidate_uri (one node = one md).
        Unresolved identity/conflict groups are never materialized. Pages are
        enqueued into the shared text index only after the whole batch is on
        disk; ``indexed_chunks`` reports how many chunk messages were enqueued.
        A crash mid-write leaves some pages on disk; a replay re-enqueues every
        planned page — the preflight pinned each page's content, so re-indexing
        an already-written page is idempotent and no page can stay stranded
        outside the index because it happened to be written before the crash.
        """
        if ctx.bypass_acl:
            raise PermissionDeniedError('Original authorized request context required')
        payload = await self.read_merge(ctx, receipt, expected_revision=expected_revision)
        resolve_merge_revision(receipt, expected_revision=expected_revision)
        bundle = await self._pinned_bundle(ctx, payload)
        schemas = {schema.kind: schema for schema in compile_bundle(bundle)}
        planned, existing = await self._plan_pages(ctx, payload, self._last_pin, bundle, schemas)
        pages = []
        for entry in planned:
            if await existing(entry['uri']) is not None:
                continue
            await self.fs.write(entry['uri'], entry['markdown'], ctx=ctx)
            pages.append({'uri': entry['uri'], 'sha256': hashlib.sha256(entry['markdown'].encode()).hexdigest(),
                          'published': True, 'source_uri': entry['provenance']['source_uri'],
                          'build_id': entry['provenance']['build_id'],
                          'plan_digest': entry['provenance']['plan_digest'],
                          'candidate_id': entry['candidate_id'], 'kind': entry['kind']})
        indexed_chunks = await self._index_pages(ctx, planned)
        return {'state': 'private_published', 'private': True, 'diagnostic_publishable': False,
                'namespace_id': payload['namespace_id'], 'target_uri': payload['target_uri'],
                'schema_digest': bundle.digest,
                'merge_revision': expected_revision, 'pages': pages,
                'published_count': len(pages), 'indexed_chunks': indexed_chunks,
                'group_count': len({entry['candidate_id'] for entry in planned}),
                'diagnostic_note': 'diagnostic graph publication remains gated'}

    async def _pinned_bundle(self, ctx, payload):
        # read_merge re-pinned and re-authorized every input dependency; the schema
        # files still come from the current plan's pinned collection, digest-checked.
        plan_key = next(iter({(item['build_id'], item['plan_digest']) for item in payload['inputs']}))
        plan = await self.planner.get(ctx, plan_key[0])
        if plan.digest != plan_key[1]:
            raise ConflictError('Merge source plan digest changed during publication')
        body = json.loads(plan.manifest)
        pin = await self.planner.catalog.pin_collection(ctx, body['collection']['collection_id'],
                                                        input_kind=body['input_kind'])
        if (pin.collection.namespace_id, pin.collection.target_uri) != (payload['namespace_id'], payload['target_uri']) \
                or pin.bundle.digest != payload['schema_digest']:
            raise PermissionDeniedError('Merge payload does not match the current pinned schema/collection')
        self._last_pin = pin
        return pin.bundle

    async def read_merge(self, ctx, receipt, *, expected_revision):
        if ctx.bypass_acl:
            raise PermissionDeniedError('Original authorized request context required')
        if (not isinstance(receipt, dict) or receipt.get('private') is not True
                or receipt.get('state') != 'private_merge_ready' or receipt.get('revision') != expected_revision):
            raise ConflictError('Private merge revision does not match expected revision')
        ref = receipt.get('reference')
        if (not isinstance(ref, dict) or set(ref) != {'sha256', 'size'} or ref['sha256'] != expected_revision
                or type(ref['size']) is not int or not 0 <= ref['size'] <= self.limits.result_bytes):
            raise SchemaError('Invalid private merge reference')
        raw = self.fs._handle_agfs_read(await self.fs._async_agfs.read(blob_path(self.fs, ctx, ref['sha256'])))
        if len(raw) != ref['size'] or hashlib.sha256(raw).hexdigest() != ref['sha256']:
            raise SchemaError('Private merge result is missing or corrupted')
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise SchemaError('Invalid private merge result') from exc
        if (not isinstance(payload, dict) or payload.get('account_id') != ctx.account_id
                or payload.get('merge_version') != MERGE_VERSION or payload.get('state') != 'private_merge_ready'
                or payload.get('private') is not True or payload.get('diagnostic_publishable') is not False
                or not isinstance(payload.get('inputs'), list) or not payload['inputs']):
            raise PermissionDeniedError('Private merge result does not belong to this account or compiler')
        plan_cache = {}
        for item in payload['inputs']:
            if not isinstance(item, dict) or set(item) != {'build_id', 'plan_digest', 'receipt'}:
                raise SchemaError('Invalid private merge input dependency')
            selection = DraftSelection(item['build_id'], item['plan_digest'], (item['receipt'],))
            plan_key = (selection.build_id, selection.plan_digest)
            if plan_key not in plan_cache:
                plan_cache[plan_key] = await self._pin(ctx, selection)
            plan, pin, authorization = plan_cache[plan_key]
            if (pin.collection.namespace_id, pin.collection.target_uri, pin.bundle.digest) != (
                    payload.get('namespace_id'), payload.get('target_uri'), payload.get('schema_digest')):
                raise PermissionDeniedError('Private merge dependency belongs to another namespace or schema')
            await self._read_receipt(ctx, plan, pin, authorization, item['receipt'])
        # Recheck all plans after private dependency reads before exposing any merged body.
        for build_id, _ in plan_cache:
            await self.planner.authorize_inputs(ctx, build_id)
        return payload
