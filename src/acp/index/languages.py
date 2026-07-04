"""Tree-sitter grammar loading and per-language AST extraction.

We own this layer directly rather than through an LSP/framework: the grammars
are the only third-party surface, and the model never touches them — extraction
turns an AST into our own :mod:`acp.index.model` types. Supported today: Python
and TypeScript (the polyglot requirement lives at the index level).

Extraction is deterministic: tree-sitter's walk order is fixed, and we sort in
the model, so the same bytes always yield the same partition.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser

from acp.index.model import Reference, RefKind, Symbol

if TYPE_CHECKING:
    from collections.abc import Iterator

# repo-relative extension → (index language label, grammar variant)
_EXT_TO_GRAMMAR = {
    ".py": ("python", "python"),
    ".pyi": ("python", "python"),
    ".ts": ("typescript", "typescript"),
    ".tsx": ("typescript", "tsx"),
}


def detect_language(path: str) -> tuple[str, str] | None:
    """Return ``(language_label, grammar_variant)`` for a path, or None if the
    file is not an indexed language."""
    for ext, pair in _EXT_TO_GRAMMAR.items():
        if path.endswith(ext):
            return pair
    return None


@cache
def _language(variant: str) -> Language:
    if variant == "python":
        return Language(tspython.language())
    if variant == "typescript":
        return Language(tstypescript.language_typescript())
    if variant == "tsx":
        return Language(tstypescript.language_tsx())
    raise ValueError(f"unknown grammar variant: {variant}")


@cache
def _parser(variant: str) -> Parser:
    return Parser(_language(variant))


def parse(content: bytes, variant: str) -> Node:
    """Parse bytes with the given grammar variant and return the root node."""
    return _parser(variant).parse(content).root_node


def walk(node: Node) -> Iterator[Node]:
    """Pre-order traversal of the whole tree."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        # push children reversed so we pop them left-to-right (stable order)
        stack.extend(reversed(n.children))


def _text(node: Node) -> str:
    return (node.text or b"").decode("utf-8", errors="replace")


def _line(node: Node) -> int:
    return node.start_point[0] + 1  # tree-sitter rows are 0-based


def _col(node: Node) -> int:
    return node.start_point[1]


# --- Python -----------------------------------------------------------------

_PY_DEF_TYPES = {"function_definition", "class_definition"}


def _py_is_method(defn: Node) -> bool:
    """A function is a method if its nearest enclosing definition is a class."""
    p = defn.parent
    while p is not None:
        if p.type == "class_definition":
            return True
        if p.type == "function_definition":
            return False
        p = p.parent
    return False


def _py_import_modules(node: Node) -> list[str]:
    """Module strings from an import / from-import statement (raw, incl. dots)."""
    if node.type == "import_statement":
        mods: list[str] = []
        for child in node.children:
            if child.type == "dotted_name":
                mods.append(_text(child))
            elif child.type == "aliased_import":
                name = child.child_by_field_name("name")
                if name is not None:
                    mods.append(_text(name))
        return mods
    if node.type == "import_from_statement":
        mod = node.child_by_field_name("module_name")
        return [_text(mod)] if mod is not None else []
    return []


def _extract_python(
    path: str, root: Node
) -> tuple[list[Symbol], list[Reference], list[tuple[str, int]]]:
    symbols: list[Symbol] = []
    references: list[Reference] = []
    raw_imports: list[tuple[str, int]] = []
    def_name_spans: set[tuple[int, int]] = set()
    in_import: set[int] = set()  # start_byte of identifiers inside an import stmt

    for n in walk(root):
        if n.type in _PY_DEF_TYPES:
            name_node = n.child_by_field_name("name")
            if name_node is None:
                continue
            if n.type == "class_definition":
                kind = "class"
            else:
                kind = "method" if _py_is_method(n) else "function"
            symbols.append(
                Symbol(
                    name=_text(name_node),
                    kind=kind,  # type: ignore[arg-type]
                    language="python",
                    file_path=path,
                    start_line=_line(n),
                    end_line=n.end_point[0] + 1,
                    start_col=_col(n),
                    end_col=n.end_point[1],
                )
            )
            def_name_spans.add((name_node.start_byte, name_node.end_byte))
        elif n.type in ("import_statement", "import_from_statement"):
            for mod in _py_import_modules(n):
                raw_imports.append((mod, _line(n)))
            for ident in walk(n):
                if ident.type in ("identifier", "dotted_name"):
                    in_import.add(ident.start_byte)
        elif n.type == "assignment" and n.parent is not None and (
            n.parent.type == "expression_statement"
            and n.parent.parent is not None
            and n.parent.parent.type == "module"
        ):
            left = n.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                symbols.append(
                    Symbol(
                        name=_text(left),
                        kind="variable",
                        language="python",
                        file_path=path,
                        start_line=_line(left),
                        end_line=left.end_point[0] + 1,
                        start_col=_col(left),
                        end_col=left.end_point[1],
                    )
                )
                def_name_spans.add((left.start_byte, left.end_byte))

    for n in walk(root):
        if n.type != "identifier":
            continue
        span = (n.start_byte, n.end_byte)
        if span in def_name_spans:
            continue  # the defining occurrence is a Symbol, not a Reference
        references.append(
            Reference(
                name=_text(n),
                language="python",
                file_path=path,
                line=_line(n),
                col=_col(n),
                ref_kind=_py_ref_kind(n, span[0] in in_import),
            )
        )
    return symbols, references, raw_imports


