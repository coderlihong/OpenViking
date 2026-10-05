"""Deep reference coverage and budgets measured against actual VLM text tokenizers."""
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

from openviking.indexing.image_context import (
    collect_image_contexts, load_vlm_token_counter, truncate_to_token_budget,
)
from openviking_cli.utils.config.vlm_config import VLMContextTokenizer


def tokenizer_entry(tmp_path, model='vision-test'):
    alphabet = pre_tokenizers.ByteLevel.alphabet()
    tok = Tokenizer(models.BPE(vocab={c: i for i, c in enumerate(alphabet)}, merges=[]))
    tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    path = tmp_path / 'tokenizer.json'
    tok.save(str(path))
    return VLMContextTokenizer(model=model, path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def config(monkeypatch, **vlm):
    settings = SimpleNamespace(vlm=SimpleNamespace(**vlm))
    monkeypatch.setattr('openviking_cli.utils.config.get_openviking_config', lambda: settings)
    return settings


def test_embedding_tokenizer_does_not_claim_vlm_budget(monkeypatch):
    cfg = config(monkeypatch, model='vision-test')
    cfg.embedding = SimpleNamespace(chunking=SimpleNamespace(tokenizer_path='/unrelated', tokenizer_sha256='a'*64))
    assert load_vlm_token_counter() is None


def test_primary_and_fallback_models_require_their_own_tokenizers(tmp_path, monkeypatch):
    entry = tokenizer_entry(tmp_path)
    cfg = config(monkeypatch, model='vision-test', backup=SimpleNamespace(model='backup-vision'), context_tokenizers=[entry])
    with pytest.raises(ValueError, match='every configured model'):
        load_vlm_token_counter()
    cfg.vlm.context_tokenizers.append(entry.model_copy(update={'model': 'backup-vision'}))
    count = load_vlm_token_counter()
    assert count('液压回路') == len('液压回路'.encode())


def test_explicit_tokenizer_hash_failure_is_not_silent_estimator(tmp_path, monkeypatch):
    entry = tokenizer_entry(tmp_path)
    config(monkeypatch, model=entry.model, context_tokenizers=[entry])
    assert load_vlm_token_counter()('pressure') > 0
    # Validate the bytes on every load, even when the same path was loaded before.
    from pathlib import Path
    Path(entry.path).write_text('{}')
    with pytest.raises(ValueError, match='SHA256'):
        load_vlm_token_counter()


def test_final_forward_join_budget_is_checked():
    # A model can tokenize separately budgeted pieces differently at the join.
    # Force the concatenated form to violate the original additive estimate.
    def count(text):
        return len(text) + (40 if '\n…\n' in text else 0)
    output = truncate_to_token_budget('液压a'*500, 50, count_tokens=count)
    assert count(output) <= 50


async def test_references_deeper_than_eight_levels_are_not_dropped():
    root = 'viking://resources/manual'
    deep = root + '/nested'*12
    image = root + '/images/a.png'
    async def ls(uri, **kw):
        depth = uri.count('/nested')
        if uri == deep:
            return [{'name': 'source.md'}]
        if depth < 12:
            return [{'name': 'nested', 'isDir': True}]
        return []
    fs = SimpleNamespace(ls=AsyncMock(side_effect=ls), read_file=AsyncMock(return_value=f'![阀芯]({image})'))
    found = await collect_image_contexts(fs, image, None)
    assert [r['context_uri'] for r in found] == [deep + '/source.md']


async def test_listing_cannot_redirect_outside_discovery_root():
    fs = SimpleNamespace(ls=AsyncMock(return_value=[{'name': 'external', 'isDir': True, 'uri': 'viking://resources/other'}]), read_file=AsyncMock())
    with pytest.raises(ValueError, match='invalid child URI'):
        await collect_image_contexts(fs, 'viking://resources/manual/images/a.png', None)
    fs.read_file.assert_not_awaited()


async def test_duplicate_listing_sources_read_once():
    fs = SimpleNamespace(ls=AsyncMock(return_value=[{'name': 'source.md'}]*2), read_file=AsyncMock(return_value='![阀芯](images/a.png)'))
    found = await collect_image_contexts(fs, 'viking://resources/manual/images/a.png', None)
    assert len(found) == 1
    fs.read_file.assert_awaited_once()


async def test_occurrence_records_actual_budget_and_estimator_distinction():
    fs = SimpleNamespace(ls=AsyncMock(return_value=[{'name': 'source.md'}]), read_file=AsyncMock(return_value='![阀芯](images/a.png)'))
    found = await collect_image_contexts(fs, 'viking://resources/manual/images/a.png', None, token_budget=90, count_tokens=lambda s: len(s.encode()))
    assert found[0]['context_budget_kind'] == 'measured'
    assert found[0]['context_token_count'] == len(found[0]['context'].encode()) <= 90
    assert found[0]['context_token_budget'] == 90
    found = await collect_image_contexts(fs, 'viking://resources/manual/images/a.png', None)
    assert found[0]['context_budget_kind'] == 'estimated'
