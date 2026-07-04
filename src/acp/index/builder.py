"""Building the structural index from files on disk.

Two entry points, one invariant:

* :meth:`IndexBuilder.build` — full build: parse every indexed file.
* :meth:`IndexBuilder.reindex_file` — incremental: re-parse exactly one file and
  replace its partition, touching no other file.

**Equivalence invariant** (asserted in tests): for any single-file edit,
``reindex_file`` produces an index whose serialization is byte-for-byte identical
to a full ``build`` of the post-edit tree. It holds because a file's partition is
a pure function of that file's bytes plus the (edit-invariant) set of repo paths,
and serialization is a pure canonical function of the partitions.

``files_parsed`` counts grammar parses so a test can prove the incremental path
re-parses one file where a full build parses N.
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from acp.index.languages import detect_language, extract
from acp.index.model import FileIndex, ImportEdge, Index

# Directories never indexed (build output, deps, vcs, our own artifacts).
_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".acp",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


class IndexBuilder:
    """Stateless-per-call builder that also tallies parses for accounting."""

    def __init__(self) -> None:
        self.files_parsed = 0

    # --- discovery -----------------------------------------------------------
    def discover(self, root: Path) -> list[str]:
        """Repo-relative (posix) paths of all indexable files, sorted."""
        out: list[str] = []
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if any(part in _IGNORED_DIRS for part in p.relative_to(root).parts):
                continue
            rel = p.relative_to(root).as_posix()
            if detect_language(rel) is not None:
                out.append(rel)
        return sorted(out)

    # --- single-file partition ----------------------------------------------
    def index_file(self, root: Path, rel_path: str, known_paths: set[str]) -> FileIndex:
        """Parse one file and produce its partition, with imports resolved
        against ``known_paths``."""
        lang = detect_language(rel_path)
        if lang is None:  # pragma: no cover - guarded by discover()
            raise ValueError(f"not an indexed language: {rel_path}")
        language_label, variant = lang
        content = (root / rel_path).read_bytes()
        self.files_parsed += 1
        symbols, references, raw_imports = extract(rel_path, content, variant)
        imports = [
            ImportEdge(
                from_path=rel_path,
                module=module,
                to_path=_resolve_import(language_label, module, rel_path, known_paths),
                line=line,
            )
            for module, line in raw_imports
        ]
        return FileIndex(
            path=rel_path,
            language=language_label,
            content_hash=hashlib.sha256(content).hexdigest(),
            symbols=symbols,
            references=references,
            imports=imports,
        )

    # --- full build ----------------------------------------------------------
    def build(self, root: Path) -> Index:
        paths = self.discover(root)
        known = set(paths)
        index = Index()
        for rel in paths:
            index.put_file(self.index_file(root, rel, known))
        return index

    # --- incremental update --------------------------------------------------
    def reindex_file(self, index: Index, root: Path, rel_path: str) -> Index:
        """Re-index exactly ``rel_path`` in-place.

        The set of repo paths is edit-invariant (an edit changes content, not the
        file set), so we resolve against the current index's known paths plus the
        edited file. Only this one file is re-parsed.
        """
        known = set(index.files.keys()) | {rel_path}
        target = root / rel_path
        if not target.is_file():
            # File removed: drop its partition (kept for completeness; Phase-1
            # incremental tests exercise content edits).
            index.drop_file(rel_path)
            return index
        index.put_file(self.index_file(root, rel_path, known))
        return index


# --- import resolution ------------------------------------------------------


def _resolve_import(
    language: str, module: str, from_path: str, known_paths: set[str]
) -> str | None:
    if language == "python":
        return _resolve_python(module, from_path, known_paths)
    return _resolve_typescript(module, from_path, known_paths)


def _first_match(candidates: list[str], known_paths: set[str]) -> str | None:
    for c in candidates:
        if c in known_paths:
            return c
    return None


def _resolve_python(module: str, from_path: str, known_paths: set[str]) -> str | None:
    """Resolve a Python import to a repo file.

    Absolute dotted modules are matched by path *suffix* so a package rooted in a
    subdir (e.g. ``backend/app``) resolves without a configured source root.
    Relative imports (leading dots) resolve against the importer's package.
    """
    if module.startswith("."):
        level = len(module) - len(module.lstrip("."))
        rest = module[level:]
        parts = rest.split(".") if rest else []
        base = PurePosixPath(from_path).parent.parts
        # level 1 = same package; each extra dot ascends one directory.
        keep = len(base) - (level - 1)
        if keep < 0:
            return None
        prefix = list(base[:keep]) + parts
        stem = "/".join(prefix)
        return _first_match([f"{stem}.py", f"{stem}/__init__.py"], known_paths)

    joined = "/".join(module.split("."))
    candidates_suffixes = [f"{joined}.py", f"{joined}/__init__.py"]
    matches = sorted(
        p
        for p in known_paths
        for suf in candidates_suffixes
        if p == suf or p.endswith("/" + suf)
    )
    return matches[0] if matches else None


def _resolve_typescript(module: str, from_path: str, known_paths: set[str]) -> str | None:
    """Resolve a relative TS import to a repo file (bare/npm imports → None)."""
    if not module.startswith("."):
        return None  # external package
    base = PurePosixPath(from_path).parent
    target = base
    for part in module.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            target = target.parent
        else:
            target = target / part
    stem = target.as_posix()
    return _first_match(
        [stem, f"{stem}.ts", f"{stem}.tsx", f"{stem}/index.ts", f"{stem}/index.tsx"],
        known_paths,
    )
