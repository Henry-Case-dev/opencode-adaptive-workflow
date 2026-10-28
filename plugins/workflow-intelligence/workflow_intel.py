#!/usr/bin/env python3
"""Global OpenCode repository intelligence cache.

Stdlib-only. The SQLite database is disposable navigation state, never source of truth.
Python uses real AST indexing; JS/TS/Go/Rust/Java use lightweight structural indexing.
Git delta + dirty-content fingerprints keep refresh incremental without a daemon.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = "2"
CODE_EXTS = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java"}
TEXT_EXTS = CODE_EXTS | {".json", ".jsonc", ".toml", ".yaml", ".yml", ".md"}
SKIP_PARTS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".cache"}
DEFAULT_MAX_FILE_BYTES = 2_000_000
LIFECYCLE_RE = re.compile(
    r"TEST-LIFECYCLE:\s*(CONTRACT|REGRESSION|INTEGRATION|PROVISIONAL)"
    r"(?:\s+owner=([^\s#*/]+))?",
    re.I,
)
FILE_LIFECYCLE_RE = re.compile(
    r"TEST-LIFECYCLE-FILE:\s*(CONTRACT|REGRESSION|INTEGRATION|PROVISIONAL)"
    r"(?:\s+owner=([^\s#*/]+))?",
    re.I,
)
JS_SYMBOLS = [
    ("class", re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)")),
    ("function", re.compile(r"\b(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")),
    ("function", re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>")),
]
JS_TEST_RE = re.compile(r"\b(?:test|it)(\.skip)?\s*\(\s*['\"]([^'\"]+)['\"]")
JS_IMPORT_RE = re.compile(r"\b(?:import\s+.*?\s+from\s+|require\s*\()\s*['\"]([^'\"]+)['\"]")
POLICY_TEST_RE = re.compile(r"(?:bound|pin|freeze|sha|hash|version|snapshot|allowlist|census)", re.I)


def run_git(repo: Path, *args: str, check: bool = True) -> bytes:
    p = subprocess.run(["git", *args], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace").strip() or f"git {' '.join(args)} failed")
    return p.stdout


def repo_root(start: Path) -> Path:
    try:
        root = run_git(start, "rev-parse", "--show-toplevel").decode().strip()
        return Path(root).resolve()
    except Exception:
        return start.resolve()


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p))).resolve()


def workspace_root() -> Path:
    # Derive from installed OpenCode global config, not OS temp/project-specific paths.
    return Path(__file__).resolve().parents[2] / "WorkspaceData"


def global_cache_root() -> Path:
    return workspace_root() / "cache" / "intelligence"


def global_scratch_root() -> Path:
    return workspace_root() / "temp" / "intelligence"


def repo_key(repo: Path) -> str:
    digest = hashlib.sha1(str(repo).encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return f"{repo.name}-{digest}"


def cache_dir(repo: Path) -> Path:
    return global_cache_root() / repo_key(repo)


def scratch_dir(repo: Path) -> Path:
    injected = os.environ.get("OPENCODE_WORKFLOW_SCRATCH", "").strip()
    if injected:
        return _expand(injected)
    return global_scratch_root() / repo_key(repo)


def db_path(repo: Path) -> Path:
    p = cache_dir(repo) / "repo-intel.sqlite3"
    p.parent.mkdir(parents=True, exist_ok=True)
    owner = p.parent / ".opencode-repo-cache.json"
    if not owner.exists():
        owner.write_text(json.dumps({"schema": 1, "repo": str(repo.resolve()),
                                     "managedBy": "opencode-global-intelligence-v3"}), encoding="utf-8")
    return p


def connect(repo: Path) -> sqlite3.Connection:
    db = sqlite3.connect(db_path(repo))
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS files(
          path TEXT PRIMARY KEY, language TEXT, digest TEXT, size INTEGER, mtime_ns INTEGER
        );
        CREATE TABLE IF NOT EXISTS symbols(
          path TEXT NOT NULL, name TEXT NOT NULL, qualname TEXT, kind TEXT,
          line INTEGER, end_line INTEGER, signature TEXT, doc TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
        CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
        CREATE TABLE IF NOT EXISTS refs(
          path TEXT NOT NULL, name TEXT NOT NULL, kind TEXT, line INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_refs_name ON refs(name);
        CREATE TABLE IF NOT EXISTS imports(
          path TEXT NOT NULL, module TEXT, name TEXT, alias TEXT, line INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(module);
        CREATE TABLE IF NOT EXISTS chunks(
          path TEXT NOT NULL, line INTEGER NOT NULL, text TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
        CREATE TABLE IF NOT EXISTS tests(
          path TEXT NOT NULL, name TEXT NOT NULL, line INTEGER,
          lifecycle TEXT, owner TEXT, body_hash TEXT, skipped INTEGER DEFAULT 0,
          PRIMARY KEY(path, name, line)
        );
        CREATE INDEX IF NOT EXISTS idx_tests_lifecycle ON tests(lifecycle);
        CREATE TABLE IF NOT EXISTS manifests(
          feature_id TEXT PRIMARY KEY, path TEXT NOT NULL, status TEXT,
          requirements_json TEXT, files_json TEXT, tests_json TEXT, evidence_json TEXT
        );
        """
    )
    if meta_get(db, "schema_version") != SCHEMA_VERSION:
        # Disposable cache: schema mismatch is cheaper/safer to rebuild than migrate forever.
        for table in ("files", "symbols", "refs", "imports", "chunks", "tests", "manifests"):
            db.execute(f"DELETE FROM {table}")
        db.execute("DELETE FROM meta")
        meta_set(db, "schema_version", SCHEMA_VERSION)
        db.commit()
    return db


