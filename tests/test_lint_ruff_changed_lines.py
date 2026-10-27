from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts import lint_ruff_changed_lines


def test_combined_rules_deduplicates_profiles_and_explicit_rules() -> None:
    rules = lint_ruff_changed_lines._combined_rules(
        profiles=["critical", "best-practices"],
        explicit_rules=["F,E9,S"],
    )

    selectors = rules.split(",")
    assert selectors[:2] == ["F", "E9"]
    assert "S" in selectors
    assert selectors.count("F") == 1
    assert selectors.count("E9") == 1


def test_changed_lines_for_parses_added_ranges(monkeypatch) -> None:
    def fake_run_git(root: Path, args: list[str]) -> str:
        assert root == Path("/repo")
        assert args[:3] == ["diff", "--unified=0", "--no-color"]
        return "\n".join(
            [
                "@@ -1 +1,2 @@",
                "+alpha",
                "+beta",
                "@@ -10,0 +12 @@",
                "+gamma",
                "@@ -20 +20,0 @@",
                "-removed",
            ]
        )

    monkeypatch.setattr(lint_ruff_changed_lines, "_run_git", fake_run_git)

    lines = lint_ruff_changed_lines._changed_lines_for(
        Path("/repo"),
        base_sha="base",
        head_sha="head",
        file_path="sample.py",
    )

    assert lines == {1, 2, 12}


def test_diagnostic_file_path_normalizes_absolute_repo_path() -> None:
    filename = "/repo/src/example.py"

    normalized = lint_ruff_changed_lines._diagnostic_file_path(Path("/repo"), filename)

    assert normalized == "src/example.py"


def test_ruff_diagnostics_checks_all_changed_files_in_one_process(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        assert kwargs["cwd"] == Path("/repo")
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(lint_ruff_changed_lines.subprocess, "run", fake_run)

    diagnostics = lint_ruff_changed_lines._ruff_diagnostics(
        Path("/repo"),
        file_paths=["src/a.py", "src/b.py"],
        rules="F,E9",
        ignored_rules="S101",
    )

    assert diagnostics == []
    assert len(calls) == 1
    assert calls[0][-2:] == ["src/a.py", "src/b.py"]
    assert calls[0][calls[0].index("--ignore") + 1] == "S101"
