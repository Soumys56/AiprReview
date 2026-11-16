from __future__ import annotations

import csv
import html
import io
import json
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

MAX_SPEC_CHARS = 14_000


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._skip_depth += 1
        elif tag.lower() in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag.lower() in {"p", "div", "li", "tr", "h1", "h2", "h3", "section"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def _flatten_xml(content: str) -> str:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid XML specification: {exc}") from exc

    lines: list[str] = []
    def visit(element: ET.Element, depth: int = 0) -> None:
        tag = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else "element"
        attrs = " ".join(f"{key}={value}" for key, value in element.attrib.items())
        text = " ".join((element.text or "").split())
        label = f"{tag}" + (f" [{attrs}]" if attrs else "")
        if text:
            label += f": {text}"
        if element.attrib or text or not list(element):
            lines.append(f"{'  ' * depth}{label}")
        for child in element:
            visit(child, depth + 1)
            tail = " ".join((child.tail or "").split())
            if tail:
                lines.append(f"{'  ' * (depth + 1)}{tail}")
    visit(root)
    return "\n".join(lines)


def _read_xlsx(data: bytes) -> str:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("Reading .xlsx files requires openpyxl; install it with `uv add openpyxl`.") from exc
    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines: list[str] = []
    try:
        for sheet in workbook.worksheets:
            lines.append(f"## Sheet: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                values = [str(value).strip() if value is not None else "" for value in row]
                if any(values):
                    lines.append(" | ".join(values))
    finally:
        workbook.close()
    return "\n".join(lines)


def parse_user_story_attachment(filename: str, content: bytes | str) -> str:
    """Convert supported story/bug attachments to readable, capped plain text."""
    suffix = Path(filename).suffix.lower()
    if isinstance(content, bytes):
        if suffix in {".txt", ".md", ".xml", ".json", ".csv", ".html", ".htm"}:
            text = content.decode("utf-8-sig", errors="replace")
        elif suffix == ".xlsx":
            text = _read_xlsx(content)
        else:
            raise ValueError(f"Unsupported specification file type: {suffix or '(none)'}")
    else:
        if suffix == ".xlsx":
            raise ValueError(".xlsx specifications must be provided as uploaded file bytes.")
        text = content

    if suffix == ".xml":
        text = _flatten_xml(text)
    elif suffix == ".json":
        try:
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON specification: {exc}") from exc
    elif suffix == ".csv":
        rows = csv.reader(io.StringIO(text))
        text = "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)
    elif suffix in {".html", ".htm"}:
        parser = _TextHTMLParser()
        parser.feed(text)
        parser.close()
        text = html.unescape("".join(parser.parts))
    elif suffix not in {".txt", ".md", ".xlsx", ""}:
        raise ValueError(f"Unsupported specification file type: {suffix}")

    text = re.sub(r"[\t\r ]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_SPEC_CHARS:
        marker = "\n\n[Specification truncated at 14,000 characters]"
        text = text[: MAX_SPEC_CHARS - len(marker)].rstrip() + marker
    return text
