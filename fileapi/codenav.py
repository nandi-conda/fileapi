"""Code navigation for the forge web UI: go to definition, find references, hover and a file outline.

Search-based, like GitHub's code navigation: every supported file in a commit is parsed with tree-sitter,
definitions come from each grammar's tags query, and references are the identifiers with the same name in
files of the same language family. Parameters and variables bound inside a function are resolved within
that function instead (a small scope walk over the syntax tree). No language servers run, so it needs no build of the repo and works
for any commit, at the cost of not knowing types (two methods called `run` both show up).

Parsed files are cached by blob sha (content-addressed, so they're shared between commits) and whole-commit
indexes by commit sha.
"""
from __future__ import annotations

import importlib
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from dulwich.objects import Blob, Tree

try:  # optional: without tree-sitter the endpoints answer "not supported" instead of failing to import
    from tree_sitter import Language, Parser, Query, QueryCursor
except ImportError:  # pragma: no cover
    Language = None

MAX_FILE = 1 << 20          # bytes: bigger files aren't indexed (the viewer doesn't highlight them either)
MAX_REPO_BYTES = 48 << 20   # stop indexing a commit after this much source
MAX_REPO_FILES = 8000
MAX_REFS = 500

# name, module, language attribute, extra tags patterns, family (references cross files of one family)
_SPECS: dict[str, tuple[str, str, str, str, str]] = {
    "python": ("Python", "tree_sitter_python", "language", "", "python"),
    "javascript": ("JavaScript", "tree_sitter_javascript", "language", "", "js"),
    "typescript": ("TypeScript", "tree_sitter_typescript", "language_typescript", "ts", "js"),
    "tsx": ("TSX", "tree_sitter_typescript", "language_tsx", "ts", "js"),
    "go": ("Go", "tree_sitter_go", "language", "", "go"),
    "rust": ("Rust", "tree_sitter_rust", "language", "", "rust"),
    "c": ("C", "tree_sitter_c", "language", "", "c"),
    "cpp": ("C++", "tree_sitter_cpp", "language", "cpp", "c"),
    "java": ("Java", "tree_sitter_java", "language", "", "java"),
}

_EXTRA = {
    # TypeScript's own tags.scm only adds TS-specific patterns on top of JavaScript's.
    "ts": """
(type_alias_declaration name: (type_identifier) @name) @definition.type
(enum_declaration name: (identifier) @name) @definition.enum
""",
    # Plain declarations the C++ tags miss: members declared in a class body, and out-of-line methods
    # whose scope is a class (Foo::bar) when it is parsed as a namespace.
    "cpp": """
(field_declaration declarator: (function_declarator declarator: (field_identifier) @name)) @definition.method
(namespace_definition name: (namespace_identifier) @name) @definition.module
""",
}

_EXT = {
    ".py": "python", ".pyi": "python", ".pyw": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".go": "go", ".rs": "rust",
    ".c": "c",
    ".h": "cpp", ".hh": "cpp", ".hpp": "cpp", ".hxx": "cpp", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".c++": "cpp", ".ipp": "cpp", ".inl": "cpp",
    ".java": "java",
}

# Leaf node types that name something; strings and comments never match, so neither do words in them.
_IDENT_TYPES = ("identifier", "type_identifier", "field_identifier", "property_identifier",
                "shorthand_property_identifier", "shorthand_property_identifier_pattern",
                "private_property_identifier", "namespace_identifier", "package_identifier",
                "statement_identifier", "constant")


def language_of(path: str) -> str | None:
    return _EXT.get(Path(path).suffix.lower())


def available() -> bool:
    return Language is not None


@dataclass
class _Lang:
    key: str
    name: str
    family: str
    language: object
    tags: object
    idents: object


_langs: dict[str, _Lang | None] = {}
_langs_lock = threading.Lock()


