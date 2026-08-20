"""Guard AI-generated autofix diffs before opening a pull request."""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from pathlib import PurePosixPath


DEFAULT_DENY_PATTERNS = [
    ".github/ai/**",
    ".github/actions/**",
    ".github/workflows/**",
    ".github/CODEOWNERS",
    ".github/dependabot.yml",
    ".github/condarc",
    "scripts/autofix/**",
    "scripts/pack/**",
    "scripts/pack-tauri/**",
    "deploy/**",
    "src/qwenpaw/console/**",
    "src/qwenpaw/security/**",
    "src/qwenpaw/__version__.py",
    "uv.lock",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
]


def git_diff_args(staged: bool, options: list[str]) -> list[str]:
    args = ["diff"]
    if staged:
        args.append("--cached")
    args.extend(options)
    if not staged:
        args.append("HEAD")
    args.append("--")
    return args


def run_git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
    )
    return result.stdout


def normalize_path(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).as_posix()


def matches_any(path: str, patterns: list[str]) -> str | None:
    normalized = normalize_path(path)
    for pattern in patterns:
        if fnmatch.fnmatch(normalized, pattern):
            return pattern
    return None


def changed_files(staged: bool) -> list[str]:
    output = run_git(
        git_diff_args(staged, ["--name-only", "--diff-filter=ACMRTD"]),
    )
    return [normalize_path(line) for line in output.splitlines() if line.strip()]


def deleted_files(staged: bool) -> list[str]:
    output = run_git(git_diff_args(staged, ["--name-only", "--diff-filter=D"]))
    return [normalize_path(line) for line in output.splitlines() if line.strip()]


def numstat(staged: bool) -> tuple[int, int, list[str]]:
    output = run_git(git_diff_args(staged, ["--numstat"]))
    added = 0
    deleted = 0
    binary_files: list[str] = []
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        add_s, del_s, path = parts
        path = normalize_path(path)
        if add_s == "-" or del_s == "-":
            binary_files.append(path)
            continue
        added += int(add_s)
        deleted += int(del_s)
    return added, deleted, binary_files


def build_report(
    files: list[str],
    added: int,
    deleted: int,
    violations: list[str],
    max_files: int,
    max_lines: int,
) -> str:
    status = "failed" if violations else "passed"
    lines = [
        "# AI Autofix Diff Guard",
        "",
        f"Status: {status}",
        f"Changed files: {len(files)} / {max_files}",
        f"Changed lines: {added + deleted} / {max_lines}",
        "",
        "## Files",
    ]
    if files:
        lines.extend(f"- `{path}`" for path in files)
    else:
        lines.append("- No changed files detected.")
    if violations:
        lines.extend(["", "## Violations"])
        lines.extend(f"- {item}" for item in violations)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--max-files", type=int, default=6)
    parser.add_argument("--max-lines", type=int, default=500)
    parser.add_argument("--deny", action="append", default=[])
    parser.add_argument("--report")
    args = parser.parse_args()

    deny_patterns = [*DEFAULT_DENY_PATTERNS, *args.deny]
    files = changed_files(args.staged)
    added, deleted, binary_files = numstat(args.staged)
    removed = deleted_files(args.staged)

    violations: list[str] = []
    if len(files) > args.max_files:
        violations.append(f"too many files changed: {len(files)} > {args.max_files}")
    if added + deleted > args.max_lines:
        violations.append(
            f"too many lines changed: {added + deleted} > {args.max_lines}",
        )
    for path in files:
        pattern = matches_any(path, deny_patterns)
        if pattern:
            violations.append(f"`{path}` is denied by pattern `{pattern}`")
    for path in removed:
        violations.append(f"`{path}` is deleted; deletions require human review")
    for path in binary_files:
        violations.append(f"`{path}` is binary; binary changes are not allowed")

    report = build_report(
        files=files,
        added=added,
        deleted=deleted,
        violations=violations,
        max_files=args.max_files,
        max_lines=args.max_lines,
    )
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(report)
    sys.stdout.write(report)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
