"""Small portable OOXML package postprocessor for core document properties."""
from __future__ import annotations

import os
import stat
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

DC = "http://purl.org/dc/elements/1.1/"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
ET.register_namespace("w", W)


def _set_text(root: ET.Element, tag: str, value: str) -> None:
    node = root.find(tag)
    if node is None:
        node = ET.SubElement(root, tag)
    node.text = value


def postprocess(path: Path, metadata: dict[str, object]) -> None:
    """Set portable core properties and request field refresh when opened."""
    original_mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary = tempfile.mkstemp(prefix="docskill-", suffix=".docx", dir=path.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == "docProps/core.xml":
                    core = ET.fromstring(data)
                    for key, tag in (("title", f"{{{DC}}}title"), ("author", f"{{{DC}}}creator"),
                                     ("subject", f"{{{DC}}}subject")):
                        if metadata.get(key):
                            _set_text(core, tag, str(metadata[key]))
                    data = ET.tostring(core, encoding="utf-8", xml_declaration=True)
                elif item.filename == "word/settings.xml":
                    settings = ET.fromstring(data)
                    update = settings.find(f"{{{W}}}updateFields")
                    if update is None:
                        update = ET.SubElement(settings, f"{{{W}}}updateFields")
                    update.set(f"{{{W}}}val", "true")
                    data = ET.tostring(settings, encoding="utf-8", xml_declaration=True)
                target.writestr(item, data)
        os.chmod(temporary, original_mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