def _lang(key: str) -> _Lang | None:
    with _langs_lock:
        if key in _langs:
            return _langs[key]
        name, module, attr, extra, family = _SPECS[key]
        try:
            mod = importlib.import_module(module)
            language = Language(getattr(mod, attr)())
            tags_src = getattr(mod, "TAGS_QUERY", None)
            if tags_src is None:  # tree_sitter_typescript ships its queries as files, not constants
                tags_src = (Path(mod.__file__).parent / "queries" / "tags.scm").read_text()
            if extra == "ts":
                tags_src = importlib.import_module("tree_sitter_javascript").TAGS_QUERY + "\n" + tags_src
            tags_src += _EXTRA.get(extra, "")
            kinds = [t for t in _IDENT_TYPES if language.id_for_node_kind(t, True)]
            idents = Query(language, "[" + " ".join(f"({t})" for t in kinds) + "] @id")
            _langs[key] = _Lang(key, name, family, language, Query(language, tags_src), idents)
        except Exception:  # grammar missing or its query doesn't compile: treat the language as unsupported
            _langs[key] = None
        return _langs[key]


@dataclass
class Definition:
    name: str
    kind: str
    line: int       # 0-based
    col: int        # 0-based, in characters
    end_col: int
    node_line: int  # where the whole definition starts (decorators/signature)
    signature: str
    doc: str
    container: str = ""  # enclosing class/struct/impl, when there is one


@dataclass
class FileIndex:
    lang: str
    lines: list[str]
    defs: list[Definition]
    # name -> [(line, col, end_col)], every identifier occurrence (definitions included)
    idents: dict[str, list[tuple[int, int, int]]] = field(default_factory=dict)

    def at(self, line: int, col: int) -> tuple[str, int, int] | None:
        """The identifier covering (line, col), as (name, col, end_col); a click just past a name counts."""
        after = None
        for name, spots in self.idents.items():
            for ln, c0, c1 in spots:
                if ln != line:
                    continue
                if c0 <= col < c1:
                    return name, c0, c1
                if col == c1:
                    after = (name, c0, c1)
        return after


def _char_col(line_bytes: bytes, byte_col: int) -> int:
    return len(line_bytes[:byte_col].decode("utf-8", errors="replace"))


_CONTAINERS = {"class_definition", "class_declaration", "class_specifier", "struct_specifier", "struct_item",
               "impl_item", "trait_item", "interface_declaration", "enum_declaration", "class",
               "abstract_class_declaration", "type_declaration", "namespace_definition", "mod_item"}
_COMMENTS = {"comment", "line_comment", "block_comment"}


def _container(node, src: bytes) -> str:
    p = node.parent
    while p is not None:
        if p.type in _CONTAINERS:
            n = p.child_by_field_name("name") or p.child_by_field_name("type")
            if n is not None:
                return src[n.start_byte:n.end_byte].decode(errors="replace")
        p = p.parent
    return ""


def _doc(node, lang: str, src: bytes) -> str:
    """A docstring (Python) or the comment block right above the definition."""
    if lang == "python":
        body = node.child_by_field_name("body")
        if body is not None and body.named_child_count:
            first = body.named_children[0]
            if first.type == "expression_statement" and first.named_child_count and first.named_children[0].type == "string":
                s = first.named_children[0]
                parts = [c for c in s.named_children if c.type == "string_content"]
                text = b"".join(src[c.start_byte:c.end_byte] for c in parts) if parts else src[s.start_byte:s.end_byte]
                return _clean_doc(text.decode(errors="replace"))
    # Comments directly above (decorators/attributes in between are fine), with no blank line between.
    target = node
    while target.parent is not None and target.parent.type in ("decorated_definition", "export_statement",
                                                                "template_declaration"):
        target = target.parent
    out = []
    prev = target.prev_sibling
    line = target.start_point[0]
    while prev is not None and (prev.type in _COMMENTS or prev.type in ("attribute_item", "decorator")):
        if prev.end_point[0] < line - 1:
            break
        if prev.type in _COMMENTS:
            out.append(src[prev.start_byte:prev.end_byte].decode(errors="replace"))
        line = prev.start_point[0]
        prev = prev.prev_sibling
    return _clean_comment("\n".join(reversed(out)))


def _clean_doc(text: str) -> str:
    lines = text.expandtabs().strip("\n").splitlines()
    if not lines:
        return ""
    rest = [ln for ln in lines[1:] if ln.strip()]
    indent = min((len(ln) - len(ln.lstrip()) for ln in rest), default=0)
    out = [lines[0].strip()] + [ln[indent:].rstrip() for ln in lines[1:]]
    return "\n".join(out).strip()[:2000]


