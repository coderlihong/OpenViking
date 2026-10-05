"""Preserve OOXML payloads while making every used Excel sheet printable.

Workbook/worksheet pagination and alignment XML is changed; images, formulas, cell values,
font/value/formula semantics and drawing relationships are preserved. This avoids round-tripping
an Office source through a Python spreadsheet writer that may discard drawings.
"""
from __future__ import annotations

import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as SafeET

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE = "http://schemas.openxmlformats.org/package/2006/relationships"
ET.register_namespace("", MAIN)
ET.register_namespace("r", REL)


def column_number(label):
    value = 0
    for letter in label.upper():
        value = value * 26 + ord(letter) - ord("A") + 1
    return value


def column_name(value):
    label = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        label = chr(65 + remainder) + label
    return label


def _cell(ref):
    match = re.fullmatch(r"\$?([A-Z]+)\$?(\d+)", ref.upper())
    if not match:
        raise ValueError("Invalid Excel cell reference")
    col, row = column_number(match[1]), int(match[2])
    if not 1 <= col <= 16384 or not 1 <= row <= 1048576:
        raise ValueError("Excel used range exceeds format limits")
    return col, row


def _serialize(root, original):
    """Retain namespace bindings used by attribute values such as mc:Ignorable."""
    output = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    bindings = re.findall(rb'xmlns:([\w.-]+)=["\']([^"\']+)', original)
    for prefix, uri in bindings:
        marker = b"xmlns:" + prefix + b"="
        if marker not in output:
            start = output.find(b">", output.find(b"?>") + 2)
            output = output[:start] + b' xmlns:' + prefix + b'="' + uri + b'"' + output[start:]
    return output


