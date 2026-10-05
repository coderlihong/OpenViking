"""Transport configuration and capability verification failures."""
import subprocess
import pytest
from openviking.parse.office_converter import OfficeConverter, OfficeConverterConfig, OfficeConversionError

@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in ('OPENVIKING_OFFICE_BINARY','OPENVIKING_OFFICE_EXPECTED_VERSION','OPENVIKING_OFFICE_CHECK_FONTS','OPENVIKING_OFFICE_FONT_INVENTORY_FROM_BINARY'):
        monkeypatch.delenv(key, raising=False)

def test_default_constructors_share_explicit_transport(tmp_path, monkeypatch):
    binary=tmp_path/'wrapper'; binary.write_text('#!/bin/sh\nexit 0\n'); binary.chmod(0o755)
    monkeypatch.setenv('OPENVIKING_OFFICE_BINARY', str(binary))
    monkeypatch.setenv('OPENVIKING_OFFICE_EXPECTED_VERSION', 'LibreOffice frozen')
    monkeypatch.setenv('OPENVIKING_OFFICE_FONT_INVENTORY_FROM_BINARY', 'true')
    for c in (OfficeConverter(),OfficeConverter()):
        assert c.config.binary==str(binary)
        assert c.config.expected_version=='LibreOffice frozen'
        assert c.config.font_inventory_from_binary and c.config.check_fonts

@pytest.mark.parametrize('binary',['relative-wrapper','/missing/soffice'])
def test_transport_requires_absolute_executable(binary, monkeypatch):
    monkeypatch.setenv('OPENVIKING_OFFICE_BINARY',binary)
    with pytest.raises(ValueError,match='executable absolute'): OfficeConverter()

def test_nonexecutable_rejected(tmp_path,monkeypatch):
    path=tmp_path/'wrapper';path.write_text('code')
    monkeypatch.setenv('OPENVIKING_OFFICE_BINARY',str(path))
    with pytest.raises(ValueError,match='executable absolute'):OfficeConverter()

def test_explicit_boolean_override(monkeypatch):
    monkeypatch.setenv('OPENVIKING_OFFICE_CHECK_FONTS','0')
    with pytest.raises(ValueError,match='true or false'):OfficeConverter()
    monkeypatch.setenv('OPENVIKING_OFFICE_CHECK_FONTS','false')
    assert not OfficeConverter().config.check_fonts
    assert OfficeConverter(OfficeConverterConfig()).config.check_fonts

def test_availability_verifies_version(monkeypatch):
    c=OfficeConverter(OfficeConverterConfig(expected_version='LibreOffice frozen'))
    monkeypatch.setattr(c,'_locate_binary',lambda:'/wrapper')
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:subprocess.CompletedProcess([],0,'LibreOffice wrong',''))
    assert not c.is_available()

def test_remote_inventory_uses_wrapper(monkeypatch):
    c=OfficeConverter(OfficeConverterConfig(font_inventory_from_binary=True))
    monkeypatch.setattr(c,'_locate_binary',lambda:'/wrapper')
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv,0,'Noto Sans CJK SC\nLiberation Sans\n','')
    monkeypatch.setattr(subprocess,'run',run)
    assert c._installed_fonts()=={'Noto Sans CJK SC','Liberation Sans'}
    assert calls==[['/wrapper','--font-inventory']]

def test_remote_inventory_failure_closed(monkeypatch):
    c=OfficeConverter(OfficeConverterConfig(font_inventory_from_binary=True))
    monkeypatch.setattr(c,'_locate_binary',lambda:'/wrapper')
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:subprocess.CompletedProcess([],1,'','offline'))
    with pytest.raises(OfficeConversionError) as e:c._installed_fonts()
    assert e.value.reason=='converter_unavailable'

def test_language_attributes_are_not_font_names(tmp_path):
    import zipfile
    path=tmp_path/'language.docx'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('word/document.xml','<w:rFonts w:ascii="Liberation Sans" w:cs="Noto Sans"/><w:lang w:val="en-US" w:cs="ar-SA"/>')
    assert OfficeConverter._declared_fonts(path,'.docx')=={'Liberation Sans','Noto Sans'}

@pytest.mark.parametrize('prefix',['','x:'])
def test_excel_font_name_child_in_styles(tmp_path,prefix):
    import zipfile
    path=tmp_path/'fonts.xlsx'
    declaration='xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main"' if prefix else 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('xl/styles.xml',f"<{prefix}styleSheet {declaration}><{prefix}fonts><{prefix}font><{prefix}name val='Calibri'/></{prefix}font></{prefix}fonts></{prefix}styleSheet>")
    assert OfficeConverter._declared_fonts(path,'.xlsx')=={'Calibri'}

def test_missing_excel_font_is_rejected(tmp_path,monkeypatch):
    import zipfile
    path=tmp_path/'missing.xlsx'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('xl/styles.xml','<styleSheet><fonts><font><name val="Unavailable Excel Font"/></font></fonts></styleSheet>')
    c=OfficeConverter(OfficeConverterConfig())
    monkeypatch.setattr(c,'_installed_fonts',lambda:{'Liberation Sans'})
    with pytest.raises(OfficeConversionError) as error:c._assert_fonts_available(path,'.xlsx')
    assert error.value.reason=='missing_fonts'
    assert error.value.details['missing_fonts']==['Unavailable Excel Font']

def test_ppt_theme_reference_resolves_to_declared_theme_families(tmp_path):
    import zipfile
    path=tmp_path/'theme.pptx'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('ppt/slides/slide1.xml','<a:rPr xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:latin typeface="+mn-lt"/></a:rPr>')
        archive.writestr('ppt/theme/theme1.xml','<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:fontScheme><a:majorFont><a:latin typeface="Heading Font"/></a:majorFont><a:minorFont><a:latin typeface="Body Font"/><a:font script="Hans" typeface="CJK Font"/></a:minorFont></a:fontScheme></a:theme>')
    assert OfficeConverter._declared_fonts(path,'.pptx')=={'Heading Font','Body Font','CJK Font'}

def test_word_namespaced_font_attributes_and_language(tmp_path):
    import zipfile
    path=tmp_path/'word.docx'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:rFonts w:ascii="Body Font" w:cs="Complex Font"/><w:lang w:cs="ar-SA"/></w:document>')
    assert OfficeConverter._declared_fonts(path,'.docx')=={'Body Font','Complex Font'}