def _clean_comment(text: str) -> str:
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        for prefix in ("///", "//!", "//", "/**", "/*", "*/", "*", "#"):
            if s.startswith(prefix):
                s = s[len(prefix):]
                break
        if s.endswith("*/"):
            s = s[:-2]
        out.append(s.rstrip()[1:] if s.startswith(" ") else s.rstrip())
    return "\n".join(out).strip()[:2000]


def _inside_body(node) -> bool:
    p = node.parent
    while p is not None:
        if p.type == "compound_statement":
            return True
        p = p.parent
    return False


def _outer(node):
    """C/C++ tags point at the declarator (`bar(int x)`); widen to the whole declaration, return type included."""
    if node.type != "function_declarator":
        return node
    p = node.parent
    while p is not None and p.type in ("pointer_declarator", "reference_declarator", "function_declarator"):
        p = p.parent
    if p is not None and p.type in ("function_definition", "declaration", "field_declaration"):
        return p
    return node


def _signature(node, src: bytes, lang: str) -> str:
    """The definition's head: from its start up to the body, on one line."""
    body = node.child_by_field_name("body")
    if node.type == "variable_declarator" and node.child_by_field_name("value") is not None:
        value = node.child_by_field_name("value")
        body = value.child_by_field_name("body") or value
        if body is value:
            body = None
    if node.type in ("assignment", "expression_statement"):  # Python module constant: its first line
        end = src.find(b"\n", node.start_byte, node.end_byte)
        end = node.end_byte if end < 0 else end
    elif body is not None and body.start_byte > node.start_byte:
        end = body.start_byte
    else:
        end = node.end_byte
    text = src[node.start_byte:end].decode(errors="replace")
    text = " ".join(text.split())
    if lang == "python":
        text = text.rstrip(":").rstrip()
    else:
        text = text.rstrip("{").rstrip()
    return text if len(text) <= 240 else text[:237] + "..."


def index_file(path: str, data: bytes) -> FileIndex | None:
    key = language_of(path)
    if key is None or len(data) > MAX_FILE:
        return None
    lang = _lang(key)
    if lang is None:
        return None
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    tree = Parser(lang.language).parse(data)
    raw_lines = data.split(b"\n")
    lines = [ln.decode(errors="replace").rstrip("\r") for ln in raw_lines]

    def cols(node) -> tuple[int, int, int]:
        (r0, b0), (r1, b1) = node.start_point, node.end_point
        rb = raw_lines[r0] if r0 < len(raw_lines) else b""
        c0 = _char_col(rb, b0)
        c1 = _char_col(rb, b1) if r1 == r0 else len(lines[r0])
        return r0, c0, c1

    idents: dict[str, list[tuple[int, int, int]]] = {}
    for node in QueryCursor(lang.idents).captures(tree.root_node).get("id", []):
        if node.start_point[0] != node.end_point[0]:
            continue
        name = data[node.start_byte:node.end_byte].decode(errors="replace")
        idents.setdefault(name, []).append(cols(node))
    for spots in idents.values():
        spots.sort()

    defs: list[Definition] = []
    seen: set[tuple[int, int]] = set()
    for _, caps in QueryCursor(lang.tags).matches(tree.root_node):
        names = caps.get("name") or []
        kind_cap = next((k for k in caps if k.startswith("definition.")), None)
        if not names or kind_cap is None:
            continue
        name_node, def_node = names[0], _outer(caps[kind_cap][0])
        if (name_node.start_byte, name_node.end_byte) in seen:
            continue
        if def_node.type == "declaration" and _inside_body(def_node):
            continue  # C++ `Foo x(y);` in a function body: a local variable, not a function prototype
        seen.add((name_node.start_byte, name_node.end_byte))
        line, c0, c1 = cols(name_node)
        kind = kind_cap.split(".", 1)[1]
        scope = name_node.parent
        if scope is not None and scope.type == "qualified_identifier" and scope.child_by_field_name("scope"):
            sc = scope.child_by_field_name("scope")
            container = data[sc.start_byte:sc.end_byte].decode(errors="replace")  # Foo::bar defined out of line
        else:
            container = _container(def_node, data)
        if kind == "function" and container:
            kind = "method"
        defs.append(Definition(
            name=data[name_node.start_byte:name_node.end_byte].decode(errors="replace"),
            kind=kind, line=line, col=c0, end_col=c1, node_line=def_node.start_point[0],
            signature=_signature(def_node, data, key), doc=_doc(def_node, key, data), container=container,
        ))
    defs.sort(key=lambda d: (d.line, d.col))
    return FileIndex(lang=key, lines=lines, defs=defs, idents=idents)


