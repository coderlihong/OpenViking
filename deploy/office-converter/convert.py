"""Isolated CLI using the same product converter implementation.

Input is mounted read-only; the caller owns the copied PDF and its TTL.
"""
import argparse
import json
import shutil
from pathlib import Path

from openviking.parse.office_converter import OfficeConverter, OfficeConverterConfig

parser = argparse.ArgumentParser()
parser.add_argument("source", type=Path)
parser.add_argument("output", type=Path)
parser.add_argument("--expected-version")
args = parser.parse_args()
converter = OfficeConverter(OfficeConverterConfig(expected_version=args.expected_version))
result = converter.convert_sync(args.source)
try:
    shutil.copyfile(result.pdf_path, args.output)
    print(json.dumps({"metadata": result.metadata(), "structure": result.structure, "warnings": result.warnings}))
finally:
    converter.cleanup(result)
