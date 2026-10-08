# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""I064 filter-note root cause: prefix on a non-URI field must fail loudly.

The legacy dict filter DSL compiles ``prefix`` into a URI path scan only for
``uri``/``parent_uri``. On any other field the value used to be dropped
silently, so a caller combining e.g. ``chunk_id`` prefix with a uri PathScope
got an empty result set instead of an error and could not tell filtering from
matching failure.
"""

import pytest

from openviking.storage.vectordb_adapters.base import CollectionAdapter
from openviking_cli.exceptions import InvalidArgumentError


class _BareAdapter(CollectionAdapter):
    _URI_FIELD_NAMES = {"uri", "parent_uri"}

    def _create_backend_collection(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def _load_existing_collection_if_needed(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    @classmethod
    def from_config(cls, config):  # pragma: no cover
        raise NotImplementedError

    def _normalize_filter_payload_for_write(self, payload):
        return CollectionAdapter._normalize_filter_payload_for_write(self, payload)


def _bare() -> _BareAdapter:
    return object.__new__(_BareAdapter)


def test_prefix_on_non_uri_field_raises_instead_of_dropping():
    adapter = _bare()
    with pytest.raises(InvalidArgumentError, match="chunk_id"):
        adapter._normalize_filter_payload_for_write(
            {"op": "prefix", "field": "chunk_id", "prefix": "image:"}
        )


def test_prefix_on_uri_field_still_encodes():
    adapter = _bare()
    payload = adapter._normalize_filter_payload_for_write(
        {"op": "prefix", "field": "uri", "prefix": "viking://resources/raw/x"}
    )
    assert payload["prefix"] == "/resources/raw/x"


def test_and_of_uri_prefix_and_chunk_id_must_is_preserved():
    # The I064 combination: uri prefix AND chunk_id exact-match condition must
    # survive compilation as two conds (exact match on non-URI fields is
    # legal; only prefix was silently broken).
    adapter = _bare()
    compiled = adapter._compile_filter(
        {
            "op": "and",
            "conds": [
                {"field": "uri", "prefix": "viking://resources/manual"},
                {"op": "must", "field": "chunk_id", "conds": ["image:abc"]},
            ],
        }
    )
    assert compiled["op"] == "and"
    assert len(compiled["conds"]) == 2