# ---- locals: parameters and variables bound inside a function --------------------------------------

_FUNCTIONS = {"function_definition", "lambda", "function_declaration", "function_expression", "arrow_function",
              "method_definition", "generator_function", "generator_function_declaration", "function_item",
              "closure_expression", "method_declaration", "func_literal", "constructor_declaration",
              "lambda_expression"}
# Wrappers between a bound name and the node that binds it: `x: int = 1`, `*args`, `(a, b) = ...`, `int *p`.
_PATTERNS = {"typed_parameter", "default_parameter", "typed_default_parameter", "list_splat_pattern",
             "dictionary_splat_pattern", "pattern_list", "tuple_pattern", "list_pattern", "expression_list",
             "required_parameter", "optional_parameter", "pointer_declarator", "reference_declarator",
             "array_declarator", "mut_pattern", "reference_pattern", "tuple_struct_pattern", "object_pattern",
             "array_pattern", "shorthand_property_identifier_pattern", "assignment_pattern", "rest_pattern",
             "as_pattern_target", "variable_list"}
# node type -> fields that bind names (None: any direct child does, like a parameter list)
_BINDERS = {
    "parameters": None, "lambda_parameters": None, "formal_parameters": None, "closure_parameters": None,
    "parameter_list": None,
    "assignment": ("left",), "augmented_assignment": ("left",), "for_statement": ("left",),
    "for_in_statement": ("left",), "named_expression": ("name",), "as_pattern": ("alias",),
    "for_in_clause": ("left",), "with_item": ("value",), "except_clause": None,
    "variable_declarator": ("name",), "short_var_declaration": ("left",), "var_spec": ("name",),
    "range_clause": ("left",), "let_declaration": ("pattern",), "parameter": ("pattern",),
    "parameter_declaration": ("name", "declarator"), "init_declarator": ("declarator",),
    "declaration": ("declarator",), "formal_parameter": ("name",), "catch_formal_parameter": ("name",),
    "enhanced_for_statement": ("name",), "for_expression": ("pattern",), "catch_clause": ("parameter",),
}
_MEMBER_FIELDS = (("attribute", "attribute"), ("member_expression", "property"), ("selector_expression", "field"),
                  ("field_expression", "field"), ("field_expression", "name"), ("scoped_identifier", "name"),
                  ("qualified_identifier", "name"), ("keyword_argument", "name"))


def _same(a, b) -> bool:
    return a is not None and b is not None and a.start_byte == b.start_byte and a.end_byte == b.end_byte


def _is_member(node) -> bool:
    """`obj.name`, `Foo::name`, `f(name=...)`: the name belongs to something else, not to a local."""
    p = node.parent
    return p is not None and any(p.type == t and _same(p.child_by_field_name(f), node) for t, f in _MEMBER_FIELDS)


def _is_binding(node) -> bool:
    child, p = node, node.parent
    while p is not None and p.type in _PATTERNS:
        child, p = p, p.parent
    if p is None or p.type not in _BINDERS:
        return False
    fields = _BINDERS[p.type]
    if fields is None:
        return True
    return any(_same(c, child) for f in fields for c in p.children_by_field_name(f))  # Go: `a, b int`


def _walk_idents(node, name: bytes, src: bytes):
    stack = [node]
    while stack:
        n = stack.pop()
        if n.child_count == 0:
            if n.type in _IDENT_TYPES and src[n.start_byte:n.end_byte] == name:
                yield n
            continue
        stack.extend(reversed(n.children))


def _local(data: bytes, key: str, line: int, col: int):
    """If the name at line/col (0-based, characters) is bound inside an enclosing function, return
    (binding node, scope node, the name node); else None (it's global: search the whole repo)."""
    lang = _lang(key)
    if lang is None:
        return None
    tree = Parser(lang.language).parse(data)
    lines = data.split(b"\n")
    if line >= len(lines):
        return None
    text = lines[line].decode(errors="replace")
    byte_col = len(text[:col].encode())
    node = tree.root_node.named_descendant_for_point_range((line, byte_col), (line, byte_col))
    if node is None or node.type not in _IDENT_TYPES or _is_member(node):
        return None
    name = data[node.start_byte:node.end_byte]
    scope = node.parent
    while scope is not None:
        if scope.type in _FUNCTIONS:
            binds = [n for n in _walk_idents(scope, name, data) if _is_binding(n) and not _is_member(n)]
            if binds:
                return binds[0], scope, node
        scope = scope.parent
    return None