def meta_get(db: sqlite3.Connection, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def zsplit(data: bytes) -> list[str]:
    return [x.decode("utf-8", "surrogateescape").replace("\\", "/") for x in data.split(b"\0") if x]


def current_head(repo: Path) -> str:
    return run_git(repo, "rev-parse", "HEAD", check=False).decode().strip() or "NO_HEAD"


def dirty_paths(repo: Path) -> set[str]:
    out: set[str] = set()
    out.update(zsplit(run_git(repo, "diff", "--name-only", "-z", "--", check=False)))
    out.update(zsplit(run_git(repo, "diff", "--cached", "--name-only", "-z", "--", check=False)))
    out.update(zsplit(run_git(repo, "ls-files", "-z", "--others", "--exclude-standard", check=False)))
    return out


def _hash_file(path: Path) -> str:
    h = hashlib.sha1()
    try:
        with path.open("rb") as f:
            while True:
                chunk = f.read(256 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return "MISSING"


def dirty_fingerprint(repo: Path) -> str:
    # Hash dirty *content*, not only `git status` names. Otherwise a file edited twice
    # while remaining dirty would look unchanged and the cache could go stale.
    h = hashlib.sha1()
    for rel in sorted(dirty_paths(repo)):
        h.update(rel.encode("utf-8", "surrogatepass"))
        p = repo / rel
        if p.exists() and p.is_file():
            try:
                st = p.stat()
                h.update(str(st.st_size).encode())
                h.update(str(st.st_mtime_ns).encode())
            except OSError:
                pass
            h.update(_hash_file(p).encode())
        else:
            h.update(b"MISSING")
    return h.hexdigest()


def is_relevant(rel: str) -> bool:
    p = Path(rel)
    if any(part in SKIP_PARTS for part in p.parts):
        return False
    if p.name == "manifest.json" and ("plans/features/" in rel or "plans/archive/" in rel):
        return True
    return p.suffix.lower() in TEXT_EXTS


def full_file_list(repo: Path) -> set[str]:
    files = zsplit(run_git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard", check=False))
    return {p for p in files if is_relevant(p)}


def incremental_paths(repo: Path, db: sqlite3.Connection, force: bool = False) -> tuple[set[str], bool]:
    head = current_head(repo)
    dirty = dirty_fingerprint(repo)
    last_head = meta_get(db, "last_head")
    last_dirty = meta_get(db, "last_dirty")
    if force or not last_head:
        return full_file_list(repo), True
    if head == last_head and dirty == last_dirty:
        return set(), False

    ancestor_ok = False
    if last_head not in ("", "NO_HEAD") and head not in ("", "NO_HEAD"):
        ancestor_ok = subprocess.run(
            ["git", "merge-base", "--is-ancestor", last_head, head],
            cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    if head != last_head and not ancestor_ok:
        return full_file_list(repo), True

    changed: set[str] = set(dirty_paths(repo))
    if head != last_head and last_head != "NO_HEAD" and head != "NO_HEAD":
        changed.update(zsplit(run_git(repo, "diff", "--name-only", "-z", f"{last_head}..{head}", "--", check=False)))
    # Keep deleted/renamed paths so cached rows are purged.
    return {p for p in changed if is_relevant(p) or db.execute("SELECT 1 FROM files WHERE path=?", (p,)).fetchone()}, False


def purge_path(db: sqlite3.Connection, rel: str) -> None:
    for table in ("symbols", "refs", "imports", "chunks", "tests"):
        db.execute(f"DELETE FROM {table} WHERE path=?", (rel,))
    db.execute("DELETE FROM files WHERE path=?", (rel,))
    db.execute("DELETE FROM manifests WHERE path=?", (rel,))


def file_lifecycle_marker(lines: list[str]) -> tuple[str, str]:
    for line in lines[:60]:
        m = FILE_LIFECYCLE_RE.search(line)
        if m:
            return m.group(1).upper(), (m.group(2) or "").strip()
    return "UNCLASSIFIED", ""


def nearby_lifecycle(lines: list[str], lineno: int, fallback: tuple[str, str]) -> tuple[str, str]:
    start = max(0, lineno - 6)
    for line in reversed(lines[start:max(0, lineno - 1)]):
        m = LIFECYCLE_RE.search(line)
        if m:
            return m.group(1).upper(), (m.group(2) or "").strip()
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", "//", "/*", "*")):
            break
    return fallback


def chunk_text(db: sqlite3.Connection, rel: str, text: str) -> None:
    lines = text.splitlines()
    if not lines:
        return
    width, step = 60, 50
    rows = []
    for start in range(0, len(lines), step):
        body = "\n".join(lines[start:start + width]).strip()
        if body:
            rows.append((rel, start + 1, body[:12000]))
    db.executemany("INSERT INTO chunks(path,line,text) VALUES(?,?,?)", rows)


def py_signature(node: ast.AST) -> str:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ""
    try:
        args = [a.arg for a in node.args.posonlyargs + node.args.args]
        if node.args.vararg:
            args.append("*" + node.args.vararg.arg)
        args.extend(a.arg for a in node.args.kwonlyargs)
        if node.args.kwarg:
            args.append("**" + node.args.kwarg.arg)
        return f"({', '.join(args)})"
    except Exception:
        return ""


class PyVisitor(ast.NodeVisitor):
    def __init__(self, rel: str, lines: list[str], file_marker: tuple[str, str]):
        self.rel = rel
        self.lines = lines
        self.file_marker = file_marker
        self.stack: list[str] = []
        self.symbols: list[tuple] = []
        self.refs: list[tuple] = []
        self.imports: list[tuple] = []
        self.tests: list[tuple] = []

    def qname(self, name: str) -> str:
        return ".".join([*self.stack, name]) if self.stack else name

    def add_symbol(self, node: ast.AST, name: str, kind: str, sig: str = "") -> None:
        doc = ""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            try:
                doc = (ast.get_docstring(node) or "").strip().replace("\n", " ")[:400]
            except Exception:
                pass
        self.symbols.append((self.rel, name, self.qname(name), kind, getattr(node, "lineno", 0), getattr(node, "end_lineno", 0), sig, doc))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.add_symbol(node, node.name, "class")
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str) -> None:
        self.add_symbol(node, node.name, kind, py_signature(node))
        if node.name.startswith("test_"):
            lc, owner = nearby_lifecycle(self.lines, node.lineno, self.file_marker)
            skipped = any("pytest.mark.skip" in ast.dump(d, include_attributes=False) for d in node.decorator_list)
            bh = hashlib.sha1(ast.dump(ast.Module(body=node.body, type_ignores=[]), include_attributes=False).encode()).hexdigest()
            self.tests.append((self.rel, self.qname(node.name), node.lineno, lc, owner, bh, int(skipped)))
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func(node, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func(node, "async_function")

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.imports.append((self.rel, a.name, "", a.asname or "", node.lineno))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = ("." * node.level) + (node.module or "")
        for a in node.names:
            self.imports.append((self.rel, module, a.name, a.asname or "", node.lineno))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self.refs.append((self.rel, node.id, "name", node.lineno))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.refs.append((self.rel, node.attr, "attribute", node.lineno))
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self.stack:
            for target in node.targets:
                if isinstance(target, ast.Name) and (target.id.isupper() or not target.id.startswith("_")):
                    self.add_symbol(node, target.id, "variable")
        self.generic_visit(node)


def index_python(db: sqlite3.Connection, rel: str, text: str) -> None:
    lines = text.splitlines()
    marker = file_lifecycle_marker(lines)
    tree = ast.parse(text, filename=rel)
    v = PyVisitor(rel, lines, marker)
    v.visit(tree)
    db.executemany("INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?)", v.symbols)
    db.executemany("INSERT INTO refs VALUES(?,?,?,?)", v.refs)
    db.executemany("INSERT INTO imports VALUES(?,?,?,?,?)", v.imports)
    db.executemany("INSERT INTO tests VALUES(?,?,?,?,?,?,?)", v.tests)


def index_js(db: sqlite3.Connection, rel: str, text: str) -> None:
    lines = text.splitlines()
    marker = file_lifecycle_marker(lines)
    symbols, refs, imports, tests = [], [], [], []
    for no, line in enumerate(lines, 1):
        for kind, rx in JS_SYMBOLS:
            for m in rx.finditer(line):
                symbols.append((rel, m.group(1), m.group(1), kind, no, no, "", ""))
        for m in JS_IMPORT_RE.finditer(line):
            imports.append((rel, m.group(1), "", "", no))
        for m in re.finditer(r"\b([A-Za-z_$][\w$]*)\b", line):
            refs.append((rel, m.group(1), "token", no))
        for m in JS_TEST_RE.finditer(line):
            lc, owner = nearby_lifecycle(lines, no, marker)
            bh = hashlib.sha1(re.sub(r"\s+", " ", line.strip()).encode()).hexdigest()
            tests.append((rel, m.group(2), no, lc, owner, bh, int(bool(m.group(1)))))
    db.executemany("INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?)", symbols)
    db.executemany("INSERT INTO refs VALUES(?,?,?,?)", refs)
    db.executemany("INSERT INTO imports VALUES(?,?,?,?,?)", imports)
    db.executemany("INSERT INTO tests VALUES(?,?,?,?,?,?,?)", tests)


def index_generic(db: sqlite3.Connection, rel: str, text: str) -> None:
    ext = Path(rel).suffix.lower()
    lines = text.splitlines()
    marker = file_lifecycle_marker(lines)
    symbols, refs, tests = [], [], []
    patterns: list[tuple[str, re.Pattern[str]]] = []
    if ext == ".go":
        patterns = [
            ("type", re.compile(r"^\s*type\s+([A-Za-z_]\w*)")),
            ("function", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(")),
        ]
    elif ext == ".rs":
        patterns = [
            ("type", re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)")),
            ("function", re.compile(r"^\s*(?:pub\s+)?fn\s+([A-Za-z_]\w*)\s*\(")),
        ]
    elif ext == ".java":
        patterns = [("class", re.compile(r"\b(?:class|interface|enum)\s+([A-Za-z_]\w*)"))]

    rust_test_pending = False
    java_test_pending = False
    for no, line in enumerate(lines, 1):
        stripped = line.strip()
        if ext == ".rs" and stripped.startswith("#[test]"):
            rust_test_pending = True
        if ext == ".java" and re.search(r"@(?:Test|ParameterizedTest)\b", stripped):
            java_test_pending = True
        for kind, rx in patterns:
            m = rx.search(line)
            if m:
                name = m.group(1)
                symbols.append((rel, name, name, kind, no, no, "", ""))
                is_test = (ext == ".go" and Path(rel).name.endswith("_test.go") and name.startswith("Test")) or (ext == ".rs" and rust_test_pending) or (ext == ".java" and java_test_pending and kind == "function")
                if is_test:
                    lc, owner = nearby_lifecycle(lines, no, marker)
                    tests.append((rel, name, no, lc, owner, hashlib.sha1(stripped.encode()).hexdigest(), 0))
                rust_test_pending = False
                java_test_pending = False
        for m in re.finditer(r"\b[A-Za-z_]\w*\b", line):
            refs.append((rel, m.group(0), "token", no))
    db.executemany("INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?)", symbols)
    db.executemany("INSERT INTO refs VALUES(?,?,?,?)", refs)
    db.executemany("INSERT INTO tests VALUES(?,?,?,?,?,?,?)", tests)


def index_manifest(db: sqlite3.Connection, rel: str, text: str) -> None:
    try:
        obj = json.loads(text)
    except Exception:
        return
    fid = str(obj.get("feature_id") or obj.get("feature") or "").strip()
    if not fid:
        return
    db.execute(
        "INSERT INTO manifests(feature_id,path,status,requirements_json,files_json,tests_json,evidence_json) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(feature_id) DO UPDATE SET path=excluded.path,status=excluded.status,requirements_json=excluded.requirements_json,files_json=excluded.files_json,tests_json=excluded.tests_json,evidence_json=excluded.evidence_json",
        (
            fid, rel, str(obj.get("status", "")),
            json.dumps(obj.get("requirements", []), ensure_ascii=False),
            json.dumps(obj.get("files", obj.get("entrypoints", [])), ensure_ascii=False),
            json.dumps(obj.get("tests", []), ensure_ascii=False),
            json.dumps(obj.get("evidence", []), ensure_ascii=False),
        ),
    )


def language_for(path: Path) -> str:
    return {
        ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
        ".ts": "typescript", ".tsx": "typescript", ".go": "go", ".rs": "rust", ".java": "java",
    }.get(path.suffix.lower(), "text")


def max_file_bytes() -> int:
    raw = os.environ.get("OPENCODE_WORKFLOW_MAX_FILE_BYTES", "").strip()
    try:
        return max(64_000, int(raw)) if raw else DEFAULT_MAX_FILE_BYTES
    except ValueError:
        return DEFAULT_MAX_FILE_BYTES


def refresh(repo: Path, db: sqlite3.Connection, force: bool = False) -> dict:
    started = time.perf_counter()
    paths, full = incremental_paths(repo, db, force)
    head = current_head(repo)
    dirty = dirty_fingerprint(repo)
    indexed = removed = errors = skipped_large = 0
    limit_bytes = max_file_bytes()
    for rel in sorted(paths):
        abs_path = repo / rel
        if not abs_path.exists() or not abs_path.is_file():
            purge_path(db, rel)
            removed += 1
            continue
        if not is_relevant(rel):
            purge_path(db, rel)
            continue
        purge_path(db, rel)
        try:
            st = abs_path.stat()
            if st.st_size > limit_bytes:
                db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)", (rel, language_for(abs_path), "SKIPPED_LARGE", st.st_size, st.st_mtime_ns))
                skipped_large += 1
                continue
            data = abs_path.read_bytes()
            if b"\0" in data[:8192]:
                continue
            text = data.decode("utf-8", "replace")
            digest = hashlib.sha1(data).hexdigest()
            db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)", (rel, language_for(abs_path), digest, len(data), st.st_mtime_ns))
            chunk_text(db, rel, text)
            if abs_path.name == "manifest.json" and (rel.startswith("plans/features/") or rel.startswith("plans/archive/")):
                index_manifest(db, rel, text)
            elif abs_path.suffix.lower() in {".py", ".pyi"}:
                index_python(db, rel, text)
            elif abs_path.suffix.lower() in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
                index_js(db, rel, text)
            elif abs_path.suffix.lower() in {".go", ".rs", ".java"}:
                index_generic(db, rel, text)
            indexed += 1
        except (SyntaxError, UnicodeError, OSError):
            errors += 1
        except Exception:
            errors += 1
    meta_set(db, "last_head", head)
    meta_set(db, "last_dirty", dirty)
    db.commit()
    return {
        "mode": "full" if full else "incremental",
        "changed": len(paths), "indexed": indexed, "removed": removed,
        "skipped_large": skipped_large, "errors": errors,
        "head": head[:12], "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "db": str(db_path(repo)),
    }


def rows_as_dict(rows: Iterable[sqlite3.Row], limit: int) -> list[dict]:
    return [dict(r) for r in list(rows)[:limit]]


def locate(db: sqlite3.Connection, query: str, limit: int) -> list[dict]:
    q = query.strip()
    rows = db.execute(
        "SELECT path,name,qualname,kind,line,end_line,signature,doc FROM symbols "
        "WHERE name=? OR qualname=? ORDER BY CASE WHEN name=? THEN 0 ELSE 1 END,path,line LIMIT ?",
        (q, q, q, limit),
    ).fetchall()
    if rows:
        return rows_as_dict(rows, limit)
    like = f"%{q}%"
    return rows_as_dict(db.execute(
        "SELECT path,name,qualname,kind,line,end_line,signature,doc FROM symbols "
        "WHERE name LIKE ? OR qualname LIKE ? OR path LIKE ? ORDER BY path,line LIMIT ?",
        (like, like, like, limit),
    ).fetchall(), limit)


def references(db: sqlite3.Connection, query: str, limit: int) -> list[dict]:
    return rows_as_dict(db.execute(
        "SELECT path,name,kind,line FROM refs WHERE name=? ORDER BY path,line LIMIT ?",
        (query.strip(), limit),
    ).fetchall(), limit)


def search(db: sqlite3.Connection, query: str, limit: int) -> list[dict]:
    terms = [t for t in re.split(r"\s+", query.strip()) if t][:6]
    if not terms:
        return []
    # Prefer symbols/docs first.
    clauses, args = [], []
    for t in terms:
        like = f"%{t}%"
        clauses.append("(name LIKE ? OR qualname LIKE ? OR path LIKE ? OR doc LIKE ?)")
        args.extend([like, like, like, like])
    sql = "SELECT path,name,qualname,kind,line,signature,doc FROM symbols WHERE " + " AND ".join(clauses) + " ORDER BY path,line LIMIT ?"
    args.append(limit)
    symbol_rows = rows_as_dict(db.execute(sql, args).fetchall(), limit)
    if symbol_rows:
        return [{"source": "symbol", **r} for r in symbol_rows]

    # Then lexical chunk search. Not embeddings: deterministic and cheap.
    chunk_clauses, chunk_args = [], []
    for t in terms:
        chunk_clauses.append("LOWER(text) LIKE ?")
        chunk_args.append(f"%{t.lower()}%")
    csql = "SELECT path,line,text FROM chunks WHERE " + " AND ".join(chunk_clauses) + " ORDER BY path,line LIMIT ?"
    chunk_args.append(limit)
    out = []
    for r in db.execute(csql, chunk_args).fetchall():
        preview = re.sub(r"\s+", " ", r["text"]).strip()[:700]
        out.append({"source": "text", "path": r["path"], "line": r["line"], "preview": preview})
    return out


def impact(db: sqlite3.Connection, query: str, limit: int) -> dict:
    loc = locate(db, query, max(5, min(limit, 20)))
    refs = references(db, query, limit)
    paths = sorted({r["path"] for r in refs})
    return {
        "definitions": loc,
        "reference_files": paths[:limit],
        "references": refs[:limit],
        "note": "Static hint only. Prefer native LSP when available and verify current code/tests for authoritative impact.",
    }


def test_audit(db: sqlite3.Connection) -> dict:
    total = db.execute("SELECT COUNT(*) FROM tests").fetchone()[0]
    by_lc = {r[0]: r[1] for r in db.execute("SELECT lifecycle,COUNT(*) FROM tests GROUP BY lifecycle ORDER BY lifecycle")}
    unclassified = rows_as_dict(db.execute("SELECT path,name,line FROM tests WHERE lifecycle='UNCLASSIFIED' ORDER BY path,line LIMIT 80"), 80)
    provisional = rows_as_dict(db.execute("SELECT path,name,line,owner FROM tests WHERE lifecycle='PROVISIONAL' ORDER BY path,line LIMIT 80"), 80)
    skipped = rows_as_dict(db.execute("SELECT path,name,line,lifecycle,owner FROM tests WHERE skipped=1 ORDER BY path,line LIMIT 50"), 50)
    policy = rows_as_dict(db.execute("SELECT path,name,line,lifecycle,owner FROM tests ORDER BY path,line"), 5000)
    policy = [r for r in policy if POLICY_TEST_RE.search(r["name"]) or POLICY_TEST_RE.search(r["path"])][:60]
    dup_groups = []
    for r in db.execute("SELECT body_hash,COUNT(*) c FROM tests WHERE body_hash<>'' GROUP BY body_hash HAVING c>1 ORDER BY c DESC LIMIT 20"):
        members = rows_as_dict(db.execute("SELECT path,name,line,lifecycle,owner FROM tests WHERE body_hash=? ORDER BY path,line LIMIT 10", (r[0],)), 10)
        dup_groups.append({"count": r[1], "members": members})
    baseline_raw = meta_get(db, "test_baseline_count")
    baseline = int(baseline_raw) if baseline_raw.isdigit() else None
    growth_abs = total - baseline if baseline is not None else None
    growth_pct = ((total - baseline) / baseline * 100.0) if baseline else None
    trigger = bool(baseline and (growth_abs >= 300 or growth_pct >= 15.0)) or bool(provisional)
    return {
        "total": total,
        "by_lifecycle": by_lc,
        "baseline": baseline,
        "growth_abs": growth_abs,
        "growth_pct": round(growth_pct, 2) if growth_pct is not None else None,
        "portfolio_audit_trigger": trigger,
        "unclassified_sample": unclassified,
        "provisional": provisional,
        "skipped_sample": skipped,
        "duplicate_body_candidates": dup_groups,
        "bounds_pin_snapshot_candidates": policy,
        "policy": "Candidates are hints only. Never auto-delete tests; cleanup is Builder change + Reviewer gate.",
    }


def set_test_baseline(db: sqlite3.Connection) -> dict:
    total = db.execute("SELECT COUNT(*) FROM tests").fetchone()[0]
    meta_set(db, "test_baseline_count", str(total))
    db.commit()
    return {"baseline_set": total}


def manifest_paths(obj: object) -> list[str]:
    out: list[str] = []
    if isinstance(obj, list):
        for v in obj:
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, dict) and isinstance(v.get("path"), str):
                out.append(v["path"])
    return out


def manifest_validate(repo: Path, db: sqlite3.Connection, feature: str | None, limit: int) -> dict:
    rows = db.execute("SELECT * FROM manifests WHERE feature_id=?", (feature,)).fetchall() if feature else db.execute("SELECT * FROM manifests ORDER BY feature_id LIMIT ?", (limit,)).fetchall()
    results = []
    for r in rows:
        issues: list[str] = []
        reqs = json.loads(r["requirements_json"] or "[]")
        files = json.loads(r["files_json"] or "[]")
        tests = json.loads(r["tests_json"] or "[]")
        if not reqs:
            issues.append("no requirements")
        all_paths = manifest_paths(files) + manifest_paths(tests)
        for p in all_paths:
            if not (repo / p).exists():
                issues.append(f"missing path: {p}")
        provisional, unclassified = [], []
        for p in manifest_paths(tests):
            for tr in db.execute("SELECT name,line,lifecycle,owner FROM tests WHERE path=?", (p,)):
                item = {"path": p, **dict(tr)}
                if tr["lifecycle"] == "PROVISIONAL":
                    provisional.append(item)
                elif tr["lifecycle"] == "UNCLASSIFIED":
                    unclassified.append(item)
        if provisional:
            issues.append(f"{len(provisional)} provisional tests")
        results.append({
            "feature_id": r["feature_id"], "path": r["path"], "status": r["status"],
            "issues": issues, "provisional": provisional[:20], "unclassified_sample": unclassified[:20],
        })
    if feature and not rows:
        return {"feature": feature, "found": False, "issues": ["manifest not found"]}
    return {"found": len(rows), "results": results}


def feature_context(db: sqlite3.Connection, feature: str, limit: int) -> dict:
    r = db.execute("SELECT * FROM manifests WHERE feature_id=?", (feature,)).fetchone()
    if not r:
        return {"feature": feature, "found": False}
    files = json.loads(r["files_json"] or "[]")
    tests = json.loads(r["tests_json"] or "[]")
    paths = manifest_paths(files)
    syms: list[dict] = []
    for p in paths[:30]:
        syms.extend(rows_as_dict(db.execute("SELECT path,name,kind,line,signature FROM symbols WHERE path=? ORDER BY line LIMIT 20", (p,)), 20))
        if len(syms) >= limit:
            break
    return {
        "feature": feature,
        "found": True,
        "manifest": {
            "path": r["path"], "status": r["status"],
            "requirements": json.loads(r["requirements_json"] or "[]"),
            "files": files, "tests": tests,
            "evidence": json.loads(r["evidence_json"] or "[]"),
        },
        "symbols": syms[:limit],
    }


def stats(db: sqlite3.Connection) -> dict:
    return {
        "files": db.execute("SELECT COUNT(*) FROM files").fetchone()[0],
        "symbols": db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
        "references": db.execute("SELECT COUNT(*) FROM refs").fetchone()[0],
        "chunks": db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "tests": db.execute("SELECT COUNT(*) FROM tests").fetchone()[0],
        "manifests": db.execute("SELECT COUNT(*) FROM manifests").fetchone()[0],
        "last_head": meta_get(db, "last_head")[:12],
    }


def dir_size(path: Path, cap_files: int = 50_000) -> tuple[int, int]:
    size = count = 0
    if not path.exists():
        return 0, 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not Path(root, d).is_symlink()]
        for f in files:
            count += 1
            if count > cap_files:
                return size, count
            try:
                size += Path(root, f).stat().st_size
            except OSError:
                pass
    return size, count


def scratch_status(repo: Path) -> dict:
    p = scratch_dir(repo)
    size, count = dir_size(p)
    return {
        "path": str(p), "exists": p.exists(), "bytes": size, "files_seen": count,
        "root_configured": bool(os.environ.get("OPENCODE_WORKFLOW_SCRATCH_ROOT", "").strip()),
        "gc": "workflow.intelligence plugin removes only managed scratch children older than TTL; durable evidence must be promoted first",
    }


def output(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main() -> int:
    actions = ["refresh", "locate", "refs", "search", "impact", "stats", "test_audit", "test_baseline", "manifest_validate", "feature_context", "scratch_status"]
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=actions)
    ap.add_argument("--query", default="")
    ap.add_argument("--feature", default="")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--repo", default=os.getcwd())
    ns = ap.parse_args()
    repo = repo_root(Path(ns.repo))
    db = connect(repo)
    refresh_info = refresh(repo, db, force=ns.full if ns.action == "refresh" else False)
    limit = max(1, min(ns.limit, 100))
    if ns.action == "refresh": result = refresh_info
    elif ns.action == "locate": result = {"refresh": refresh_info, "results": locate(db, ns.query, limit)}
    elif ns.action == "refs": result = {"refresh": refresh_info, "results": references(db, ns.query, limit)}
    elif ns.action == "search": result = {"refresh": refresh_info, "results": search(db, ns.query, limit)}
    elif ns.action == "impact": result = {"refresh": refresh_info, **impact(db, ns.query, limit)}
    elif ns.action == "stats": result = {"refresh": refresh_info, **stats(db)}
    elif ns.action == "test_audit": result = {"refresh": refresh_info, **test_audit(db)}
    elif ns.action == "test_baseline": result = {"refresh": refresh_info, **set_test_baseline(db)}
    elif ns.action == "manifest_validate": result = {"refresh": refresh_info, **manifest_validate(repo, db, ns.feature or None, limit)}
    elif ns.action == "feature_context": result = {"refresh": refresh_info, **feature_context(db, ns.feature, limit)}
    elif ns.action == "scratch_status": result = scratch_status(repo)
    else: raise AssertionError(ns.action)
    output(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
