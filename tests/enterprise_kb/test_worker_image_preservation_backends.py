"""TeleOCR/Logics keep the same source-PDF image preservation as MinerU."""
import io
import json
from pathlib import Path
import sys
import zipfile

import pymupdf
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy' / 'parser-models'))
import worker


def raster():
    stream = io.BytesIO()
    Image.new('RGB', (20, 20), 'red').save(stream, format='PNG')
    return stream.getvalue()


def pdf_with_image(path):
    document = pymupdf.open()
    page = document.new_page(width=200, height=300)
    page.insert_image(pymupdf.Rect(20, 30, 80, 90), stream=raster(), keep_proportion=False)
    document.save(path)
    document.close()


def layout(tmp_path, backend):
    """Real per-backend output shapes produced by each engine container."""
    directory = tmp_path / f'job-{backend}'
    (directory / 'input').mkdir(parents=True)
    pdf_with_image(directory / 'input/input.pdf')
    output = directory / 'output'
    output.mkdir()
    if backend == 'teleocr':
        assets = output / 'input'
        assets.mkdir()
        (assets / 'input.md').write_text('# OCR body without images\n')
        (assets / 'input_middle.json').write_text(json.dumps({'pdf_info': [{'page_idx': 0}]}))
    else:
        assets = output
        (output / 'result.json').write_text(
            json.dumps({'complete_generation': True, 'windows': [{'pages': [0]}]}))
        (output / 'markdown.md').write_text('# Logics body without images\n')
    return directory, assets


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    locks = tmp_path / 'locks'
    locks.mkdir()
    for backend in ('mineru', 'teleocr', 'logics'):
        (locks / f'{backend}.commit.txt').write_text('commit-1\n')
    for name in ('mineru', 'teleocr', 'logics', 'x'):
        weights = tmp_path / 'weights' / name
        weights.mkdir(parents=True, exist_ok=True)
        (weights / 'official-snapshot.json').write_text(json.dumps({'sha': 'rev-1'}))
    monkeypatch.setattr(worker, 'ROOT', tmp_path)
    monkeypatch.setattr(worker, 'WEIGHTS', tmp_path / 'weights' / 'x')
    return worker


@pytest.mark.parametrize('backend', ['mineru', 'teleocr', 'logics'])
def test_every_backend_preserves_dropped_source_images(tmp_path, worker_env, backend):
    worker.BACKEND = backend
    directory, assets = layout(tmp_path, backend)
    if backend == 'mineru':
        (worker.ROOT / 'locks/mineru-models.json').write_text(json.dumps({'revision': 'rev-1'}))
        engine_output = directory / 'engine'
        engine_output.mkdir()
        (engine_output / 'markdown.md').write_text('# MinerU body\n')
        (engine_output / 'middle_json.json').write_text(json.dumps({'pdf_info': [{'page_idx': 0}]}))
        assets = engine_output
    worker.normalize({'mode': 'auto', 'options': '{}'}, directory, *( [assets] if backend == 'mineru' else [] ))
    with zipfile.ZipFile(directory / 'artifact.zip') as archive:
        record = json.loads(archive.read('parsed.json'))
        markdown = record['markdown']
    preservation = record['source_image_preservation']
    assert preservation['visible_placements'] == preservation['restored_placements'] == 1
    placement = preservation['placements'][0]
    assert placement['restored'] is True and placement['model_extracted'] is False
    assert 'Preserved source PDF visuals' in markdown