# ---- caches -------------------------------------------------------------------------------------------

class _LRU:
    def __init__(self, limit: int, weigh=lambda v: 1):
        self.limit, self.weigh, self.total = limit, weigh, 0
        self.data: OrderedDict = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            if key in self.data:
                self.data.move_to_end(key)
                return self.data[key]
        return None

    def put(self, key, value) -> None:
        with self.lock:
            if key in self.data:
                self.total -= self.weigh(self.data.pop(key))
            self.data[key] = value
            self.total += self.weigh(value)
            while self.total > self.limit and len(self.data) > 1:
                _, old = self.data.popitem(last=False)
                self.total -= self.weigh(old)


_MISSING = object()
_files = _LRU(160 << 20, weigh=lambda v: 64 if v is None else sum(len(ln) for ln in v.lines) * 3 + 256)


@dataclass
class RepoIndex:
    files: dict[str, FileIndex]
    defs: dict[str, list[tuple[str, Definition]]]
    truncated: bool


_repos = _LRU(12)
_building: dict[bytes, threading.Lock] = {}
_building_lock = threading.Lock()


def _walk(r, tree_id: bytes, prefix: str = ""):
    tree = r[tree_id]
    if not isinstance(tree, Tree):
        return
    for item in tree.iteritems():
        mode = item.mode & 0o170000
        name = prefix + item.path.decode(errors="replace")
        if mode == 0o040000:
            yield from _walk(r, item.sha, name + "/")
        elif mode == 0o100000:
            yield name, item.sha


def file_index(r, path: str, blob_sha: bytes) -> FileIndex | None:
    cached = _files.get(blob_sha)
    if cached is not None:
        return None if cached is _MISSING else cached
    blob = r[blob_sha]
    idx = index_file(path, blob.as_raw_string()) if isinstance(blob, Blob) else None
    _files.put(blob_sha, _MISSING if idx is None else idx)
    return idx


def repo_index(r, commit) -> RepoIndex:
    """The index of every supported file at `commit` (built once per commit, then cached)."""
    key = commit.id
    cached = _repos.get(key)
    if cached is not None:
        return cached
    with _building_lock:
        lock = _building.setdefault(key, threading.Lock())
    with lock:  # one build per commit, even with many requests arriving at once
        cached = _repos.get(key)
        if cached is not None:
            return cached
        files: dict[str, FileIndex] = {}
        total, truncated = 0, False
        for path, sha in _walk(r, commit.tree):
            if language_of(path) is None:
                continue
            if len(files) >= MAX_REPO_FILES or total > MAX_REPO_BYTES:
                truncated = True
                break
            idx = file_index(r, path, sha)
            if idx is not None:
                files[path] = idx
                total += sum(len(ln) + 1 for ln in idx.lines)
        defs: dict[str, list[tuple[str, Definition]]] = {}
        for path, idx in files.items():
            for d in idx.defs:
                defs.setdefault(d.name, []).append((path, d))
        built = RepoIndex(files=files, defs=defs, truncated=truncated)
        _repos.put(key, built)
    with _building_lock:
        _building.pop(key, None)
    return built


# ---- answers ------------------------------------------------------------------------------------------

def _def_json(path: str, d: Definition) -> dict:
    return {"path": path, "name": d.name, "kind": d.kind, "line": d.line + 1, "col": d.col, "end_col": d.end_col,
            "signature": d.signature, "doc": d.doc, "container": d.container}


def _family(key: str) -> str:
    return _SPECS[key][4]


def language_name(path: str) -> str | None:
    key = language_of(path)
    return _SPECS[key][0] if key and available() and _lang(key) is not None else None


def outline(r, commit, path: str, blob_sha: bytes) -> dict:
    idx = file_index(r, path, blob_sha)
    if idx is None:
        return {"path": path, "supported": False, "symbols": []}
    return {"path": path, "supported": True, "language": _SPECS[idx.lang][0],
            "symbols": [_def_json(path, d) for d in idx.defs]}


