"""Markdown to sanitized HTML, for rendered READMEs and .md files in the forge web UI.

GitHub-flavoured: tables, strikethrough, task lists, bare URLs as links, and raw HTML (centred logos, badges,
<details>). Raw HTML goes through nh3 (ammonia) with an allowlist close to GitHub's: no script, no event
handlers, no style, and links/images only to http(s), mailto or relative URLs. The web UI lays the result
out with Qt's rich-text engine, which can't run script anyway; the sanitizing keeps the endpoint safe for
any other client too.
"""

from __future__ import annotations

import re
from functools import lru_cache

import nh3
from markdown_it import MarkdownIt
from mdit_py_plugins.tasklists import tasklists_plugin

TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "center", "code", "dd", "del", "details", "div", "dl", "dt",
    "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins", "kbd", "li", "ol", "p", "picture", "pre",
    "q", "s", "samp", "small", "source", "span", "strike", "strong", "sub", "summary", "sup", "table", "tbody",
    "td", "tfoot", "th", "thead", "tr", "tt", "u", "ul", "var",
}
ATTRIBUTES = {
    "*": {"align", "title", "lang", "dir"},
    "a": {"href", "name"},
    "img": {"src", "alt", "width", "height"},
    "source": {"srcset", "media", "type"},
    "ol": {"start", "type"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
    "details": {"open"},
    "code": {"class"},
}
SCHEMES = {"http", "https", "mailto"}

# Task-list checkboxes become characters: the UI's rich text has no form controls.
_CHECKBOX = re.compile(r'<input class="task-list-item-checkbox"( checked="checked")?[^>]*>')


@lru_cache(maxsize=1)
def _parser() -> MarkdownIt:
    md = MarkdownIt("gfm-like", {"html": True, "linkify": True, "typographer": False})
    return md.use(tasklists_plugin)


def render(text: str) -> str:
    html = _parser().render(text)
    html = _CHECKBOX.sub(lambda m: "\u2611" if m.group(1) else "\u2610", html)
    return nh3.clean(html, tags=TAGS, attributes=ATTRIBUTES, url_schemes=SCHEMES, link_rel=None,
                     strip_comments=True)


_cache: dict[str, str] = {}


def render_cached(blob_sha: str, text: str) -> str:
    """render(), remembered per blob (the same README shows on every visit to its folder)."""
    html = _cache.get(blob_sha)
    if html is None:
        html = render(text)
        if len(_cache) >= 256:
            _cache.pop(next(iter(_cache)))
        _cache[blob_sha] = html
    return html
