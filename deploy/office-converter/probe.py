"""Synthetic six-format contract probe; never promote these inputs to gold."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys

import pymupdf

from openviking.parse.office_converter import OfficeConverter, OfficeConverterConfig, OfficeConversionError


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def main():
    source_root, output = map(Path, sys.argv[1:3])
    output.mkdir(parents=True, exist_ok=True)
    converter = OfficeConverter(OfficeConverterConfig(temp_root=output / "temporary", check_fonts=False))
    observations = []
    for source in sorted(source_root.iterdir()):
        if source.suffix not in converter.supported_extensions:
            continue
        before = checksum(source)
        result = await converter.convert(source)
        try:
            with pymupdf.open(result.pdf_path) as pdf:
                text = "\n".join(page.get_text() for page in pdf)
                compact = "".join(text.split())
                expected = {"word": ["WORD_HEAD", "WORD_TABLE", "WORD_TAIL", "PressureMPa"],
                            "excel": ["EXCEL_HEAD", "EXCEL_TAIL", "MERGED_CELL", "HIDDEN_SHEET_VALUE", "PressureMPa"],
                            "presentation": ["SLIDE_ALPHA", "SLIDE_OMEGA", "SLIDE_TABLE", "TorqueNm"]}[result.kind]
                assert all(token in compact for token in expected), (source.name, text)
                images = sum(len(page.get_images()) for page in pdf)
                assert images > 0
                if result.kind == "presentation":
                    assert len(pdf) == 2 and "SLIDE_ALPHA" in pdf[0].get_text() and "SLIDE_OMEGA" in pdf[1].get_text()
                destination = output / (source.name + ".pdf")
                destination.write_bytes(result.pdf_path.read_bytes())
                observations.append({"source": source.name, "source_sha256": before,
                    "pdf_sha256": checksum(destination), "pages": len(pdf), "images": images,
                    "structure": result.structure, "version": result.converter_version,
                    "status": "CONTRACT_PASS", "sample_class": "synthetic_not_gold"})
            assert checksum(source) == before
        finally:
            converter.cleanup(result)
    assert len(observations) == 6
    bad = output / "corrupt.docx"
    bad.write_text("invalid ZIP")
    try:
        await converter.convert(bad)
        raise AssertionError("Corrupt source was accepted")
    except OfficeConversionError as exc:
        assert exc.reason == "corrupt"
    bad.unlink()
    source = source_root / "word.docx"
    timed = OfficeConverter(OfficeConverterConfig(temp_root=output / "timeout", timeout=.001, check_fonts=False))
    try:
        await timed.convert(source)
        raise AssertionError("Actual LibreOffice exceeded the wall-clock timeout")
    except OfficeConversionError as exc:
        assert exc.reason == "timeout", exc.reason
    assert list(timed.temp_root.iterdir()) == []
    task = asyncio.create_task(converter.convert(source))
    await asyncio.sleep(.05)
    task.cancel()
    try:
        await task
        raise AssertionError("Conversion finished before the cancellation probe")
    except asyncio.CancelledError:
        pass
    assert list(converter.temp_root.iterdir()) == []
    report = {"status": "CONTRACT_PASS", "samples": observations,
              "corrupt_rejected": True, "actual_libreoffice_timeout_cleanup": True,
              "actual_libreoffice_cancellation_cleanup": True,
              "input_unchanged": True, "quality_gold": "NOT_RUN"}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report))


asyncio.run(main())