def lookup(r, commit, path: str, blob_sha: bytes, line: int, col: int, references: bool = True) -> dict:
    """What's at `line` (1-based), `col` (0-based characters): its definitions and, optionally, references."""
    idx = file_index(r, path, blob_sha)
    base = {"path": path, "line": line, "col": col}
    if idx is None:
        return {**base, "supported": False, "symbol": None, "definitions": [], "references": []}
    hit = idx.at(line - 1, col)
    if hit is None:
        return {**base, "supported": True, "symbol": None, "definitions": [], "references": []}
    name, c0, c1 = hit
    family = _family(idx.lang)

    local = _local(r[blob_sha].as_raw_string(), idx.lang, line - 1, c0)
    if local is not None and not any(d.line == line - 1 and d.col == c0 for d in idx.defs):
        bind, scope, _ = local
        data = r[blob_sha].as_raw_string()
        raw = data.split(b"\n")

        def span(n):
            ln = n.start_point[0]
            return ln, _char_col(raw[ln], n.start_point[1]), _char_col(raw[ln], n.end_point[1])
        bl, b0, b1 = span(bind)
        head = idx.lines[bl].strip() if bl < len(idx.lines) else ""
        kind = "parameter" if bind.parent is not None and (bind.parent.type in _BINDERS and _BINDERS[bind.parent.type] is None
                                                           or "parameter" in bind.parent.type) else "variable"
        d = {"path": path, "name": name, "kind": kind, "line": bl + 1, "col": b0, "end_col": b1,
             "signature": head[:240], "doc": "", "container": "", "local": True}
        out = {**base, "supported": True, "language": _SPECS[idx.lang][0],
               "symbol": {"name": name, "line": line, "col": c0, "end_col": c1}, "definitions": [d],
               "references": [], "truncated": False, "local": True}
        if references:
            refs = []
            for n in _walk_idents(scope, name.encode(), data):
                if _is_member(n):
                    continue
                ln, s0, s1 = span(n)
                refs.append({"path": path, "line": ln + 1, "col": s0, "end_col": s1,
                             "text": idx.lines[ln][:300], "definition": (ln, s0) == (bl, b0)})
            refs.sort(key=lambda x: (x["line"], x["col"]))
            out["references"] = refs[:MAX_REFS]
            out["reference_count"] = len(refs)
        return out

    ri = repo_index(r, commit)

    # Is the cursor on a definition itself? Then that's the definition (an editor shows references next).
    here = [d for d in idx.defs if d.line == line - 1 and d.col == c0 and d.name == name]
    candidates = [(p, d) for p, d in ri.defs.get(name, []) if p in ri.files and _family(ri.files[p].lang) == family]
    if path not in ri.files:  # the file wasn't part of the commit index (cut off by the limits)
        candidates += [(path, d) for d in idx.defs if d.name == name]
    if here:
        defs = [(path, here[0])]
    else:
        # Nearest first: this file (closest definition above the cursor first), then the same directory,
        # then everything else by path.
        folder = path.rsplit("/", 1)[0] if "/" in path else ""

        def rank(item):
            p, d = item
            if p == path:
                return (0, 0 if d.line <= line - 1 else 1, -d.line if d.line <= line - 1 else d.line, "")
            same_dir = (p.rsplit("/", 1)[0] if "/" in p else "") == folder
            return (1 if same_dir else 2, 0, 0, p)
        defs = sorted(candidates, key=rank)

    out = {**base, "supported": True, "language": _SPECS[idx.lang][0],
           "symbol": {"name": name, "line": line, "col": c0, "end_col": c1},
           "definitions": [_def_json(p, d) for p, d in defs[:50]], "references": [], "truncated": ri.truncated}
    if references:
        refs, total = [], 0
        files = dict(ri.files)
        files.setdefault(path, idx)
        def_spots = {(p, d.line, d.col) for p, d in ri.defs.get(name, [])}
        order = sorted(files, key=lambda p: (p != path, p))
        for p in order:
            fi = files[p]
            if _family(fi.lang) != family:
                continue
            for ln, s0, s1 in fi.idents.get(name, ()):
                total += 1
                if len(refs) < MAX_REFS:
                    refs.append({"path": p, "line": ln + 1, "col": s0, "end_col": s1,
                                 "text": fi.lines[ln][:300] if ln < len(fi.lines) else "",
                                 "definition": (p, ln, s0) in def_spots})
        out["references"] = refs
        out["reference_count"] = total
    return out
