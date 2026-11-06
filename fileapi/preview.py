"""Link previews for forge URLs: Open Graph / Twitter card tags and a generated card image.

The web UI routes by path (/forge/<owner>/<repo>/-/commits/<sha>, ...), so the server sees the whole route
and can put a title, description and image in the page it serves, which is what Slack, Discord, iMessage,
Bluesky and the like read when a link is pasted. Previews are always built as an anonymous visitor would
see the repo: a private repo, or one that doesn't exist, gets the same generic card, so a link never leaks
its name, commits or files.
"""

from __future__ import annotations

import hashlib
import html
import io
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from .storage import NotFound, StorageError

SITE = "fileapi forge"
GENERIC_DESC = "Git repositories, commits and releases on fileapi."
STATIC = Path(__file__).parent / "static"
TAB_NAMES = {"commits", "releases", "settings", "deploys"}


@dataclass
class Preview:
    title: str = SITE
    description: str = GENERIC_DESC
    # On the card image: a small line above the title (e.g. "owner/repo · commit"), the title, and a footer.
    kicker: str = ""
    footer: str = ""
    repo: str = ""  # set when the preview names a public repo (the image cache key includes its head)
    stamp: str = ""  # changes when what the card shows changes
    card_text: str | None = None  # under the title on the card; None = the description
    extra: dict = field(default_factory=dict)


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:10]


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""


def _clip(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _plain_markdown(text: str) -> str:
    """Release notes as one plain line: drop markup that reads badly in a preview."""
    text = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)  # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links -> their text
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"^\s{0,3}(#{1,6}|[-*+]|\d+\.|>)\s+", "", text, flags=re.M)
    text = re.sub(r"[*_`~]+", "", text)
    return text


def _author_name(author: str) -> str:
    return re.sub(r"\s*<[^>]*>\s*$", "", author or "").strip()


def _size(n: int | None) -> str:
    if n is None:
        return ""
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def split_route(route: str) -> tuple[str, str]:
    """'<path>?ref=<ref>' -> (path, ref). The #L12 part never reaches the server."""
    path, _, query = route.partition("?")
    ref = ""
    for part in query.split("&"):
        if part.startswith("ref="):
            ref = part[4:]
    return path, ref


def describe(forge, releases, route: str, ref: str = "") -> Preview:
    """What a link to /forge/<route> should unfurl as, for an anonymous visitor."""
    parts = [p for p in route.split("/") if p]
    if len(parts) < 2:
        return Preview()
    try:
        full = forge.canonical(f"{parts[0]}/{parts[1]}")
        if not forge.is_public(full):
            return Preview()
        info = forge.info(full)
        heads = forge.branches(full)
    except (NotFound, StorageError, ValueError):
        return Preview()
    repo = info.full_name
    default = heads.get("default") or "main"
    use_ref = ref or default
    head = heads.get("branches", {}).get(use_ref) or heads.get("tags", {}).get(use_ref) or ""
    repo_desc = _clip(info.description, 200)
    base = Preview(title=repo, description=repo_desc or f"Git repository {repo} on fileapi forge.",
                   kicker="repository", footer=f"{len(heads.get('branches', {}))} branches · "
                   f"{len(heads.get('tags', {}))} tags" if heads.get("branches") else "empty repository",
                   repo=repo, stamp=f"{head}:{info.description}")
    rest = parts[2:]
    try:
        if rest[:1] == ["-"] and len(rest) >= 2 and rest[1].lower() in TAB_NAMES:
            tab, item = rest[1].lower(), "/".join(rest[2:])
            if tab == "commits":
                return _commit(forge, repo, item, use_ref, base)
            if tab == "releases":
                return _release(releases, repo, item, base)
            base.kicker = f"{repo} · {tab}"
            base.title = f"{tab.capitalize()} · {repo}"
            return base
        if rest:
            return _path(forge, repo, "/".join(rest), use_ref, ref, base)
    except (NotFound, StorageError, ValueError, KeyError):
        pass  # a link to something that isn't there (any more): the repo's own card
    return base


