"""Source-visible image preservation, no invented semantics/chapter membership."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import zipfile

import pymupdf
import pytest
from PIL import Image
from openviking.parse.model_artifact import package_document, safe_extract, validate_document

spec=importlib.util.spec_from_file_location('pdf_preservation_test',Path(__file__).resolve().parents[2]/'deploy/parser-models/pdf_image_preservation.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)

def raster(alpha=False):
    image=Image.new('RGBA' if alpha else 'RGB',(20,20),(255,0,0,255) if alpha else 'red')
    if alpha:
        for x in range(10,20):
            for y in range(20):image.putpixel((x,y),(255,0,0,0))
    stream=io.BytesIO();image.save(stream,format='PNG');return stream.getvalue()

def source_pdf(tmp_path,placements=None,rotation=0,alpha=False):
    document=pymupdf.open()
    for rectangles in (placements or [[(20,30,80,90)]]):
        page=document.new_page(width=200,height=300)
        for rectangle in rectangles:page.insert_image(pymupdf.Rect(rectangle),stream=raster(alpha),keep_proportion=False)
        page.set_rotation(rotation)
    path=tmp_path/'source.pdf';document.save(path);document.close();return path

def result(tmp_path,pdf,model_image=False):
    assets=tmp_path/'assets';assets.mkdir()
    markdown='Original model body.\n'
    if model_image:
        (assets/'model.png').write_bytes(raster());markdown+='![Existing image](model.png)\n'
    return (*helper.preserve_pdf_images(pdf,assets,markdown),assets)

def artifact(tmp_path,pdf,markdown,preservation,assets,backend='mineru'):
    with pymupdf.open(pdf) as document:pages=len(document)
    archive=tmp_path/'artifact.zip'
    package_document(markdown,assets,archive,backend=backend,version='pinned',model_revision='pinned',mode='auto',options={},input_pages=pages,processed_pages=pages,page_ids=list(range(pages)))
    helper.annotate_artifact(archive,preservation)
    target=tmp_path/'unpacked';safe_extract(archive,target)
    return target,validate_document(target,backend=backend,mode='auto',options={},expected_pages=pages)

def test_missing_visible_image_restored_with_bound_provenance(tmp_path):
    pdf=source_pdf(tmp_path);markdown,preservation,assets=result(tmp_path,pdf)
    target,record=artifact(tmp_path,pdf,markdown,preservation,assets)
    assert preservation['visible_placements']==preservation['restored_placements']==1
    occurrence=preservation['placements'][0]
    assert occurrence['source_page_number']==1 and occurrence['source_bbox_pt']==[20,30,80,90]
    assert occurrence['restored'] and not occurrence['model_extracted']
    assert occurrence['chapter_assignment']=='unassigned' and occurrence['interpretation']=='not inferred'
    assert record['assets'][0]['source_pdf_occurrences']==[occurrence]
    assert (target/occurrence['path']).read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    assert markdown.startswith('Original model body.') and 'meaning and chapter membership are unverified' in markdown

def test_identical_model_pixels_are_not_duplicated(tmp_path):
    pdf=source_pdf(tmp_path);markdown,preservation,assets=result(tmp_path,pdf,True)
    assert preservation['visible_placements']==1 and preservation['restored_placements']==0
    assert preservation['placements'][0]['path']=='model.png'
    assert 'Preserved source PDF visuals' not in markdown
    artifact(tmp_path,pdf,markdown,preservation,assets)

@pytest.mark.parametrize('placements',[[[(20,30,80,90)],[(20,30,80,90)]],[[(20,30,80,90),(100,130,160,190)]]])
def test_shared_xref_repeated_placements_remain_distinct(tmp_path,placements):
    pdf=source_pdf(tmp_path,placements);markdown,preservation,assets=result(tmp_path,pdf)
    occurrences=preservation['placements']
    assert len(occurrences)==2 and occurrences[0]['source_xref']==occurrences[1]['source_xref']
    assert len({(o['source_page_index'],o['source_draw_index']) for o in occurrences})==2
    assert len({o['path'] for o in occurrences})==2
    artifact(tmp_path,pdf,markdown,preservation,assets)

@pytest.mark.parametrize('rotation',[90,180,270])
def test_rotation_renders_the_visible_placement(tmp_path,rotation):
    pdf=source_pdf(tmp_path,rotation=rotation);markdown,preservation,assets=result(tmp_path,pdf)
    occurrence=preservation['placements'][0];image=Image.open(assets/occurrence['path'])
    assert image.getpixel((image.width//2,image.height//2))==(255,0,0)
    assert occurrence['page_rotation']==rotation
    artifact(tmp_path,pdf,markdown,preservation,assets)

def test_soft_mask_is_rendered_with_original_transparency(tmp_path):
    pdf=source_pdf(tmp_path,alpha=True);markdown,preservation,assets=result(tmp_path,pdf)
    image=Image.open(assets/preservation['placements'][0]['path'])
    assert image.getpixel((image.width//4,image.height//2))==(255,0,0)
    assert image.getpixel((3*image.width//4,image.height//2))==(255,255,255)
    artifact(tmp_path,pdf,markdown,preservation,assets)

def test_partial_outside_bounds_is_clipped_and_recorded(tmp_path):
    pdf=source_pdf(tmp_path,[[(-20,30,80,90)]]);markdown,preservation,assets=result(tmp_path,pdf)
    occurrence=preservation['placements'][0]
    assert occurrence['source_bbox_pt'][0]==-20 and occurrence['visible_bbox_pt'][0]==0
    artifact(tmp_path,pdf,markdown,preservation,assets)

def test_fully_outside_page_is_not_claimed_visible(tmp_path):
    pdf=source_pdf(tmp_path,[[(220,30,280,90)]]);markdown,preservation,assets=result(tmp_path,pdf)
    assert preservation['visible_placements']==0 and not preservation['placements']
    artifact(tmp_path,pdf,markdown,preservation,assets)

@pytest.mark.parametrize('bad_bbox',[(float('nan'),0,20,20),(0,0,0,20),(0,0,float('inf'),20)])
def test_invalid_displayed_bounds_fail_closed(tmp_path,monkeypatch,bad_bbox):
    pdf=source_pdf(tmp_path);assets=tmp_path/'assets';assets.mkdir()
    monkeypatch.setattr(pymupdf.Page,'get_image_info',lambda *a,**k:[{'bbox':bad_bbox,'xref':0}])
    with pytest.raises(ValueError,match='bounds'):helper.preserve_pdf_images(pdf,assets,'Body')

def test_preservation_count_limit_fails_closed(tmp_path):
    pdf=source_pdf(tmp_path,[[(20,30,80,90)],[(20,30,80,90)]]);assets=tmp_path/'assets';assets.mkdir()
    with pytest.raises(ValueError,match='count exceeds'):helper.preserve_pdf_images(pdf,assets,'Body',max_images=1)

@pytest.mark.parametrize('field,value',[('source_page_index',99),('source_page_number',99),('source_pdf_sha256','f'*64),('asset_sha256','f'*64),('visible_bbox_pt',[0,0,1000,1000]),('restored',0),('page_rotation',45)])
def test_artifact_rejects_forged_source_provenance(tmp_path,field,value):
    pdf=source_pdf(tmp_path);markdown,preservation,assets=result(tmp_path,pdf)
    target,record=artifact(tmp_path,pdf,markdown,preservation,assets)
    record['source_image_preservation']['placements'][0][field]=value
    (target/'parsed.json').write_text(json.dumps(record))
    with pytest.raises(ValueError,match='source PDF image preservation'):validate_document(target,backend='mineru',mode='auto',options={},expected_pages=1)

def test_client_and_worker_protocol_copies_are_identical():
    root=Path(__file__).resolve().parents[2]
    assert (root/'openviking/parse/model_artifact.py').read_bytes()==(root/'deploy/parser-models/model_artifact.py').read_bytes()

def test_textual_path_does_not_suppress_a_real_image_reference(tmp_path):
    pdf=source_pdf(tmp_path);assets=tmp_path/'assets';assets.mkdir();(assets/'model.png').write_bytes(raster())
    markdown,preservation=helper.preserve_pdf_images(pdf,assets,'A code sample mentions `model.png`, without a visual reference.\n')
    assert preservation['restored_placements']==0
    assert '![Source PDF image, page 1](model.png)' in markdown
    artifact(tmp_path,pdf,markdown,preservation,assets)

def test_real_inline_image_is_preserved_with_xref_zero(tmp_path):
    document=pymupdf.open();page=document.new_page(width=200,height=300)
    xref=document.get_new_xref();document.update_object(xref,'<<>>')
    document.update_stream(xref,b'q 40 0 0 40 20 30 cm BI /W 1 /H 1 /BPC 8 /CS /RGB ID \xff\x00\x00 EI Q')
    page.set_contents(xref);pdf=tmp_path/'source.pdf';document.save(pdf);document.close()
    markdown,preservation,assets=result(tmp_path,pdf)
    assert preservation['visible_placements']==1 and preservation['placements'][0]['source_xref']==0
    image=Image.open(assets/preservation['placements'][0]['path'])
    assert image.getpixel((image.width//2,image.height//2))==(255,0,0)
    artifact(tmp_path,pdf,markdown,preservation,assets)

def test_different_crop_does_not_claim_full_source_pixel_coverage(tmp_path):
    pdf=source_pdf(tmp_path);assets=tmp_path/'assets';assets.mkdir()
    Image.new('RGB',(20,10),'red').save(assets/'crop.png')
    markdown,preservation=helper.preserve_pdf_images(pdf,assets,'![Model crop](crop.png)\n')
    assert preservation['restored_placements']==1
    assert preservation['placements'][0]['exact_model_pixel_match'] is False
    assert preservation['placements'][0]['path']!='crop.png'
    artifact(tmp_path,pdf,markdown,preservation,assets)

def test_preservation_cannot_hide_an_empty_model_result(tmp_path):
    pdf=source_pdf(tmp_path);assets=tmp_path/'assets';assets.mkdir()
    with pytest.raises(ValueError,match='Empty model markdown'):helper.preserve_pdf_images(pdf,assets,'  \n')

def test_asset_source_claims_cannot_strip_top_pdf_binding(tmp_path):
    pdf=source_pdf(tmp_path);markdown,preservation,assets=result(tmp_path,pdf)
    target,record=artifact(tmp_path,pdf,markdown,preservation,assets)
    del record['source_image_preservation']
    (target/'parsed.json').write_text(json.dumps(record))
    with pytest.raises(ValueError,match='source PDF image preservation'):validate_document(target,backend='mineru',mode='auto',options={},expected_pages=1)


@pytest.mark.parametrize('backend',['teleocr','logics'])
def test_teleocr_and_logics_artifacts_carry_bound_source_preservation(tmp_path,backend):
    pdf=source_pdf(tmp_path);markdown,preservation,assets=result(tmp_path,pdf)
    assert preservation['restored_placements']==1
    target,record=artifact(tmp_path,pdf,markdown,preservation,assets,backend=backend)
    assert record['backend']==backend
    occurrence=preservation['placements'][0]
    assert record['source_image_preservation']['placements']==[occurrence] and occurrence['restored']
    asset=record['assets'][0]
    assert asset['path']==occurrence['path'] and asset['source_pdf_occurrences']==[occurrence] and asset['restored'] is True
    assert (target/asset['path']).is_file()


async def test_logics_unavailable_reference_stripped_but_preserved_image_kept(tmp_path):
    pdf=source_pdf(tmp_path);assets=tmp_path/'assets';assets.mkdir()
    # Model claims an image it did not ship; preservation restores the real one.
    markdown,preservation=helper.preserve_pdf_images(pdf,assets,'Body.\n\n![Ghost](ghost.png)\n')
    assert preservation['restored_placements']==1
    target,record=artifact(tmp_path,pdf,markdown,preservation,assets,backend='logics')
    assert 'ghost.png' not in record['markdown'] and 'LOGICS_IMAGE_UNAVAILABLE' in json.dumps([w['code'] for w in record['warnings']])
    occurrence=preservation['placements'][0]
    assert occurrence['path'] in {a['path'] for a in record['assets']}
    assert record['assets'][{a['path']:i for i,a in enumerate(record['assets'])}[occurrence['path']]]['source_pdf_occurrences']==[occurrence]
