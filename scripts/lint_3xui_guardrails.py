#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SKIP_MARKER = "noqa: 3xui-guardrails"
SCAN_EXTENSIONS = {".py", ".md", ".txt", ".yml", ".yaml", ".json", ".sh", ".html"}
TEXT_CONTEXT_EXTENSIONS = {".md", ".txt", ".yml", ".yaml", ".json", ".sh", ".html"}
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

FULL_SUBSCRIPTION_URL_RE = re.compile(r"https?://[^\s`\"'<>()]+/(?:podpiska|pepelvpn|sub)/[0-9a-fA-F]{12,}(?:\b|[/?#])")
HIDDEN_PANEL_URL_RE = re.compile(r"https?://(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}/[A-Za-z0-9_-]{16,}/")
FULL_VLESS_URI_RE = re.compile(r"vless://(?!<uuid>)[^\s`\"'<>()]+@[^\s`\"'<>()]+")
REAL_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


@dataclass(frozen=True)
class GuardrailPattern:
    rule_id: str
    pattern: re.Pattern[str]
    message: str
    text_context_only: bool = False


PATTERNS = (
    GuardrailPattern(
        "FULL_SUBSCRIPTION_URL",
        FULL_SUBSCRIPTION_URL_RE,
        "do not publish real subscription URLs/sub_id; use /podpiska/<sub_id>",
    ),
    GuardrailPattern(
        "HIDDEN_PANEL_URL",
        HIDDEN_PANEL_URL_RE,
        "do not hardcode or publish hidden panel URLs/paths; load from env/DB",
    ),
    GuardrailPattern(
        "FULL_VLESS_URI",
        FULL_VLESS_URI_RE,
        "do not publish full VLESS links; use vless://<uuid>@<host>:443?...#<remark>",
        text_context_only=True,
    ),
    GuardrailPattern(
        "REAL_UUID",
        REAL_UUID_RE,
        "do not publish real UUIDs in docs/CI text; use <uuid>",
        text_context_only=True,
    ),
)

CI_TEXT_ENV_VARS = (
    "CI_COMMIT_TITLE",
    "CI_COMMIT_MESSAGE",
    "CI_MERGE_REQUEST_TITLE",
    "CI_MERGE_REQUEST_DESCRIPTION",
)


def _is_allowed_placeholder(line: str) -> bool:
    return "<sub_id>" in line or "<uuid>" in line or "<subscription" in line


def _iter_scan_files(root: Path):
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file() or path.suffix not in SCAN_EXTENSIONS:
            continue
        yield path


def scan_text_for_guardrail_leaks(
    text: str,
    *,
    label: str,
    text_context: bool,
) -> list[str]:
    errors: list[str] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or SKIP_MARKER in line or _is_allowed_placeholder(line):
            continue
        for pattern in PATTERNS:
            if pattern.text_context_only and not text_context:
                continue
            match = pattern.pattern.search(line)
            if not match:
                continue
            # A leak detector must not reproduce credentials in CI logs.
            errors.append(f"{label}:{line_no} [{pattern.rule_id}] {pattern.message}")
    return errors


def scan_repo(root: Path) -> list[str]:
    errors: list[str] = []
    for path in _iter_scan_files(root):
        rel = path.relative_to(root)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        errors.extend(
            scan_text_for_guardrail_leaks(
                text,
                label=str(rel),
                text_context=path.suffix in TEXT_CONTEXT_EXTENSIONS,
            )
        )
    return errors


def scan_ci_context() -> list[str]:
    errors: list[str] = []
    for env_name in CI_TEXT_ENV_VARS:
        value = os.environ.get(env_name)
        if not value:
            continue
        errors.extend(
            scan_text_for_guardrail_leaks(
                value,
                label=f"env:{env_name}",
                text_context=True,
            )
        )
    return errors


def lint(root: Path) -> list[str]:
    return [*scan_repo(root), *scan_ci_context()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enforce 3x-ui Backend Guardrails leakage rules.")
    parser.add_argument("root", nargs="?", default=".", help="Repository root")
    args = parser.parse_args(argv)

    errors = lint(Path(args.root).resolve())
    if errors:
        print("3x-ui guardrails failed:", file=sys.stderr)
        for error in errors:
            print(f" - {error}", file=sys.stderr)
        return 1
    print("3x-ui guardrails passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
