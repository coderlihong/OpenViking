"""G3: strict_diagnostic publication admission reads real receipts.

The six former pending labels become named checks backed by four receipt
readers — G1 catalog rows, canonical identity resolution, the admitted merge
graph with its contributor stamps, and the seven durable joint-publication
gate observations. No receipt object, no admission.
"""
import pytest

from openviking.wiki.policies import check_build_profile, publication_adjudication
from openviking.wiki.schema import SchemaError

from test_wiki_page_rules import bundle

ONE = 'viking://resources/raw/manual/pump.md'


@pytest.fixture(scope='module')
def adapted():
    return bundle()[1]


def green_readers():
    return (
        lambda: [{'kind': 'Fault', 'uri': 'viking://resources/sany/Fault/x.md'}],
        lambda: {'canonical': 'viking://resources/sany/Fault/x.md'},
        lambda: {'groups': [{'candidate_id': 'g1', 'kind': 'Fault',
                             'contributors': [ONE], 'pending_identity': None}]},
        lambda: {'verified': True, 'missing': {}},
    )


def refuse(adapted, adjudication):
    with pytest.raises(SchemaError) as err:
        check_build_profile(adapted, 'strict_diagnostic', adjudication=lambda: adjudication)
    return str(err.value)


# ── Entry gates ──────────────────────────────────────────────────────────────

def test_invalid_profile_and_non_callable_reader_are_rejected(adapted):
    with pytest.raises(SchemaError, match='draft or strict_diagnostic'):
        check_build_profile(adapted, 'publish')
    with pytest.raises(SchemaError, match='four receipt readers'):
        publication_adjudication(lambda: [], None, lambda: {}, lambda: {})


def test_draft_profile_passes_without_any_adjudication(adapted):
    coverage = check_build_profile(adapted, 'draft')
    assert 'publication_adjudicated' not in coverage


def test_strict_without_receipt_object_stays_statically_pending(adapted):
    with pytest.raises(SchemaError, match='WIKI_PUBLICATION_GATE_PENDING'):
        check_build_profile(adapted, 'strict_diagnostic')


# ── The four receipt-backed states ───────────────────────────────────────────

def test_green_receipts_admit_and_stamp_the_coverage(adapted):
    adjudication = publication_adjudication(*green_readers())
    assert adjudication == {}
    coverage = check_build_profile(adapted, 'strict_diagnostic', adjudication=lambda: adjudication)
    assert coverage['publication_adjudicated'] is True


def test_single_missing_gate_is_named_in_the_refusal(adapted):
    read, entry, payload, observation = green_readers()
    text = refuse(adapted, publication_adjudication(lambda: [], entry, payload, observation))
    assert 'trusted_entity_catalog' in text


def test_unverified_joint_observation_names_the_durable_gaps(adapted):
    read, entry, payload, _ = green_readers()
    text = refuse(adapted, publication_adjudication(
        read, entry, payload, lambda: {'verified': False, 'missing': ['bm25', 'semantic_levels']}))
    assert 'atomic_joint_publication' in text and 'bm25' in text and 'semantic_levels' in text


def test_merge_graph_without_contributors_blocks_derived_intersection(adapted):
    read, entry, _, observation = green_readers()
    text = refuse(adapted, publication_adjudication(
        read, entry, lambda: {'groups': [{'candidate_id': 'g1', 'kind': 'Fault'}]}, observation))
    assert 'derived_access_intersection' in text


def test_pending_identity_in_merge_graph_blocks_identity_resolution(adapted):
    read, entry, _, observation = green_readers()
    adjudication = publication_adjudication(
        read, entry,
        lambda: {'groups': [{'candidate_id': 'g1', 'kind': 'Fault',
                             'contributors': [ONE], 'pending_identity': 'E828?'}]},
        observation)
    assert set(adjudication) == {'schema_identity_resolution'}
    assert 'schema_identity_resolution' in refuse(adapted, adjudication)


def test_raising_reader_becomes_a_named_unreadable_gate(adapted):
    def broken():
        raise RuntimeError('catalog backend down')
    read, entry, payload, observation = green_readers()
    text = refuse(adapted, publication_adjudication(broken, entry, payload, observation))
    assert 'trusted_entity_catalog' in text and 'unreadable' in text