def _py_ref_kind(node: Node, is_import: bool) -> RefKind:
    if is_import:
        return "import"
    p = node.parent
    if p is not None and p.type == "call" and p.child_by_field_name("function") is node:
        return "call"
    return "use"


# --- TypeScript -------------------------------------------------------------

_TS_TYPE_DEFS = {
    "interface_declaration": "type",
    "type_alias_declaration": "type",
    "enum_declaration": "type",
}


def _ts_add_symbol(
    symbols: list[Symbol],
    spans: set[tuple[int, int]],
    name_node: Node,
    kind: str,
    path: str,
    defn: Node,
) -> None:
    symbols.append(
        Symbol(
            name=_text(name_node),
            kind=kind,  # type: ignore[arg-type]
            language="typescript",
            file_path=path,
            start_line=_line(defn),
            end_line=defn.end_point[0] + 1,
            start_col=_col(defn),
            end_col=defn.end_point[1],
        )
    )
    spans.add((name_node.start_byte, name_node.end_byte))


def _extract_typescript(
    path: str, root: Node
) -> tuple[list[Symbol], list[Reference], list[tuple[str, int]]]:
    symbols: list[Symbol] = []
    references: list[Reference] = []
    raw_imports: list[tuple[str, int]] = []
    def_name_spans: set[tuple[int, int]] = set()
    in_import: set[int] = set()

    for n in walk(root):
        name_node = n.child_by_field_name("name")
        if n.type == "function_declaration" and name_node is not None:
            _ts_add_symbol(symbols, def_name_spans, name_node, "function", path, n)
        elif n.type in ("class_declaration", "abstract_class_declaration") and name_node:
            _ts_add_symbol(symbols, def_name_spans, name_node, "class", path, n)
        elif n.type == "method_definition" and name_node is not None:
            _ts_add_symbol(symbols, def_name_spans, name_node, "method", path, n)
        elif n.type in _TS_TYPE_DEFS and name_node is not None:
            _ts_add_symbol(symbols, def_name_spans, name_node, _TS_TYPE_DEFS[n.type], path, n)
        elif n.type == "variable_declarator" and name_node is not None:
            value = n.child_by_field_name("value")
            kind = (
                "function"
                if value is not None and value.type in ("arrow_function", "function_expression")
                else "variable"
            )
            _ts_add_symbol(symbols, def_name_spans, name_node, kind, path, n)
        elif n.type in ("import_statement", "export_statement"):
            src = n.child_by_field_name("source")
            if src is not None and src.type == "string":
                raw_imports.append((_text(src).strip("'\"`"), _line(n)))
            if n.type == "import_statement":
                for ident in walk(n):
                    if ident.type in ("identifier", "type_identifier"):
                        in_import.add(ident.start_byte)

    for n in walk(root):
        if n.type not in ("identifier", "type_identifier"):
            continue
        span = (n.start_byte, n.end_byte)
        if span in def_name_spans:
            continue
        references.append(
            Reference(
                name=_text(n),
                language="typescript",
                file_path=path,
                line=_line(n),
                col=_col(n),
                ref_kind=_ts_ref_kind(n, span[0] in in_import),
            )
        )
    return symbols, references, raw_imports


def _ts_ref_kind(node: Node, is_import: bool) -> RefKind:
    if is_import:
        return "import"
    p = node.parent
    if (
        p is not None
        and p.type == "call_expression"
        and p.child_by_field_name("function") is node
    ):
        return "call"
    return "use"


def extract(
    path: str, content: bytes, variant: str
) -> tuple[list[Symbol], list[Reference], list[tuple[str, int]]]:
    """Parse ``content`` and extract (symbols, references, raw_imports) for the
    file at repo-relative ``path``."""
    root = parse(content, variant)
    if variant == "python":
        return _extract_python(path, root)
    return _extract_typescript(path, root)