def prepare_workbook(source: Path, destination: Path):
    """Return a sheet manifest and a lossless OOXML package with readable paging."""
    changes, manifest = {}, []
    with zipfile.ZipFile(source) as archive:
        workbook_bytes = archive.read("xl/workbook.xml")
        workbook = SafeET.fromstring(workbook_bytes)
        relationships = SafeET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {rel.get("Id"): rel.get("Target") for rel in relationships if rel.get("TargetMode") != "External"}
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(node.itertext()) for node in SafeET.fromstring(archive.read("xl/sharedStrings.xml"))]
        # A narrow source column must wrap, never clip text in the rendered PDF.
        # Values, formulas, fonts and drawings remain unchanged.
        if "xl/styles.xml" in archive.namelist():
            style_bytes = archive.read("xl/styles.xml")
            styles = SafeET.fromstring(style_bytes)
            formats = styles.find(f"{{{MAIN}}}cellXfs")
            if formats is not None:
                for fmt in formats:
                    alignment = fmt.find(f"{{{MAIN}}}alignment")
                    if alignment is None:
                        alignment = ET.SubElement(fmt, f"{{{MAIN}}}alignment")
                    alignment.set("wrapText", "1")
                    alignment.set("shrinkToFit", "0")
                    fmt.set("applyAlignment", "1")
                changes["xl/styles.xml"] = _serialize(styles, style_bytes)
        defined = workbook.find(f"{{{MAIN}}}definedNames")
        if defined is None:
            defined = ET.SubElement(workbook, f"{{{MAIN}}}definedNames")
        for name in list(defined):
            if name.get("name") == "_xlnm.Print_Area":
                defined.remove(name)
        sheets = workbook.find(f"{{{MAIN}}}sheets")
        if sheets is None:
            raise ValueError("Workbook has no sheets")
        for ordinal, sheet in enumerate(sheets):
            name, target = sheet.get("name"), targets.get(sheet.get(f"{{{REL}}}id"))
            if not name or not target:
                raise ValueError("Workbook sheet relationship missing")
            member = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            if not member.startswith("xl/worksheets/") or ".." in member.split("/"):
                raise ValueError("Unsafe workbook sheet relationship")
            original = archive.read(member)
            root = SafeET.fromstring(original)
            max_col = max_row = 0
            column_widths = {}
            for cell in root.findall(f".//{{{MAIN}}}sheetData/{{{MAIN}}}row/{{{MAIN}}}c"):
                if any(child.tag in {f"{{{MAIN}}}v", f"{{{MAIN}}}is", f"{{{MAIN}}}f"} for child in cell):
                    col, row = _cell(cell.get("r", ""))
                    max_col, max_row = max(max_col, col), max(max_row, row)
                    value = cell.find(f"{{{MAIN}}}v")
                    inline = cell.find(f"{{{MAIN}}}is")
                    text = "".join(inline.itertext()) if inline is not None else (value.text or "" if value is not None else "")
                    if cell.get("t") == "s" and value is not None:
                        text = shared[int(value.text)]
                    width = max((len(line) for line in text.splitlines()), default=0) + 4
                    column_widths[col] = max(column_widths.get(col, 12), min(60, width))
            for merge in root.findall(f".//{{{MAIN}}}mergeCell"):
                col, row = _cell(merge.get("ref", "").split(":")[-1])
                max_col, max_row = max(max_col, col), max(max_row, row)
            # Drawings must print even when the sheet contains no cell values.
            if root.find(f"{{{MAIN}}}drawing") is not None:
                rel_member = posixpath.dirname(member) + "/_rels/" + posixpath.basename(member) + ".rels"
                rels = SafeET.fromstring(archive.read(rel_member))
                for rel in rels:
                    if not (rel.get("Type") or "").endswith("/drawing"):
                        continue
                    target = rel.get("Target", "")
                    drawing_member = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.dirname(member) + "/" + target)
                    if rel.get("TargetMode") == "External" or not drawing_member.startswith("xl/drawings/") or ".." in drawing_member.split("/"):
                        raise ValueError("Unsafe Excel drawing relationship")
                    drawing = SafeET.fromstring(archive.read(drawing_member))
                    for anchor in drawing:
                        for item in anchor:
                            if item.tag.rsplit("}", 1)[-1] not in {"from", "to"}:
                                continue
                            coords = {node.tag.rsplit("}", 1)[-1]: int(node.text or "0") for node in item}
                            max_col = max(max_col, coords.get("col", 0) + 2)
                            max_row = max(max_row, coords.get("row", 0) + 8)
            entry = {"name": name, "ordinal": ordinal, "original_state": sheet.get("state", "visible"),
                     "used_range": f"A1:{column_name(max_col)}{max_row}" if max_col and max_row else None,
                     "pagination": "A4 at 100 percent; rows and columns may span multiple pages",
                     "printed": bool(max_col and max_row)}
            manifest.append(entry)
            if not entry["printed"]:
                continue
            for row in root.findall(f"{{{MAIN}}}sheetData/{{{MAIN}}}row"):
                row.attrib.pop("ht", None)
                row.attrib.pop("customHeight", None)
            columns = root.find(f"{{{MAIN}}}cols")
            if columns is None:
                columns = ET.Element(f"{{{MAIN}}}cols")
                data = root.find(f"{{{MAIN}}}sheetData")
                root.insert(list(root).index(data), columns)
            # Retain grouping/hidden flags while replacing clipped column widths.
            for col in columns:
                col.set("width", str(max(column_widths.get(number, 12) for number in range(int(col.get("min", "1")), int(col.get("max", "1")) + 1))))
                col.set("customWidth", "1")
            covered = {number for col in columns for number in range(int(col.get("min", "1")), int(col.get("max", "1")) + 1)}
            for number, width in sorted(column_widths.items()):
                if number not in covered:
                    ET.SubElement(columns, f"{{{MAIN}}}col", min=str(number), max=str(number), width=str(width), customWidth="1")
            sheet.set("state", "visible")
            quoted = "'" + name.replace("'", "''") + "'"
            ET.SubElement(defined, f"{{{MAIN}}}definedName", {"name": "_xlnm.Print_Area", "localSheetId": str(ordinal)}).text = quoted + "!$A$1:$" + column_name(max_col) + "$" + str(max_row)
            setup = root.find(f"{{{MAIN}}}pageSetup")
            if setup is None:
                setup = ET.SubElement(root, f"{{{MAIN}}}pageSetup")
            setup.attrib.update(paperSize="9", orientation="landscape" if max_col > 6 else "portrait", scale="100", fitToWidth="0", fitToHeight="0", usePrinterDefaults="0")
            sheet_pr = root.find(f"{{{MAIN}}}sheetPr")
            if sheet_pr is None:
                sheet_pr = ET.Element(f"{{{MAIN}}}sheetPr")
                root.insert(0, sheet_pr)
            page_pr = sheet_pr.find(f"{{{MAIN}}}pageSetUpPr")
            if page_pr is None:
                page_pr = ET.SubElement(sheet_pr, f"{{{MAIN}}}pageSetUpPr")
            page_pr.set("fitToPage", "0")
            header = root.find(f"{{{MAIN}}}headerFooter")
            if header is None:
                header = ET.SubElement(root, f"{{{MAIN}}}headerFooter")
            for kind in ("oddHeader", "evenHeader", "firstHeader"):
                child = header.find(f"{{{MAIN}}}{kind}")
                if child is None:
                    child = ET.SubElement(header, f"{{{MAIN}}}{kind}")
                if "&A" not in (child.text or ""):
                    child.text = (child.text or "") + "&R&A"
            changes[member] = _serialize(root, original)
        if not any(entry["printed"] for entry in manifest):
            raise ValueError("Workbook contains no used sheets")
        changes["xl/workbook.xml"] = _serialize(workbook, workbook_bytes)
        with zipfile.ZipFile(destination, "w") as output:
            for entry in archive.infolist():
                output.writestr(entry, changes.get(entry.filename, archive.read(entry.filename)))
    return manifest
