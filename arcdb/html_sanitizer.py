"""Parser-based allowlist sanitizer for untrusted EPUB chapter HTML."""

from __future__ import annotations

from html import escape
from html.parser import HTMLParser
import re


ALLOWED_TAGS = frozenset({
    "a", "abbr", "article", "aside", "b", "bdi", "bdo", "blockquote",
    "br", "caption", "cite", "code", "col", "colgroup", "dd", "del",
    "details", "dfn", "div", "dl", "dt", "em", "figcaption", "figure",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
    "i", "img", "ins", "kbd", "li", "main", "mark", "nav", "ol", "p",
    "pre", "q", "rp", "rt", "ruby", "s", "samp", "section", "small",
    "span", "strong", "sub", "summary", "sup", "table", "tbody", "td",
    "tfoot", "th", "thead", "time", "tr", "u", "ul", "var", "wbr",
})

VOID_TAGS = frozenset({"br", "col", "hr", "img", "wbr"})

# Content inside these elements is executable, active, misleading, or belongs to
# a foreign parsing namespace. Suppress the complete subtree, not just its wrapper.
DROP_CONTENT_TAGS = frozenset({
    "applet", "base", "button", "canvas", "embed", "form", "frame",
    "frameset", "iframe", "input", "math", "meta", "noembed", "noframes",
    "noscript", "object", "script", "select", "style", "svg", "template",
    "textarea",
})

# These forbidden HTML void elements never have an end tag. Dropping one must
# not suppress the safe content that follows it, even when EPUB markup spells
# it as ``<embed>`` rather than XML-style ``<embed />``.
DROP_VOID_TAGS = frozenset({"base", "embed", "frame", "input", "meta"})

GLOBAL_ATTRIBUTES = frozenset({
    "aria-hidden", "aria-label", "class", "dir", "epub:type", "id", "lang",
    "role", "title", "xml:lang",
})

TAG_ATTRIBUTES = {
    "a": frozenset({"href"}),
    "blockquote": frozenset({"cite"}),
    "col": frozenset({"span"}),
    "colgroup": frozenset({"span"}),
    "del": frozenset({"cite", "datetime"}),
    "img": frozenset({"alt", "height", "src", "width"}),
    "ins": frozenset({"cite", "datetime"}),
    "li": frozenset({"value"}),
    "ol": frozenset({"reversed", "start", "type"}),
    "q": frozenset({"cite"}),
    "td": frozenset({"colspan", "headers", "rowspan"}),
    "th": frozenset({"abbr", "colspan", "headers", "rowspan", "scope"}),
    "time": frozenset({"datetime"}),
}

URL_ATTRIBUTES = frozenset({"cite", "href", "src"})
ALLOWED_URL_SCHEMES = frozenset({"http", "https", "mailto"})
_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+.-]*):", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _safe_url(value: str) -> str | None:
    cleaned = _CONTROL_RE.sub("", value).strip()
    if not cleaned:
        return None
    # Browsers ignore embedded ASCII whitespace/control characters while parsing
    # schemes. Validate the same collapsed prefix to stop entity/whitespace tricks.
    scheme_input = re.sub(r"[\x00-\x20\x7f]+", "", cleaned)
    match = _SCHEME_RE.match(scheme_input)
    if match and match.group(1).casefold() not in ALLOWED_URL_SCHEMES:
        return None
    return cleaned


class _EpubHTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self.open_tags: list[str] = []
        self.blocked_tag: str | None = None
        self.blocked_nesting = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.casefold()
        if self.blocked_tag is not None:
            if tag == self.blocked_tag:
                self.blocked_nesting += 1
            return
        if tag in DROP_CONTENT_TAGS:
            if tag in DROP_VOID_TAGS:
                return
            self.blocked_tag = tag
            self.blocked_nesting = 1
            return
        if tag not in ALLOWED_TAGS:
            return

        allowed = GLOBAL_ATTRIBUTES | TAG_ATTRIBUTES.get(tag, frozenset())
        rendered = []
        seen = set()
        for raw_name, raw_value in attrs:
            name = (raw_name or "").casefold()
            if not name or name in seen:
                continue
            seen.add(name)
            if name not in allowed or name.startswith("on") or name == "style":
                continue
            if raw_value is None:
                if name == "reversed":
                    rendered.append(" reversed")
                continue
            value = str(raw_value)
            if name in URL_ATTRIBUTES:
                value = _safe_url(value)
                if value is None:
                    continue
            rendered.append(f' {name}="{escape(value, quote=True)}"')

        self.output.append(f"<{tag}{''.join(rendered)}>")
        if tag not in VOID_TAGS:
            self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        normalized = tag.casefold()
        if self.blocked_tag is not None or normalized in DROP_CONTENT_TAGS:
            return
        self.handle_starttag(normalized, attrs)
        if normalized in self.open_tags:
            self.handle_endtag(normalized)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self.blocked_tag is not None:
            if tag == self.blocked_tag:
                self.blocked_nesting -= 1
                if self.blocked_nesting == 0:
                    self.blocked_tag = None
            return
        if tag not in ALLOWED_TAGS or tag in VOID_TAGS or tag not in self.open_tags:
            return
        while self.open_tags:
            current = self.open_tags.pop()
            self.output.append(f"</{current}>")
            if current == tag:
                break

    def handle_data(self, data: str) -> None:
        if self.blocked_tag is None:
            self.output.append(escape(data, quote=False))

    def close_document(self) -> str:
        self.close()
        while self.open_tags:
            self.output.append(f"</{self.open_tags.pop()}>")
        return "".join(self.output)


def sanitize_epub_html(content: str) -> str:
    """Return a balanced allowlisted fragment; unsafe constructs fail closed."""
    parser = _EpubHTMLSanitizer()
    parser.feed(str(content or ""))
    return parser.close_document()
