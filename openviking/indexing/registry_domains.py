# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Transactional Wiki/namespace repository commands; no backend ACL authority.

Callers perform current actor/backend authorization around I/O. These commands
preserve registry CAS and cannot certify backend permissions or publish Wiki.
"""
import hashlib
import json
import re
import time
from uuid import uuid4

from openviking_cli.exceptions import ConflictError, NotFoundError, PermissionDeniedError, FailedPreconditionError

DOMAIN_COMMANDS = (
    'wiki_schema_get', 'wiki_schema_check', 'wiki_schema_register', 'wiki_schema_refresh',
    'wiki_collection_register', 'wiki_collection_get', 'wiki_collection_domain_check', 'wiki_collection_migrate', 'namespace_get', 'namespace_list',
    'namespace_registration_check', 'namespace_register', 'namespace_set_export',
    'namespace_authorize_exports', 'namespace_check_revision',
    'wiki_plan_get', 'wiki_plan_find', 'wiki_plan_persist', 'wiki_plan_check_epochs',
)


def _inside(root, uri):
    return uri == root or uri.startswith(root + '/')


def _overlaps(a, b):
    return _inside(a, b) or _inside(b, a)


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('Invalid registry revision digest')


def _schema_state(state):
    if state not in {'structural_ready', 'draft_rules_pending'}:
        raise ValueError('Schema capability cannot certify Wiki execution or publication')


class RegistryDomains:
    def wiki_schema_get(self, account, schema_id, version):
        with self.db() as db:
            row = db.execute('SELECT * FROM wiki_schemas WHERE account=? AND schema_id=? AND version=?',
                             (account, schema_id, version)).fetchone()
            if row is None:
                raise NotFoundError(schema_id, 'Wiki schema')
            return dict(row)

    @staticmethod
    def _wiki_schema_check(db, account, schema_id, version, digest):
        old = db.execute('SELECT digest FROM wiki_schemas WHERE account=? AND schema_id=? AND version=?',
                         (account, schema_id, version)).fetchone()
        if old and old['digest'] != digest:
            raise ConflictError('Schema ID/version already names different contents')

    def wiki_schema_check(self, account, schema_id, version, digest):
        with self.db() as db:
            self._wiki_schema_check(db, account, schema_id, version, digest)

    def wiki_schema_register(self, account, schema_id, version, digest, reference, state):
        _digest(digest)
        _schema_state(state)
        ref = json.loads(reference)
        _digest(ref['sha256'])
        if type(ref['size']) is not int or ref['size'] < 0:
            raise ValueError('Invalid schema reference size')
        with self.db() as db:
            self._wiki_schema_check(db, account, schema_id, version, digest)
            db.execute('INSERT OR IGNORE INTO wiki_schemas VALUES (?,?,?,?,?,?)',
                       (account, schema_id, version, digest, reference, state))

    def wiki_schema_refresh(self, account, schema_id, version, digest, state):
        _digest(digest)
        _schema_state(state)
        with self.db() as db:
            updated = db.execute('UPDATE wiki_schemas SET state=? WHERE account=? AND schema_id=? AND version=? AND digest=?',
                                 (state, account, schema_id, version, digest)).rowcount
            if updated != 1:
                raise ConflictError('Schema identity changed while compiler capability was refreshed')

    @staticmethod
    def _collection_domains(db, account, binding):
        target = db.execute('SELECT root_uri FROM kb_namespaces WHERE account=? AND namespace=?',
                            (account, binding['namespace_id'])).fetchone()
        if target is None or not _inside(target['root_uri'], binding['target_uri']):
            raise FailedPreconditionError('Collection target must belong to its registered knowledge namespace')
        sources = [dict(row) for row in db.execute('SELECT namespace,root_uri FROM kb_namespaces WHERE account=?', (account,))
                   if _inside(row['root_uri'], binding['raw_sources_prefix'])]
        if len(sources) != 1 or sources[0]['namespace'] == binding['namespace_id']:
            raise FailedPreconditionError('Collection raw prefix requires an independent registered knowledge namespace')
        return target['root_uri'], sources[0]['namespace'], sources[0]['root_uri']

    def wiki_collection_domain_check(self, account, collection_id):
        with self.db() as db:
            binding = db.execute('SELECT binding FROM wiki_collections WHERE account=? AND collection_id=?', (account, collection_id)).fetchone()
            if binding is None:
                raise NotFoundError(collection_id, 'Wiki collection')
            db.execute("INSERT OR IGNORE INTO wiki_collection_domains(account,collection_id,state) VALUES (?,?,'legacy_domain_pending')", (account, collection_id))
            domain = db.execute('SELECT * FROM wiki_collection_domains WHERE account=? AND collection_id=?', (account, collection_id)).fetchone()
            if domain['state'] != 'registered':
                raise FailedPreconditionError('legacy_domain_pending: explicit administrator collection migration required')
            roots = self._collection_domains(db, account, json.loads(binding['binding']))
            if roots != (domain['target_root'], domain['raw_namespace'], domain['raw_root']):
                raise FailedPreconditionError('Collection registered domain identity changed')
            return dict(domain)

    def wiki_collection_migrate(self, account, collection_id, expected_binding_digest):
        with self.db() as db:
            row = db.execute('SELECT binding FROM wiki_collections WHERE account=? AND collection_id=?', (account, collection_id)).fetchone()
            if row is None:
                raise NotFoundError(collection_id, 'Wiki collection')
            if hashlib.sha256(row['binding'].encode()).hexdigest() != expected_binding_digest:
                raise ConflictError('Collection binding changed before explicit domain migration')
            binding = json.loads(row['binding'])
            roots = self._collection_domains(db, account, binding)
            for other in db.execute('SELECT binding FROM wiki_collections WHERE account=? AND collection_id!=?', (account, collection_id)):
                existing = json.loads(other['binding'])
                if existing['namespace_id'] == binding['namespace_id'] or any(
                    _overlaps(a, b) for a in (existing['target_uri'], existing['raw_sources_prefix'])
                    for b in (binding['target_uri'], binding['raw_sources_prefix'])
                ):
                    raise ConflictError('Legacy collection conflicts with another collection; revise before migration')
            db.execute("INSERT OR REPLACE INTO wiki_collection_domains VALUES (?,?,'registered',?,?,?)", (account, collection_id, *roots))
            return roots

    def wiki_collection_register(self, account, binding):
        body = json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        collection_id, namespace = binding['collection_id'], binding['namespace_id']
        target, raw = binding['target_uri'], binding['raw_sources_prefix']
        if _overlaps(target, raw):
            raise ValueError('Wiki and raw source roots must not overlap')
        with self.db() as db:
            roots = self._collection_domains(db, account, binding)
            schema = db.execute('SELECT digest FROM wiki_schemas WHERE account=? AND schema_id=? AND version=?',
                                (account, binding['schema_id'], binding['schema_version'])).fetchone()
            if schema is None or schema['digest'] != binding['schema_digest']:
                raise ConflictError('Schema identity changed before collection registration')
            for old in db.execute('SELECT collection_id,binding FROM wiki_collections WHERE account=?', (account,)):
                existing = json.loads(old['binding'])
                if old['collection_id'] == collection_id:
                    if old['binding'] != body:
                        raise ConflictError('Collection already exists with a different binding; explicit revision update required')
                    domain = db.execute('SELECT state FROM wiki_collection_domains WHERE account=? AND collection_id=?', (account, collection_id)).fetchone()
                    if domain is None or domain['state'] != 'registered':
                        raise FailedPreconditionError('legacy_domain_pending: explicit administrator collection migration required')
                    return
                if existing['namespace_id'] == namespace or any(
                    _overlaps(old_root, new_root)
                    for old_root in (existing['target_uri'], existing['raw_sources_prefix'])
                    for new_root in (target, raw)
                ):
                    raise ConflictError('Wiki collections require independent target and raw write roots')
            db.execute('INSERT INTO wiki_collections VALUES (?,?,?)', (account, collection_id, body))
            db.execute("INSERT INTO wiki_collection_domains VALUES (?,?,'registered',?,?,?)", (account, collection_id, *roots))

    def wiki_collection_get(self, account, collection_id):
        with self.db() as db:
            row = db.execute('SELECT binding FROM wiki_collections WHERE account=? AND collection_id=?',
                             (account, collection_id)).fetchone()
            if row is None:
                raise NotFoundError(collection_id, 'Wiki collection')
            return json.loads(row['binding'])

    @staticmethod
    def _namespace_bump(db, account):
        db.execute('INSERT OR IGNORE INTO kb_policy_revisions VALUES (?,0)', (account,))
        db.execute('UPDATE kb_policy_revisions SET revision=revision+1 WHERE account=?', (account,))

    @staticmethod
    def _namespace_check(db, account, namespace, root):
        existing = False
        for row in db.execute('SELECT namespace,root_uri FROM kb_namespaces WHERE account=?', (account,)):
            if row['namespace'] == namespace:
                if row['root_uri'] != root:
                    raise ConflictError('Namespace already names a different immutable root')
                existing = True
            elif _overlaps(row['root_uri'], root):
                raise ConflictError('Independent knowledge namespace roots must not overlap')
        return existing

    def namespace_registration_check(self, account, namespace, root):
        with self.db() as db:
            return self._namespace_check(db, account, namespace, root)

    def namespace_register(self, account, namespace, root):
        with self.db() as db:
            if not self._namespace_check(db, account, namespace, root):
                db.execute('INSERT INTO kb_namespaces VALUES (?,?,?)', (account, namespace, root))
                self._namespace_bump(db, account)

    def namespace_get(self, account, namespace):
        with self.db() as db:
            row = db.execute('SELECT root_uri FROM kb_namespaces WHERE account=? AND namespace=?',
                             (account, namespace)).fetchone()
            if row is None:
                raise NotFoundError(namespace, 'Knowledge namespace')
            return row['root_uri']

    def namespace_list(self, account):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT namespace,root_uri FROM kb_namespaces WHERE account=?', (account,))]

    def namespace_set_export(self, account, source, target, principal, allowed):
        if type(allowed) is not bool or source == target:
            raise ValueError('Export requires distinct namespaces and boolean allowed')
        with self.db() as db:
            for namespace in (source, target):
                if db.execute('SELECT 1 FROM kb_namespaces WHERE account=? AND namespace=?', (account, namespace)).fetchone() is None:
                    raise NotFoundError(namespace, 'Knowledge namespace')
            key = (account, source, target, principal)
            old = db.execute('SELECT 1 FROM kb_exports WHERE account=? AND source_namespace=? AND target_namespace=? AND principal=?', key).fetchone()
            if allowed and not old:
                db.execute('INSERT INTO kb_exports VALUES (?,?,?,?)', key)
                self._namespace_bump(db, account)
            elif not allowed and old:
                db.execute('DELETE FROM kb_exports WHERE account=? AND source_namespace=? AND target_namespace=? AND principal=?', key)
                self._namespace_bump(db, account)

    def namespace_authorize_exports(self, account, target, sources, principals):
        with self.db() as db:
            for namespace in set(sources) | {target}:
                if db.execute('SELECT 1 FROM kb_namespaces WHERE account=? AND namespace=?', (account, namespace)).fetchone() is None:
                    raise NotFoundError(namespace, 'Knowledge namespace')
            for source in set(sources):
                if source == target:
                    continue
                allowed = {row[0] for row in db.execute('SELECT principal FROM kb_exports WHERE account=? AND source_namespace=? AND target_namespace=?',
                                                       (account, source, target))}
                if not allowed.intersection(principals):
                    raise PermissionDeniedError('Explicit source-to-target export authorization required')
            row = db.execute('SELECT revision FROM kb_policy_revisions WHERE account=?', (account,)).fetchone()
            return row['revision'] if row else 0

    def namespace_check_revision(self, account, revision):
        with self.db() as db:
            row = db.execute('SELECT revision FROM kb_policy_revisions WHERE account=?', (account,)).fetchone()
            if (row['revision'] if row else 0) != revision:
                raise ConflictError('Namespace export policy changed during source use')

    def wiki_plan_get(self, account, build_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM wiki_build_plans WHERE account=? AND id=?', (account, build_id)).fetchone()
            if row is None:
                raise NotFoundError(build_id, 'Wiki build plan')
            return dict(row)

    def wiki_plan_find(self, account, namespace, idempotency_key):
        with self.db() as db:
            row = db.execute('SELECT * FROM wiki_build_plans WHERE account=? AND namespace=? AND idempotency_key=?',
                             (account, namespace, idempotency_key)).fetchone()
            return dict(row) if row else None

    def _wiki_plan_epochs(self, db, account, body):
        dependencies = [(source['uri'], source['epoch']) for source in body['sources']] + list(body['directories'])
        for uri, epoch in dependencies:
            self._assert_not_writing(db, account, uri)
            row = db.execute('SELECT epoch FROM drafts WHERE account=? AND uri=?', (account, uri)).fetchone()
            if row is None or row['epoch'] != epoch:
                raise ConflictError('Wiki input changed before plan persistence')

    def wiki_plan_check_epochs(self, account, body):
        with self.db() as db:
            self._wiki_plan_epochs(db, account, body)

    def wiki_plan_persist(self, account, namespace, idempotency_key, manifest):
        body = json.loads(manifest)
        if body.get('version') != 1 or body.get('input_kind') not in {'upload', 'uri'} or body.get('authorization_state') != 'namespace_export_pending':
            raise ValueError('Only private prepared Wiki manifests are accepted')
        if body['collection']['namespace_id'] != namespace:
            raise ValueError('Wiki plan namespace does not match collection')
        digest = hashlib.sha256(manifest.encode()).hexdigest()
        with self.db() as db:
            self._wiki_plan_epochs(db, account, body)
            domain = db.execute('SELECT state FROM wiki_collection_domains WHERE account=? AND collection_id=?', (account, body['collection']['collection_id'])).fetchone()
            if domain is None or domain['state'] != 'registered':
                raise FailedPreconditionError('legacy_domain_pending: collection cannot prepare or execute')
            self._collection_domains(db, account, body['collection'])
            binding = db.execute('SELECT binding FROM wiki_collections WHERE account=? AND collection_id=?',
                                 (account, body['collection']['collection_id'])).fetchone()
            if binding is None or hashlib.sha256(binding['binding'].encode()).hexdigest() != body['binding_digest'] or json.loads(binding['binding']) != body['collection']:
                raise ConflictError('Wiki collection changed before plan persistence')
            old = db.execute('SELECT * FROM wiki_build_plans WHERE account=? AND namespace=? AND idempotency_key=?',
                             (account, namespace, idempotency_key)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise ConflictError('Wiki idempotency key already names different input revisions or parameters')
                return dict(old)
            build_id = uuid4().hex
            db.execute('INSERT INTO wiki_build_plans VALUES (?,?,?,?,?,?,?,?)',
                       (build_id, account, namespace, idempotency_key, digest, manifest, 'prepared', time.time()))
            return dict(db.execute('SELECT * FROM wiki_build_plans WHERE id=?', (build_id,)).fetchone())
