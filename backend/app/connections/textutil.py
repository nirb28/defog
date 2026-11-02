"""Turn Canvas HTML (assignment descriptions, announcements) into plain text."""

import re
from html import unescape
from html.parser import HTMLParser

_BLOCK = {
    "p",
    "div",
    "br",
    "li",
    "ul",
    "ol",
    "tr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "table",
    "blockquote",
    "pre",
    "hr",
}
_SKIP = {"script", "style", "head", "title"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in _SKIP:
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag in _BLOCK and tag != "li":
            self.parts.append("\n")

    def handle_data(self, data):  # noqa: ANN001
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str | None, limit: int = 20_000) -> str | None:
    if not html:
        return None
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # noqa: BLE001 - malformed HTML: fall back to stripping tags
        text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit] or None
