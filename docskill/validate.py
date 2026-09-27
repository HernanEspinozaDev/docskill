"""Structural validation of the generated OOXML ZIP package."""
from __future__ import annotations

import xml.etree.ElementTree as ET
import re
import zipfile
from pathlib import Path

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def inspect_docx_fields(path: Path) -> dict[str, object]:
    """Return lightweight Word field and bookmark facts from a DOCX package."""
    records: list[dict[str, str]] = []
    malformed = 0
    starts: dict[str, list[str]] = {}
    ends: list[str] = []
    anchors: list[str] = []
    heading_count = 0
    heading_bookmark_count = 0
    missing_heading_bookmark_count = 0
    toc_heading_count = 0
    style_ids: set[str] = set()
    with zipfile.ZipFile(path) as package:
        outline_style_ids: set[str] = set()
        if "word/styles.xml" in package.namelist():
            style_root = ET.fromstring(package.read("word/styles.xml"))
            style_ids = {style.get(f"{W}styleId", "")
                         for style in style_root.findall(f"{W}style")}
            outline_style_ids = {
                style.get(f"{W}styleId", "") for style in style_root.findall(f"{W}style")
                if style.find(f"{W}pPr/{W}outlineLvl") is not None
            }
        for member in package.namelist():
            if not re.fullmatch(r"word/(document|header\d+|footer\d+)\.xml", member):
                continue
            root = ET.fromstring(package.read(member))
            location = member.removeprefix("word/").removesuffix(".xml")
            stack: list[dict[str, object]] = []
            for node in root.iter():
                if node.tag == f"{W}fldSimple":
                    instruction = node.get(f"{W}instr", "")
                    field_type = _field_type(instruction)
                    if field_type:
                        records.append({"type": field_type,
                                        "instruction": _normalize_field(instruction),
                                        "location": location})
                    else:
                        malformed += 1
                elif node.tag == f"{W}fldChar":
                    kind = node.get(f"{W}fldCharType")
                    if kind == "begin":
                        stack.append({"instruction": ""})
                    elif kind == "separate":
                        if not stack:
                            malformed += 1
                    elif kind == "end":
                        if stack:
                            instruction = str(stack.pop()["instruction"])
                            field_type = _field_type(instruction)
                            if field_type:
                                records.append({"type": field_type,
                                                "instruction": _normalize_field(instruction),
                                                "location": location})
                            else:
                                malformed += 1
                        else:
                            malformed += 1
                elif node.tag == f"{W}instrText":
                    if stack:
                        stack[-1]["instruction"] = str(stack[-1]["instruction"]) + (node.text or "")
                elif node.tag == f"{W}bookmarkStart":
                    bookmark_id = node.get(f"{W}id", "")
                    name = node.get(f"{W}name", "")
                    starts.setdefault(bookmark_id, []).append(name)
                elif node.tag == f"{W}bookmarkEnd":
                    ends.append(node.get(f"{W}id", ""))
                elif node.tag == f"{W}hyperlink" and node.get(f"{W}anchor"):
                    anchors.append(node.get(f"{W}anchor", ""))
            malformed += len(stack)
            if location == "document":
                toc_heading_count = sum(
                    pstyle.get(f"{W}val", "") == "TOCHeading"
                    for pstyle in root.findall(f".//{W}pPr/{W}pStyle"))
                body = root.find(f"./{W}body")
                body_children = list(body) if body is not None else []
                for index, paragraph in enumerate(body_children):
                    if paragraph.tag != f"{W}p":
                        continue
                    p_style = paragraph.find(f"{W}pPr/{W}pStyle")
                    style_id = p_style.get(f"{W}val", "") if p_style is not None else ""
                    direct_outline = paragraph.find(f"{W}pPr/{W}outlineLvl") is not None
                    if style_id == "TOCHeading":
                        continue
                    if (direct_outline or style_id in outline_style_ids
                            or re.fullmatch(r"Heading[1-6]", style_id)):
                        heading_count += 1
                        preceding_starts: list[ET.Element] = []
                        previous = index - 1
                        while previous >= 0 and body_children[previous].tag == f"{W}bookmarkStart":
                            preceding_starts.append(body_children[previous])
                            previous -= 1
                        valid = any(
                            start.get(f"{W}id", "") in ends
                            and start.get(f"{W}name", "")
                            and starts.get(start.get(f"{W}id", ""), []).count(
                                start.get(f"{W}name", "")) == 1
                            for start in preceding_starts)
                        if valid:
                            heading_bookmark_count += 1
                        else:
                            missing_heading_bookmark_count += 1

    start_ids = set(starts)
    end_ids = set(ends)
    bookmark_errors = (sum(max(0, len(values) - 1) for values in starts.values())
                       + sum(1 for values in starts.values() if not values[0])
                       + len(start_ids ^ end_ids)
                       + sum(max(0, ends.count(value) - 1) for value in end_ids))
    bookmark_names = {name for values in starts.values() for name in values if name}
    missing_anchors = sorted(set(anchors) - bookmark_names)
    counts: dict[str, int] = {}
    for record in records:
        counts[record["type"]] = counts.get(record["type"], 0) + 1
    return {
        "fields": records,
        "field_counts": counts,
        "field_invalid_count": malformed,
        "bookmark_count": len(bookmark_names),
        "bookmark_start_count": sum(len(values) for values in starts.values()),
        "bookmark_end_count": len(ends),
        "bookmark_invalid_count": bookmark_errors,
        "missing_anchor_targets": missing_anchors,
        "heading_count": heading_count,
        "heading_bookmark_count": heading_bookmark_count,
        "missing_heading_bookmark_count": missing_heading_bookmark_count,
        "toc_heading_count": toc_heading_count,
        "style_ids": sorted(style_ids),
    }


def _normalize_field(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _field_type(instruction: str) -> str:
    match = re.match(r"\s*([A-Za-z]+)\b", instruction)
    return match.group(1).upper() if match else ""


def validate_docx(path: Path) -> list[str]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"DOCX is missing or empty: {path}")
    try:
        with zipfile.ZipFile(path) as package:
            bad_member = package.testzip()
            if bad_member:
                raise ValueError(f"Corrupt DOCX ZIP member: {bad_member}")
            document = package.read("word/document.xml")
            ET.fromstring(document)
            if "[Content_Types].xml" not in package.namelist():
                raise ValueError("DOCX package has no [Content_Types].xml")
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError(f"Invalid DOCX package: {exc}") from exc
    return [f"Valid OOXML ZIP and word/document.xml: {path.name}",
            "Visual layout and field updates still require a word processor review."]
