"""Preserve visible PDF image placements independently of model extraction.

Pixels/bounds come from the source PDF. This supplies no figure interpretation
or inferred chapter membership and does not turn a model omission into quality
acceptance. Exact pixel matches establish equality only, never crop coverage.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import zipfile
import pymupdf
from markdown_it import MarkdownIt
from PIL import Image, UnidentifiedImageError
from urllib.parse import unquote


def pixel_digest(pix):
    if pix.colorspace is None:
        raise ValueError('Image has no supported color space')
    if pix.colorspace.n != 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    return hashlib.sha256(str((pix.width, pix.height, pix.alpha)).encode() + pix.samples).hexdigest()


def preserve_pdf_images(pdf_path: Path, assets_dir: Path, markdown: str, *, max_images=10000, max_pixels=25_000_000):
    if not isinstance(markdown,str) or not markdown.strip():
        raise ValueError('Empty model markdown cannot be replaced by source image preservation')
    with pdf_path.open('rb') as source:
        source_hash = hashlib.file_digest(source,'sha256').hexdigest()
    image_references = {unquote(child.attrGet('src') or '') for token in MarkdownIt('commonmark').enable('table').parse(markdown) for child in token.children or [] if child.type == 'image'}
    model_pixels = {}
    for path in sorted(assets_dir.rglob('*')):
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 32*1024**2 and path.suffix.lower() in {'.png','.jpg','.jpeg','.webp','.bmp','.tif','.tiff'}:
            try:
                with Image.open(path) as profile:
                    if profile.width * profile.height > max_pixels:
                        continue
                pix = pymupdf.Pixmap(str(path))
                if pix.width * pix.height <= max_pixels:
                    model_pixels.setdefault(pixel_digest(pix), path.relative_to(assets_dir).as_posix())
            except (RuntimeError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
                continue
    placements = []
    additions = []
    with pymupdf.open(pdf_path) as document:
        for page_index, page in enumerate(document):
            unrotated_bounds = page.rect * page.derotation_matrix
            for draw_index, info in enumerate(page.get_image_info(xrefs=True)):
                bbox = info.get('bbox')
                if not isinstance(bbox, (tuple,list)) or len(bbox) != 4 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in bbox):
                    raise ValueError('Invalid displayed PDF image bounds')
                original = pymupdf.Rect(bbox)
                if original.width <= 0 or original.height <= 0:
                    raise ValueError('Invalid displayed PDF image bounds')
                visible = original & unrotated_bounds
                if visible.is_empty:
                    continue
                if len(placements) >= max_images:
                    raise ValueError('Visible PDF image count exceeds preservation limit')
                display = visible * page.rotation_matrix
                if display.width * display.height * 4 > max_pixels:
                    raise ValueError('Visible PDF image render exceeds preservation pixel limit')
                xref = info.get('xref', 0)
                if isinstance(xref,bool) or not isinstance(xref,int) or xref < 0:
                    raise ValueError('Invalid displayed PDF image reference')
                matching_path = None
                # A clipped placement is not equal to the full source raster.
                if xref and visible == original and info.get('width',0) * info.get('height',0) <= max_pixels:
                    try:
                        pix = pymupdf.Pixmap(document,xref)
                        mask_xref = document.extract_image(xref).get('smask',0)
                        if mask_xref:
                            pix = pymupdf.Pixmap(pix,pymupdf.Pixmap(document,mask_xref))
                        if pix.width * pix.height <= max_pixels:
                            matching_path = model_pixels.get(pixel_digest(pix))
                    except (RuntimeError, ValueError):
                        pass
                restored = matching_path is None
                if restored:
                    directory = assets_dir / '__source_pdf_images'
                    directory.mkdir(exist_ok=True)
                    name = f'page_{page_index+1:06d}_draw_{draw_index+1:06d}_{source_hash[:16]}.png'
                    output = directory / name
                    if output.exists() or output.is_symlink():
                        raise ValueError('Source PDF preservation asset collision')
                    # Crop the rendered placement, including soft masks, rotation,
                    # clipping and any original overlay; never a synthetic image.
                    page.get_pixmap(matrix=pymupdf.Matrix(2,2),clip=display,alpha=False).save(output)
                    matching_path = output.relative_to(assets_dir).as_posix()
                with (assets_dir / matching_path).open('rb') as asset:
                    asset_hash = hashlib.file_digest(asset,'sha256').hexdigest()
                record = {'path':matching_path,'asset_sha256':asset_hash,'source_pdf_sha256':source_hash,
                    'source_page_index':page_index,'source_page_number':page_index+1,
                    'source_draw_index':draw_index,'source_xref':xref,
                    'source_bbox_pt':list(original),'visible_bbox_pt':list(visible),'page_bounds_pt':list(unrotated_bounds),
                    'render_bbox_pt':list(display),'page_rotation':page.rotation,
                    'source_coordinates':'unrotated page points','render_coordinates':'rotated page points',
                    'restored':restored,'model_extracted':not restored,
                    'exact_model_pixel_match':not restored,'chapter_assignment':'unassigned',
                    'interpretation':'not inferred',
                    'extraction_method':'source_pdf_visible_region_render' if restored else 'model_asset_exact_pixels'}
                placements.append(record)
                if restored or matching_path not in image_references:
                    additions.append(f'## Source PDF image — page {page_index+1}, placement {draw_index+1}\n\n'
                        f'Source PDF SHA-256: `{source_hash}`. Page {page_index+1}; '
                        f'bounds {list(visible)} points (unrotated). '
                        'This visual is preserved from the source PDF; its meaning and chapter membership are unverified.\n\n'
                        f'![Source PDF image, page {page_index+1}]({matching_path})\n')
    if additions:
        markdown = markdown.rstrip() + '\n\n# Preserved source PDF visuals\n\n' + '\n'.join(additions)
    return markdown, {'version':1,'source_pdf_sha256':source_hash,'scope':'visible embedded/inline PDF image placements; vector-only figures are not identified',
        'model_quality_accepted':False,'visible_placements':len(placements),
        'restored_placements':sum(item['restored'] for item in placements),'placements':placements}


def annotate_artifact(artifact: Path, preservation):
    temporary = artifact.with_suffix('.provenance.zip')
    with zipfile.ZipFile(artifact) as source, zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as output:
        record = json.loads(source.read('parsed.json'))
        record['source_image_preservation'] = preservation
        by_path = {}
        for item in preservation['placements']:
            by_path.setdefault(item['path'],[]).append(item)
        for asset in record['assets']:
            if asset['path'] in by_path:
                asset['source_pdf_occurrences'] = by_path[asset['path']]
                asset['restored'] = any(item['restored'] for item in by_path[asset['path']])
                asset['model_extracted'] = not asset['restored']
        for block in record['blocks']:
            occurrences = [item for path,items in by_path.items() if path in block['text_or_markdown'] for item in items]
            if occurrences:
                block['source_pdf_image_occurrences'] = occurrences
        if preservation['restored_placements']:
            record['warnings'].append({'code':'SOURCE_PDF_IMAGES_RESTORED','count':preservation['restored_placements'],
                'reason':'visible source PDF placements had no exact-pixel model asset; model extraction quality remains unaccepted'})
        for name in source.namelist():
            if name != 'parsed.json':output.writestr(name,source.read(name))
        output.writestr('parsed.json',json.dumps(record,ensure_ascii=False))
    temporary.replace(artifact)
