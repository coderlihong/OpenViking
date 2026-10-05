# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Deterministic, bounded, model-free diagnostic expansion over Wiki Markdown.

Relations are read from the authoritative frontmatter fields declared by the
fixed schema, so expansion never consults a model and never invents an edge.
The configured Profile path and the configuration-free ``asserted_faults`` path
stay separate: a generic assertion is reported as evidence only and never
promotes itself to a configuration-specific applicability or probability.

Fuzzy symptom normalization is delegated to the injected unified-search
callable; this module adds no second retriever.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping

from openviking.wiki.schema import EntitySchema, SchemaError, _yaml
from openviking.wiki.validation import source_uri

_LINK = re.compile(r'\[[^\]]*\]\((viking://[^)\s]+)\)')
_UNDETERMINED = 'undetermined'
_APPLICABLE = 'applicable'
_NOT_APPLICABLE = 'not_applicable'


@dataclass(frozen=True)
class Configuration:
    """Confirmed configuration context; never inferred from generic assertions."""
    anchor_uri: str | None = None
    not_applicable: bool = False


@dataclass(frozen=True)
class DiagnosticNode:
    uri: str
    kind: str
    depth: int
    path: str
    via: str
    detail: str


@dataclass(frozen=True)
class DiagnosticExpansion:
    start_uri: str
    namespace_root: str
    configuration: str
    applicability: str
    configured: tuple[DiagnosticNode, ...]
    generic: tuple[DiagnosticNode, ...]
    unknown: tuple[str, ...]
    gaps: tuple[str, ...]
    truncated: bool


Normalizer = Callable[[str], Awaitable[tuple[str, ...]]]


def _frontmatter(markdown: str):
    lines = markdown.replace('\r\n', '\n').splitlines()
    if not lines or lines[0] != '---':
        raise SchemaError('Entity YAML frontmatter required')
    try:
        end = lines.index('---', 1)
    except ValueError as exc:
        raise SchemaError('Unterminated frontmatter') from exc
    return _yaml('\n'.join(lines[1:end])), lines[end + 1:]


