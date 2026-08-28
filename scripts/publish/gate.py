#!/usr/bin/env python3
"""Neutrality gate for the public export tree.

Scans an EXPORTED tree (never the private repo itself) and fails loudly
on any trace of private or company content: Cyrillic characters
(U+0400-U+04FF), denylisted terms from scripts/publish/denylist.txt
(case-insensitive, matched in file contents and file paths), and any
absolute /Users/ path other than the documented /Users/USERNAME
placeholder used by README-EXTENDED.md's Claude Desktop config example.

This file and denylist.txt live in scripts/publish/, which is marked
export-ignore in .gitattributes and excluded by .dockerignore: neither
ever leaves the private repository.

Usage:
    .venv/bin/python scripts/publish/gate.py <export-dir>
    .venv/bin/python scripts/publish/gate.py --selftest

Exit codes: 0 clean, 1 violations found, 2 structural/setup failure.
"""

import argparse
import pathlib
import re
import sys

GATE_DIR = pathlib.Path(__file__).parent.resolve()
DENYLIST_PATH = GATE_DIR / "denylist.txt"

# The full Cyrillic block U+0400-U+04FF.
CYRILLIC_RE = re.compile("[Ѐ-ӿ]")

# Any /Users/ path except the documented /Users/USERNAME placeholder.
USERS_RE = re.compile(r"/Users/(?!USERNAME\b)")

# Must NOT exist in an export (structural leak check).
FORBIDDEN = ["CLAUDE.md", ".claude", ".gitattributes", "scripts"]

# Must exist in an export; guards against vacuously passing on an
# empty or wrong directory.
REQUIRED = [
    "ads_mcp",
    "tests",
    "pyproject.toml",
    "LICENSE",
    "README.md",
    "noxfile.py",
]

SKIP_DIRS = {".git"}


def load_denylist():
    """Returns the denylist terms lowercased, failing if none exist."""
    terms = []
    for raw in DENYLIST_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            terms.append(line.lower())
    if not terms:
        print(f"gate: denylist is empty: {DENYLIST_PATH}", file=sys.stderr)
        sys.exit(2)
    return terms


def scan_line(line, terms):
    """Returns the violation reasons found in one line of text."""
    reasons = []
    lowered = line.lower()
    for term in terms:
        if term in lowered:
            reasons.append(f"denylisted term {term!r}")
    if CYRILLIC_RE.search(line):
        reasons.append("Cyrillic character")
    if USERS_RE.search(line):
        reasons.append("absolute /Users/ path (not /Users/USERNAME)")
    return reasons


def check_structure(root):
    """Fails fast when root is not a plausible clean export."""
    errors = []
    for name in FORBIDDEN:
        if (root / name).exists():
            errors.append(f"forbidden path present: {name}")
    for name in REQUIRED:
        if not (root / name).exists():
            errors.append(f"required path missing: {name}")
    return errors


def scan_tree(root, terms):
    """Scans all paths and file contents under root.

    Returns:
      A (violations, scanned_file_count) tuple.
    """
    violations = []
    scanned = 0
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        for reason in scan_line(str(rel), terms):
            violations.append(f"{rel} (path): {reason}")
        if not path.is_file():
            continue
        scanned += 1
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            violations.append(f"{rel}: not valid UTF-8 (manual review)")
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for reason in scan_line(line, terms):
                violations.append(f"{rel}:{lineno}: {reason}")
    return violations, scanned


def selftest(terms):
    """Proves the detectors actually detect; returns True on success."""
    cyrillic_sample = "Стан"  # "Stan" in Cyrillic.
    checks = [
        (f"prefix {terms[0].upper()} suffix", True),
        (cyrillic_sample, True),
        ("/Users/janedoe/.config", True),
        ("/Users/USERNAME/.config/gcloud", False),
        ("a perfectly neutral line", False),
    ]
    ok = True
    for sample, expect_hit in checks:
        hit = bool(scan_line(sample, terms))
        if hit != expect_hit:
            print(f"gate: selftest FAILED for {sample!r}", file=sys.stderr)
            ok = False
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_dir", nargs="?", help="tree to scan")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="verify the detectors against known-bad samples",
    )
    args = parser.parse_args()

    terms = load_denylist()

    if args.selftest:
        if selftest(terms):
            print("gate: selftest passed")
            return 0
        return 2

    if not args.export_dir:
        parser.error("export_dir is required unless --selftest is given")
    root = pathlib.Path(args.export_dir).resolve()
    if not root.is_dir():
        print(f"gate: not a directory: {root}", file=sys.stderr)
        return 2

    structural = check_structure(root)
    if structural:
        for error in structural:
            print(f"gate: STRUCTURE: {error}", file=sys.stderr)
        return 2

    violations, scanned = scan_tree(root, terms)
    if violations:
        for violation in violations:
            print(f"gate: LEAK: {violation}", file=sys.stderr)
        print(
            f"gate: FAILED with {len(violations)} violation(s) in {root}",
            file=sys.stderr,
        )
        return 1

    print(f"gate: PASSED - scanned {scanned} files in {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