def _commit(forge, repo, sha, ref, base: Preview) -> Preview:
    if not sha:
        log = forge.log(repo, ref, limit=1) if base.stamp.split(":")[0] else []
        latest = _first_line(log[0]["message"]) if log else ""
        return Preview(title=f"Commits · {repo}", description=f"Latest on {ref}: {_clip(latest, 180)}" if latest
                       else base.description, kicker=f"{repo} · commits", footer=f"on {ref}", repo=repo,
                       stamp=base.stamp)
    c = forge.commit_diff(repo, sha)
    subject = _first_line(c["message"]) or c["sha"][:12]
    body = c["message"].strip()[len(subject):].strip()
    s = c["stats"]
    stats = f"{s['files']} file{'s' if s['files'] != 1 else ''} changed, +{s['additions']} −{s['deletions']}"
    who = _author_name(c["author"])
    desc = f"{who} committed {c['date'][:10]} · {stats}" + (f". {body}" if body else "")
    return Preview(title=_clip(subject, 120), description=_clip(desc, 280), kicker=f"{repo} · commit {c['sha'][:7]}",
                   footer=f"{who} · {c['date'][:10]} · {stats}", repo=repo, stamp=c["sha"], card_text=body)


def _release(releases, repo, tag, base: Preview) -> Preview:
    if not tag:
        rels = releases.list(repo, drafts=False)
        latest = next((r for r in rels if not r.get("prerelease")), rels[0] if rels else None)
        desc = f"Latest: {latest.get('name') or latest['tag']}" if latest else "No releases yet."
        return Preview(title=f"Releases · {repo}", description=desc, kicker=f"{repo} · releases",
                       footer=f"{len(rels)} release{'s' if len(rels) != 1 else ''}", repo=repo,
                       stamp=f"{len(rels)}:{latest['tag'] if latest else ''}")
    rel = releases.get(repo, tag, drafts=False)
    name = rel.get("name") or rel["tag"]
    notes = _clip(_plain_markdown(rel.get("notes") or ""), 260)
    assets = rel.get("assets") or []
    footer = " · ".join(x for x in [rel["tag"], "pre-release" if rel.get("prerelease") else "",
                                    f"{len(assets)} asset{'s' if len(assets) != 1 else ''}" if assets else ""] if x)
    return Preview(title=f"{repo} {name}" if name == rel["tag"] else name, description=notes or f"Release {rel['tag']} of {repo}.",
                   kicker=f"{repo} · release", footer=footer, repo=repo,
                   stamp=f"{rel['tag']}:{rel.get('name')}:{len(assets)}:{_digest(rel.get('notes') or '')}")


def _path(forge, repo, path, ref, explicit_ref, base: Preview) -> Preview:
    b = forge.browse(repo, path, ref)
    at = f" at {explicit_ref}" if explicit_ref else ""
    name = b["path"].rsplit("/", 1)[-1]
    if b["type"] == "dir":
        n = len(b["entries"])
        return Preview(title=f"{b['path']}/ · {repo}", description=f"Folder in {repo}{at}: {n} entr{'ies' if n != 1 else 'y'}"
                       + (f" ({', '.join(e['name'] for e in b['entries'][:6])}{', …' if n > 6 else ''})" if n else ""),
                       kicker=f"{repo} · folder", footer=f"{n} entr{'ies' if n != 1 else 'y'}{at}", repo=repo,
                       stamp=b["commit"])
    snippet = ""
    if b.get("content"):
        lines = [ln.strip() for ln in b["content"].splitlines() if ln.strip()]
        snippet = _clip(" ".join(lines[:4]), 200)
    kind = "Git LFS file" if b.get("lfs") else "binary file" if b.get("binary") else "file"
    desc = f"{_size(b.get('size'))} {kind} in {repo}{at}" + (f": {snippet}" if snippet else ".")
    return Preview(title=f"{name} · {repo}", description=_clip(desc, 280), kicker=f"{repo} · {b['path']}",
                   footer=f"{_size(b.get('size'))}{at}", repo=repo, stamp=b["commit"],
                   extra={"filename": name})


# --------------------------------------------------------------------------- HTML

def meta_tags(p: Preview, page_url: str, image_url: str) -> str:
    e = lambda s: html.escape(s, quote=True)  # noqa: E731
    tags = [
        f'<meta name="description" content="{e(p.description)}">',
        f'<meta property="og:site_name" content="{SITE}">',
        '<meta property="og:type" content="website">',
        f'<meta property="og:title" content="{e(p.title)}">',
        f'<meta property="og:description" content="{e(p.description)}">',
        f'<meta property="og:url" content="{e(page_url)}">',
        f'<meta property="og:image" content="{e(image_url)}">',
        '<meta property="og:image:type" content="image/png">',
        f'<meta property="og:image:width" content="{CARD_W}">',
        f'<meta property="og:image:height" content="{CARD_H}">',
        f'<meta property="og:image:alt" content="{e(p.title)}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{e(p.title)}">',
        f'<meta name="twitter:description" content="{e(p.description)}">',
        f'<meta name="twitter:image" content="{e(image_url)}">',
        f'<link rel="canonical" href="{e(page_url)}">',
    ]
    return "\n".join(tags)


