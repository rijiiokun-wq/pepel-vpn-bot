#!/usr/bin/env python3
"""
Async-safety linter for the PEPELBOT codebase.

Checks patterns that can block the asyncio event loop:
1. Known blocking sync calls inside `async def`
2. Sync network/database patterns inside `async def`
3. py3xui sync panel calls inside `async def` unless wrapped in executor
4. Optional: sync `database.*` / imports from `shop_bot.data_manager.database` inside `async def`
   (enable with `--database-in-async`; off by default for CI until call sites are migrated)

Suppress a specific line with: `# noqa: async-safety`
"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path

BLOCKING_CALLS = {
    "time.sleep": "blocks event loop; use asyncio.sleep() or run_in_executor",
    "socket.setdefaulttimeout": "sync socket config in async context; move to executor",
    "sqlite3.connect": "sync DB connect; use DB layer or run_in_executor",
    "urllib.request.urlopen": "sync HTTP; use aiohttp or run_in_executor",
    "requests.get": "sync HTTP; use aiohttp or run_in_executor",
    "requests.post": "sync HTTP; use aiohttp or run_in_executor",
    "requests.put": "sync HTTP; use aiohttp or run_in_executor",
    "requests.patch": "sync HTTP; use aiohttp or run_in_executor",
    "requests.delete": "sync HTTP; use aiohttp or run_in_executor",
    "requests.request": "sync HTTP; use aiohttp or run_in_executor",
}

BLOCKING_SUFFIX_CALLS = {
    ".inbound.get_by_id": "py3xui sync API call; wrap in run_in_executor",
    ".inbound.get_list": "py3xui sync API call; wrap in run_in_executor",
    ".inbound.update": "py3xui sync API call; wrap in run_in_executor",
    ".client.delete": "py3xui sync API call; wrap in run_in_executor",
    ".login_to_host": "sync host login; wrap in run_in_executor",
    ".login_to_host_detailed": "sync host login; wrap in run_in_executor",
}

BLOCKING_METHOD_PATTERNS = {
    "urlopen": "sync HTTP; use aiohttp or run_in_executor",
}

EXECUTOR_WRAPPER_SUFFIXES = {
    ".run_in_executor",
    ".to_thread",
}

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
}
NOQA_MARKER = "noqa: async-safety"
ZERO_SHA = "0" * 40


def _is_database_module_name(module: str | None) -> bool:
    if not module:
        return False
    return module == "shop_bot.data_manager.database" or module.endswith(".database")


class AsyncSafetyVisitor(ast.NodeVisitor):
    """Walk AST and find blocking calls inside async functions."""

    def __init__(self, lines: list[str], *, check_database_sync: bool = False):
        self.errors: list[tuple[int, str]] = []
        self._in_async = False
        self._in_executor = False
        self._lines = lines
        self._check_database_sync = check_database_sync
        self._database_func_names: set[str] = set()
        self._database_module_aliases: set[str] = set()

    def _line_has_noqa(self, lineno: int) -> bool:
        if 1 <= lineno <= len(self._lines):
            return NOQA_MARKER in self._lines[lineno - 1]
        return False

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if _is_database_module_name(alias.name):
                self._database_module_aliases.add(alias.asname or alias.name.rsplit(".", 1)[-1])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if _is_database_module_name(node.module):
            for alias in node.names:
                if alias.name == "*":
                    continue
                self._database_func_names.add(alias.asname or alias.name)
        elif node.module == "shop_bot.data_manager":
            for alias in node.names:
                if alias.name == "database":
                    self._database_module_aliases.add(alias.asname or "database")
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        prev_async = self._in_async
        self._in_async = True
        self.generic_visit(node)
        self._in_async = prev_async

    def visit_FunctionDef(self, node: ast.FunctionDef):
        prev_async = self._in_async
        self._in_async = False
        self.generic_visit(node)
        self._in_async = prev_async

    def visit_Call(self, node: ast.Call):
        call_name = self._resolve_call_name(node.func)
        if self._is_executor_wrapper(call_name):
            prev_executor = self._in_executor
            self._in_executor = True
            for arg in node.args:
                self.visit(arg)
            for kw in node.keywords:
                if kw.value is not None:
                    self.visit(kw.value)
            self._in_executor = prev_executor
            return

        if self._in_async and not self._in_executor and not self._line_has_noqa(node.lineno):
            error = self._resolve_violation(call_name, node.func)
            if error:
                self.errors.append((node.lineno, error))
            elif self._check_database_sync:
                db_err = self._database_sync_violation(node.func)
                if db_err:
                    self.errors.append((node.lineno, db_err))

        self.generic_visit(node)

    def _resolve_violation(self, call_name: str, func_node: ast.expr) -> str | None:
        if call_name in BLOCKING_CALLS:
            return f"{call_name}() inside async function: {BLOCKING_CALLS[call_name]}"

        for suffix, reason in BLOCKING_SUFFIX_CALLS.items():
            if call_name.endswith(suffix):
                return f"{call_name}() inside async function: {reason}"

        if isinstance(func_node, ast.Attribute):
            method = func_node.attr
            if method in BLOCKING_METHOD_PATTERNS:
                return (
                    f".{method}() inside async function: "
                    f"{BLOCKING_METHOD_PATTERNS[method]}"
                )

        return None

    def _database_sync_violation(self, func_node: ast.expr) -> str | None:
        if isinstance(func_node, ast.Name) and func_node.id in self._database_func_names:
            return (
                f"{func_node.id}() looks like a sync database call inside async; "
                "use asyncio.to_thread() or run_in_executor"
            )
        if (
            isinstance(func_node, ast.Attribute)
            and isinstance(func_node.value, ast.Name)
            and func_node.value.id in self._database_module_aliases
        ):
            return (
                f"{func_node.value.id}.{func_node.attr}() sync database call inside async; "
                "use asyncio.to_thread() or run_in_executor"
            )
        return None

    @staticmethod
    def _is_executor_wrapper(call_name: str) -> bool:
        if call_name == "asyncio.to_thread":
            return True
        return any(call_name.endswith(suffix) for suffix in EXECUTOR_WRAPPER_SUFFIXES)

    @staticmethod
    def _resolve_call_name(node: ast.expr) -> str:
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return ".".join(reversed(parts))


def _run_git(args: list[str], *, cwd: Path) -> str | None:
    try:
        out = subprocess.check_output(
            args,
            cwd=str(cwd),
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.strip()


def _resolve_base_sha(root: Path, explicit_base_sha: str | None) -> str | None:
    candidate = (explicit_base_sha or "").strip()
    if not candidate:
        candidate = (os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA") or "").strip()
    if not candidate:
        candidate = (os.environ.get("CI_COMMIT_BEFORE_SHA") or "").strip()
    if candidate == ZERO_SHA:
        candidate = ""
    if candidate:
        return candidate
    return _run_git(["git", "rev-parse", "HEAD~1"], cwd=root)


def _resolve_head_sha(explicit_head_sha: str | None) -> str:
    candidate = (explicit_head_sha or "").strip()
    if candidate:
        return candidate
    env_sha = (os.environ.get("CI_COMMIT_SHA") or "").strip()
    if env_sha:
        return env_sha
    return "HEAD"


def _iter_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def _iter_changed_python_files(root: Path, *, base_sha: str, head_sha: str) -> list[Path]:
    diff_out = _run_git(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMRT",
            base_sha,
            head_sha,
            "--",
            "*.py",
        ],
        cwd=root,
    )
    if not diff_out:
        return []

    files: list[Path] = []
    for raw in diff_out.splitlines():
        rel = raw.strip()
        if not rel:
            continue
        path = root / rel
        if not path.exists() or not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


def check_file(path: Path, *, check_database_sync: bool = False) -> list[str]:
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    visitor = AsyncSafetyVisitor(source.splitlines(), check_database_sync=check_database_sync)
    visitor.visit(tree)
    return [f"{path}:{lineno}  [ASYNC_BLOCK]  {msg}" for lineno, msg in visitor.errors]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lint blocking sync calls inside async functions.")
    parser.add_argument("root", nargs="?", default=".", help="Repository root (default: current directory)")
    parser.add_argument(
        "--changed-only",
        action="store_true",
        help="Check only changed .py files from git diff base..head",
    )
    parser.add_argument("--base-sha", default="", help="Base revision for --changed-only")
    parser.add_argument("--head-sha", default="", help="Head revision for --changed-only")
    parser.add_argument(
        "--database-in-async",
        action="store_true",
        help="Also flag shop_bot.data_manager.database imports/calls inside async (opt-in; noisy)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = Path(args.root).resolve()

    files_to_check: list[Path]
    if args.changed_only:
        base_sha = _resolve_base_sha(root, args.base_sha)
        head_sha = _resolve_head_sha(args.head_sha)
        if not base_sha:
            print("async-safety lint: base sha not found, fallback to full scan")
            files_to_check = _iter_python_files(root)
        else:
            files_to_check = _iter_changed_python_files(root, base_sha=base_sha, head_sha=head_sha)
            if not files_to_check:
                print(f"async-safety lint: no changed Python files in range {base_sha}..{head_sha}")
                return 0
    else:
        files_to_check = _iter_python_files(root)

    all_errors: list[str] = []
    for path in files_to_check:
        db_sync = bool(args.database_in_async)
        if db_sync and ("tests" in path.parts or "scripts" in path.parts):
            db_sync = False
        all_errors.extend(check_file(path, check_database_sync=db_sync))

    if all_errors:
        print("async-safety lint failed:", file=sys.stderr)
        for err in all_errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    scope = "changed files" if args.changed_only else "full repo"
    print(f"async-safety lint passed ({scope}, files={len(files_to_check)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
