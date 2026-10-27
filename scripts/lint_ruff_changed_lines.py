#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

RULE_PROFILES = {
    "critical": "F,E9",
    "security": "S,B904,RUF006",
    "best-practices": ("A,ARG,ASYNC,B,BLE,C4,DTZ,G,LOG,PIE,PERF,PLE,PLW,RET,RSE,SIM,T20,TRY,UP,RUF"),
}
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _run_git(root: Path, args: list[str]) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=root,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _changed_python_files(root: Path, *, base_sha: str, head_sha: str) -> list[str]:
    raw_output = _run_git(
        root,
        [
            "diff",
            "--name-only",
            "--diff-filter=ACMRT",
            base_sha,
            head_sha,
            "--",
            "*.py",
        ],
    )
    return [line.strip() for line in raw_output.splitlines() if line.strip()]


def _changed_lines_for(root: Path, *, base_sha: str, head_sha: str, file_path: str) -> set[int]:
    diff_text = _run_git(
        root,
        [
            "diff",
            "--unified=0",
            "--no-color",
            base_sha,
            head_sha,
            "--",
            file_path,
        ],
    )

    lines: set[int] = set()
    for raw_line in diff_text.splitlines():
        match = HUNK_RE.match(raw_line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        if count > 0:
            lines.update(range(start, start + count))
    return lines


def _ruff_global_ignores(root: Path) -> str:
    config_path = root / "pyproject.toml"
    if not config_path.exists():
        return ""
    with config_path.open("rb") as config_file:
        config = tomllib.load(config_file)
    ignored_rules = config.get("tool", {}).get("ruff", {}).get("lint", {}).get("ignore", [])
    if not isinstance(ignored_rules, list):
        return ""
    normalized = [str(rule).strip() for rule in ignored_rules if str(rule).strip()]
    return ",".join(normalized)


def _ruff_diagnostics(root: Path, *, file_paths: list[str], rules: str, ignored_rules: str) -> list[dict[str, object]]:
    if not file_paths:
        return []

    command = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--select",
        rules,
        "--output-format=json",
    ]
    if ignored_rules:
        command.extend(["--ignore", ignored_rules])
    command.extend(file_paths)

    proc = subprocess.run(
        command,
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        if proc.stdout:
            print(proc.stdout)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)
        raise SystemExit(proc.returncode)

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return []
    return json.loads(stdout)


def _diagnostic_file_path(root: Path, filename: object, *, fallback_file: str | None = None) -> str:
    raw_filename = str(filename or "").strip()
    if not raw_filename and fallback_file:
        return fallback_file
    path = Path(raw_filename)
    if not path.is_absolute():
        return raw_filename
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return raw_filename


def _combined_rules(*, profiles: list[str], explicit_rules: list[str]) -> str:
    selectors: list[str] = []
    for profile in profiles:
        selectors.extend(RULE_PROFILES[profile].split(","))
    for rules in explicit_rules:
        selectors.extend(rules.split(","))

    normalized: list[str] = []
    seen: set[str] = set()
    for selector in selectors:
        rule = selector.strip()
        if not rule or rule in seen:
            continue
        seen.add(rule)
        normalized.append(rule)
    return ",".join(normalized)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Ruff only on changed Python lines.")
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--base-sha", required=True, help="Base revision")
    parser.add_argument("--head-sha", default="HEAD", help="Head revision")
    parser.add_argument(
        "--profile",
        action="append",
        choices=sorted(RULE_PROFILES),
        default=[],
        help="Named rule profile to check; can be passed multiple times",
    )
    parser.add_argument(
        "--rules",
        action="append",
        default=[],
        help="Comma-separated Ruff rule selectors; can be passed multiple times",
    )
    parser.add_argument("--label", default="ruff-changed-lines", help="Label used in output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = Path(args.root).resolve()
    rules = _combined_rules(profiles=args.profile, explicit_rules=args.rules)
    ignored_rules = _ruff_global_ignores(root)
    if not rules:
        print(f"{args.label}: no Ruff rules selected", file=sys.stderr)
        return 2

    changed_files = _changed_python_files(root, base_sha=args.base_sha, head_sha=args.head_sha)
    if not changed_files:
        print(f"{args.label}: no changed Python files")
        return 0

    changed_lines_by_file: dict[str, set[int]] = {}
    for file_path in changed_files:
        path = root / file_path
        if not path.exists():
            continue

        changed_lines = _changed_lines_for(
            root,
            base_sha=args.base_sha,
            head_sha=args.head_sha,
            file_path=file_path,
        )
        if not changed_lines:
            continue
        changed_lines_by_file[file_path] = changed_lines

    fallback_file = next(iter(changed_lines_by_file), None) if len(changed_lines_by_file) == 1 else None
    violations: list[str] = []
    for item in _ruff_diagnostics(
        root,
        file_paths=list(changed_lines_by_file),
        rules=rules,
        ignored_rules=ignored_rules,
    ):
        location = item.get("location", {})
        if not isinstance(location, dict):
            continue
        file_path = _diagnostic_file_path(root, item.get("filename"), fallback_file=fallback_file)
        changed_lines = changed_lines_by_file.get(file_path)
        if not changed_lines:
            continue
        line = int(location.get("row", 0) or 0)
        if line not in changed_lines:
            continue
        code = item.get("code", "UNKNOWN")
        message = str(item.get("message", "")).strip()
        violations.append(f"{file_path}:{line}: {code} {message}")

    if violations:
        print(f"{args.label}: blocking violations found on changed lines:")
        for violation in violations:
            print(f"  {violation}")
        return 1

    print(f"{args.label}: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