def inject(index_html: str, p: Preview, page_url: str, image_url: str) -> str:
    title = SITE if p.title == SITE else f"{p.title} · {SITE}"
    head = f"<title>{html.escape(title)}</title>\n{meta_tags(p, page_url, image_url)}"
    return re.sub(r"<title>.*?</title>", lambda _: head, index_html, count=1, flags=re.S)


def image_query(p: Preview, route: str, ref: str) -> str:
    """The card's URL query: the route, plus a version so chat apps refetch when the card changes."""
    q = f"r={quote(route, safe='/-._~')}"
    if ref:
        q += f"&ref={quote(ref, safe='')}"
    if p.stamp:
        q += f"&v={_digest(p.stamp)}"
    return q


# --------------------------------------------------------------------------- card image

CARD_W, CARD_H = 1200, 630
_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()
_fonts: dict = {}


def _font(bold: bool, size: int):
    from PIL import ImageFont

    key = (bold, size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(str(STATIC / "fonts" / ("Inter-Bold.otf" if bold else "Inter-Regular.otf")), size)
    return _fonts[key]


def _wrap(draw, text: str, font, width: int, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        cur = w
        while draw.textlength(cur, font=font) > width:  # one very long word (a path, a sha): break it
            cut = len(cur)
            while cut > 1 and draw.textlength(cur[:cut], font=font) > width:
                cut -= 1
            lines.append(cur[:cut])
            cur = cur[cut:]
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def render_card(p: Preview) -> bytes:
    title, text = p.title, p.description if p.card_text is None else p.card_text
    if title == SITE:  # the generic card: the name is already in the header
        title, text = "Git repositories, commits and releases", ""
    key = (title, p.kicker, p.footer, text)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (CARD_W, CARD_H))
    # The favicon's navy gradient, top to bottom.
    top, bottom = (0x26, 0x32, 0x4D), (0x14, 0x1A, 0x29)
    grad = Image.linear_gradient("L").resize((CARD_W, CARD_H))
    img = Image.composite(Image.new("RGB", img.size, bottom), Image.new("RGB", img.size, top), grad)
    draw = ImageDraw.Draw(img)
    pad = 72
    # Accent bar in the favicon's spark orange.
    draw.rectangle([0, 0, CARD_W, 10], fill=(0xF5, 0x9E, 0x0B))

    logo = Image.open(STATIC / "icons" / "icon-192.png").convert("RGBA").resize((88, 88), Image.LANCZOS)
    img.paste(logo, (pad, 56), logo)
    draw.text((pad + 108, 70), SITE, font=_font(True, 40), fill=(0xFF, 0xFF, 0xFF))
    if p.kicker and p.kicker != "repository":
        kicker = _wrap(draw, p.kicker, _font(False, 30), CARD_W - 2 * pad - 108, 1)[0]
        draw.text((pad + 108, 116), kicker, font=_font(False, 28), fill=(0xA9, 0xB4, 0xCC))

    title_font = _font(True, 72 if len(title) < 40 else 60)
    lines = _wrap(draw, title, title_font, CARD_W - 2 * pad, 3)
    y = 210
    for line in lines:
        draw.text((pad, y), line, font=title_font, fill=(0xFF, 0xFF, 0xFF))
        y += int(title_font.size * 1.18)
    if text and len(lines) < 3:
        desc_font = _font(False, 32)
        y += 10
        for line in _wrap(draw, text, desc_font, CARD_W - 2 * pad, 3 - len(lines) + 1):
            y += 4
            draw.text((pad, y), line, font=desc_font, fill=(0xC9, 0xD1, 0xE3))
            y += 42
    if p.footer:
        foot = _wrap(draw, p.footer, _font(False, 30), CARD_W - 2 * pad, 1)[0]
        draw.text((pad, CARD_H - 56 - 30), foot, font=_font(False, 30), fill=(0xF5, 0xB0, 0x4A))

    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    data = out.getvalue()
    with _cache_lock:
        _cache[key] = data
        while len(_cache) > 200:
            _cache.popitem(last=False)
    return data
