"""Minimal xlsx reader. Stdlib only.

An .xlsx is a zip of XML, so reading one needs no dependency. `openpyxl` is not
installed and adding a dependency to read six files -- when the format is a
documented zip and the need is read-only -- is not a trade worth making
(CLAUDE.md: "No dependency added without asking").

Handles what NPCI's published workbooks actually use: shared strings, inline
strings, numbers, and merged-header rows. It does not handle formulas, dates as
serial numbers, or styling, and it says so rather than guessing.
"""

from __future__ import annotations

import pathlib
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
CELL_REF = re.compile(r"([A-Z]+)(\d+)")


def _column_index(reference: str) -> int:
    """`C` -> 2. Column letters are base-26 with no zero."""
    match = CELL_REF.match(reference)
    letters = match.group(1) if match else reference
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


@dataclass(frozen=True)
class Sheet:
    name: str
    rows: list[list[str]]

    def non_empty_rows(self) -> list[list[str]]:
        return [row for row in self.rows if any(cell.strip() for cell in row)]


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    strings = []
    for item in root.findall("m:si", NS):
        # A string can be split across runs; concatenate every text node.
        strings.append("".join(node.text or "" for node in item.iter() if node.text))
    return strings


def _sheet_names(archive: zipfile.ZipFile) -> dict[str, str]:
    """rId -> sheet name, so sheets can be reported by their real names."""
    root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relationships = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    return {
        sheet.attrib.get(relationships, ""): sheet.attrib.get("name", "")
        for sheet in root.findall("m:sheets/m:sheet", NS)
    }


def _relationship_targets(archive: zipfile.ZipFile) -> dict[str, str]:
    path = "xl/_rels/workbook.xml.rels"
    if path not in archive.namelist():
        return {}
    root = ElementTree.fromstring(archive.read(path))
    return {
        node.attrib.get("Id", ""): node.attrib.get("Target", "")
        for node in root
    }


def read(path: pathlib.Path | str) -> list[Sheet]:
    """Every sheet in the workbook, as rows of strings."""
    path = pathlib.Path(path)
    sheets: list[Sheet] = []
    with zipfile.ZipFile(path) as archive:
        strings = _shared_strings(archive)
        names = _sheet_names(archive)
        targets = _relationship_targets(archive)

        for rid, name in names.items():
            target = targets.get(rid, "")
            member = f"xl/{target.lstrip('/')}" if target else ""
            if member not in archive.namelist():
                continue
            sheets.append(Sheet(name=name, rows=_rows(archive.read(member), strings)))
    return sheets


def _rows(payload: bytes, strings: list[str]) -> list[list[str]]:
    root = ElementTree.fromstring(payload)
    rows: list[list[str]] = []
    for row_node in root.findall("m:sheetData/m:row", NS):
        cells: dict[int, str] = {}
        for cell in row_node.findall("m:c", NS):
            reference = cell.attrib.get("r", "")
            index = _column_index(reference) if reference else len(cells)
            cells[index] = _cell_value(cell, strings)
        width = max(cells) + 1 if cells else 0
        rows.append([cells.get(i, "") for i in range(width)])
    return rows


def _cell_value(cell: ElementTree.Element, strings: list[str]) -> str:
    kind = cell.attrib.get("t", "n")
    if kind == "s":
        value = cell.find("m:v", NS)
        if value is not None and value.text is not None:
            index = int(value.text)
            return strings[index] if index < len(strings) else ""
        return ""
    if kind == "inlineStr":
        node = cell.find("m:is", NS)
        return "".join(part.text or "" for part in node.iter() if part.text) if node is not None else ""
    value = cell.find("m:v", NS)
    return value.text or "" if value is not None else ""