class DiagnosticExpander:
    """Bounded deterministic traversal; the caller supplies the fixed schema."""

    def __init__(self, fs, ctx, schemas: Mapping[str, EntitySchema], *, namespace_root,
                 max_depth=6, max_nodes=256, normalize: Normalizer | None = None):
        if not isinstance(namespace_root, str) or not namespace_root:
            raise SchemaError('A namespace root is required for bounded expansion')
        self.fs, self.ctx = fs, ctx
        self.schemas = dict(schemas)
        self.namespace_root = source_uri(namespace_root)
        self.max_depth, self.max_nodes, self.normalize = max_depth, max_nodes, normalize

    def _inside(self, uri):
        return uri == self.namespace_root or uri.startswith(self.namespace_root + '/')

    def _kind(self, uri, metadata=None):
        """Resolve the entity kind from the top-level segment and declared fields.

        Profile pages live under a Symptom directory, so the declared field set
        disambiguates them without a second path convention.
        """
        top = next((segment for segment in uri[len(self.namespace_root):].strip('/').split('/')
                    if segment in self.schemas), '')
        if metadata:
            candidates = [kind for kind, schema in self.schemas.items()
                          if set(metadata) <= {field.name for field in schema.fields}]
            if len(candidates) == 1:
                return candidates[0]
            if top in candidates:
                return top
        return top

    async def _read(self, uri):
        try:
            markdown = await self.fs.read_file(uri, ctx=self.ctx)
        except Exception:
            return None
        try:
            return _frontmatter(markdown)
        except SchemaError:
            return None

    def _edges(self, kind, metadata):
        """Authoritative relation fields only: never semantic or inferred links."""
        schema = self.schemas.get(kind)
        if schema is None:
            return ()
        result = []
        for field in schema.fields:
            if not field.target or field.target == 'source' or field.kind not in {'string', 'list'}:
                continue
            value = metadata.get(field.name)
            if value is None:
                continue
            items = value if field.kind == 'list' else [value]
            if not isinstance(items, list):
                continue
            for item in items:
                target = item.get(field.reference_key) if isinstance(item, dict) else item
                if not isinstance(target, str) or not target.strip():
                    continue
                try:
                    result.append((field.name, field.target, source_uri(target)))
                except SchemaError:
                    continue
        return tuple(result)

    @staticmethod
    def _body_links(body):
        return tuple(dict.fromkeys(_LINK.findall('\n'.join(body))))

    async def normalize_symptom(self, text):
        """Fuzzy normalization only; keeps in-namespace Symptom candidates."""
        if self.normalize is None:
            return ()
        result = []
        for uri in await self.normalize(text) or ():
            try:
                uri = source_uri(uri)
            except SchemaError:
                continue
            if self._inside(uri) and self._kind(uri) == 'Symptom':
                result.append(uri)
        return tuple(dict.fromkeys(result))

    async def _profiles(self, symptom_uri, body):
        directory = symptom_uri.removesuffix('/index.md') + '/profiles/'
        found = []
        for candidate in self._body_links(body):
            if not candidate.startswith(directory):
                continue
            loaded = await self._read(candidate)
            if loaded is None:
                continue
            metadata, _ = loaded
            if self._kind(candidate, metadata) != 'Profile':
                continue
            if metadata.get('parent_symptom') == symptom_uri:
                found.append((candidate, metadata))
        return found

    async def _walk(self, seeds, path, start_node, *, order=None):
        nodes, gaps = [start_node], []
        visited, truncated = {start_node.uri}, False
        frontier = [(uri, kind, via, detail, 1) for uri, kind, via, detail in seeds]
        while frontier:
            uri, kind, via, detail, depth = frontier.pop(0)
            if uri in visited:
                continue
            if depth > self.max_depth:
                truncated = True
                gaps.append('depth_cap')
                continue
            if len(nodes) >= self.max_nodes:
                truncated = True
                gaps.append('node_cap')
                break
            if not self._inside(uri):
                gaps.append('outside_namespace:' + uri)
                continue
            loaded = await self._read(uri)
            if loaded is None:
                gaps.append('missing_edge:' + via + ':' + uri)
                continue
            metadata, _ = loaded
            actual = self._kind(uri, metadata) or kind
            visited.add(uri)
            nodes.append(DiagnosticNode(uri, actual, depth, path, via, detail))
            edges = self._edges(actual, metadata)
            if order:
                edges = tuple(sorted(edges, key=lambda edge: order.index(edge[0])
                                     if edge[0] in order else len(order)))
            for field, target_kind, target in edges:
                frontier.append((target, target_kind, field, field, depth + 1))
        return nodes, tuple(dict.fromkeys(gaps)), truncated

    async def expand(self, start_uri, *, configuration: Configuration | None = None):
        start_uri = source_uri(start_uri)
        configuration = configuration or Configuration()
        if not self._inside(start_uri):
            raise SchemaError('Diagnostic start is outside the namespace root')
        loaded = await self._read(start_uri)
        if loaded is None:
            raise SchemaError('Diagnostic start is not a readable entity')
        metadata, body = loaded
        kind = self._kind(start_uri, metadata)
        unknown, gaps = [], []
        configured: tuple[DiagnosticNode, ...] = ()
        truncated = False

        if kind == 'Symptom':
            anchor = source_uri(configuration.anchor_uri) if configuration.anchor_uri else None
            profile, anchor_kind = None, 'ConfigSpec'
            for candidate, profile_metadata in await self._profiles(start_uri, body):
                if anchor is None:
                    break
                if anchor == profile_metadata.get('config_spec_uri'):
                    profile, anchor_kind = candidate, 'ConfigSpec'
                    break
                if anchor == profile_metadata.get('device_uri'):
                    profile, anchor_kind = candidate, 'Device'
                    break
            if anchor is None:
                applicability = _UNDETERMINED
                unknown.append('configuration')
            elif configuration.not_applicable:
                # An explicit determination that the configuration does not apply
                # outranks a generic assertion; the generic path stays evidence only.
                applicability = _NOT_APPLICABLE
                unknown.append('profile:not_applicable')
            elif profile is not None:
                applicability = _APPLICABLE
                configured, configured_gaps, truncated = await self._walk(
                    [(anchor, anchor_kind, 'configuration_anchor', 'confirmed configuration anchor'),
                     (profile, 'Profile', 'possible_faults', 'matched configuration profile')],
                    'configured', DiagnosticNode(start_uri, kind, 0, 'configured', 'start', 'canonical symptom'),
                    order=('configuration_anchor', 'possible_faults'))
                gaps.extend(configured_gaps)
            else:
                applicability = _UNDETERMINED
                unknown.append('profile')
        else:
            applicability = _UNDETERMINED
            unknown.append('configuration')

        asserted = tuple((uri, target_kind, field, 'configuration-free source assertion')
                         for field, target_kind, uri in self._edges(kind, metadata)
                         if kind == 'Symptom' and field == 'asserted_faults')
        if kind != 'Symptom':
            asserted = tuple((uri, target_kind, field, 'schema relation')
                             for field, target_kind, uri in self._edges(kind, metadata))
        generic, generic_gaps, generic_truncated = await self._walk(
            asserted, 'generic', DiagnosticNode(start_uri, kind, 0, 'generic', 'start', 'canonical entry'))
        gaps.extend(generic_gaps)
        truncated = truncated or generic_truncated

        if configuration.anchor_uri and not self._inside(source_uri(configuration.anchor_uri)):
            # An anchor outside the namespace is never traversed; it is an explicit gap.
            gaps.append('outside_namespace:' + source_uri(configuration.anchor_uri))
        return DiagnosticExpansion(start_uri, self.namespace_root,
                                   source_uri(configuration.anchor_uri) if configuration.anchor_uri else '',
                                   applicability, tuple(configured), tuple(generic),
                                   tuple(dict.fromkeys(unknown)), tuple(dict.fromkeys(gaps)), truncated)