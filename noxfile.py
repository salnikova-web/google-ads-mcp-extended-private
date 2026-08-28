# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import glob
import json
import os
import pathlib
import sys

import nox

PYTHON_VERSIONS = ["3.10", "3.11", "3.12", "3.13"]

# Honesty over green: a missing interpreter must fail the session instead of
# being skipped. Without this, `nox -s tests` on a machine that has none of
# PYTHON_VERSIONS installed reports success having run zero tests.
nox.options.error_on_missing_interpreters = True

# The real clone that the live Claude Desktop server is installed from.
# `nox -s deploy` must be invoked from THIS clone on branch main:
# `_deploy_guard` checks the invoking checkout's OWN current branch, and a
# worktree's current branch is its own worktree branch, never literally
# "main", so running this session from a worktree fails the guard before
# any format/test/install step runs (fail-safe, not a supported path). The
# pipx install and the commit verification below are always anchored at
# this path; override it for local testing via GOOGLE_ADS_MCP_DEPLOY_REPO.
DEPLOY_SOURCE_REPO = os.environ.get(
    "GOOGLE_ADS_MCP_DEPLOY_REPO",
    "/Users/user/Documents/Develop/google-ads-mcp-extended",
)
DEPLOY_PACKAGE_SPEC = f"google-ads-mcp @ git+file://{DEPLOY_SOURCE_REPO}@main"

REPO_ROOT = pathlib.Path(__file__).parent.resolve()
VENV_BIN = REPO_ROOT / ".venv" / "bin"
VENV_PYTHON = VENV_BIN / "python"
VENV_BLACK = VENV_BIN / "black"

TEST_COMMAND = [
    "coverage",
    "run",
    "--append",
    "-m",
    "unittest",
    "discover",
    "--buffer",
    "-s=tests",
    "-p",
    "*_test.py",
]

FREEZE_COMMAND = [sys.executable, "-m", "pip", "freeze"]
TEST_DEPENDENCIES = [
    "pyfakefs>=5.0.0,<6.0",
    "coverage>=7.6",
]

# Pinned so `nox -s deploy` gives the same black verdict on every machine
# instead of whatever the newest black happens to be that day. Chosen by
# installing the newest stable black into .venv and confirming
# `black --check -l 80 .` passes on the already-formatted tree with no
# reformat needed.
FORMAT_DEPENDENCIES = [
    "black==26.5.1",
]

BLACK_EXCLUDE = (
    r"/(v[0-9]+|\.eggs|\.git|_cache|\.nox|\.tox|\.venv|env|venv"
    r"|\.svn|_build|buck-out|build|dist)/"
)


def _black_args(check):
    """Builds the black CLI args shared by `_format` and `deploy`.

    Args:
      check: If True, adds --check so black reports without rewriting.
    """
    args = []
    if check:
        args.append("--check")
    args.extend(["-l", "80", "--exclude", BLACK_EXCLUDE, "."])
    return args


def _format(session, check=False):
    """Helper function to run formatters.

    Args:
      session: The nox session object.
      check: If True, checks formatting and fails if any file requires
        formatting, but doesn't apply any changes. If False, applies formatting
        fixes.
    """
    session.run("black", *_black_args(check))


@nox.session(venv_backend="none")
def lint(session):
    """Fails if the code is not formatted correctly."""
    _format(session, check=True)


@nox.session(venv_backend="none")
def format(session):
    """Runs the black formatter and applies formatting fixes."""
    _format(session)


# `tests` runs once per entry in PYTHON_VERSIONS, and TEST_COMMAND uses
# `coverage run --append` so every entry's data accumulates into one shared
# .coverage file. Without an erase step that file also accumulates *across*
# separate `nox -s tests` invocations, forever. This flag makes sure erase
# happens exactly once per invocation (nox imports this module once and
# runs all matrix entries in that same process) rather than once per entry,
# which would just erase the previous entry's coverage.
_coverage_erased = False


def _erase_coverage_once(session):
    """Erases stale coverage data the first time it's called this run."""
    global _coverage_erased
    if not _coverage_erased:
        session.run("coverage", "erase")
        _coverage_erased = True


@nox.session(python=PYTHON_VERSIONS)
def tests(session):
    session.install(".")
    # modules for testing
    session.install(*TEST_DEPENDENCIES)
    session.run(*FREEZE_COMMAND)
    _erase_coverage_once(session)
    session.run(
        *TEST_COMMAND,
    )


@nox.session(python="3.12")
def coverage_report(session):
    """Prints the combined report for the `tests` matrix's coverage data.

    Run after `nox -s tests`: that session's `coverage run --append` steps
    accumulate into one shared .coverage file that nothing else reports on.
    """
    session.install("coverage>=7.6")
    session.run("coverage", "report")


@nox.session(python="3.12")
def smoke_tests(session):
    """Runs the smoke tests."""
    session.install(".")
    session.run("python", "-m", "unittest", "tests.smoke.smoke_test")


@nox.session(python="3.12")
def llm_tests(session):
    """Runs the LLM tool selection smoke tests."""
    session.install(".", "google-genai")
    session.run("python", "-m", "unittest", "tests.smoke.llm_test")


@nox.session(python="3.12")
def update_smoke_golden(session):
    """Updates the smoke test golden file."""
    session.install(".")
    session.run("python", "-m", "tests.smoke.generate_golden")


@nox.session(python="3.12")
def token_usage(session):
    """Compares live LLM token usage against tests/smoke/llm_cases.json.

    Talks to the real Gemini API. Skips cleanly (exit 0) instead of
    failing the session when GEMINI_API_KEY is unset.
    """
    session.install(".", "google-genai")
    session.run("python", "-m", "tests.smoke.token_usage_check")


def _deploy_guard(session):
    """Refuses to deploy an uncommitted or non-main tree.

    Deploying whatever happens to be on disk instead of committed main is
    the historical failure mode this gate exists to stop.
    """
    status = session.run("git", "status", "--porcelain", silent=True)
    if status.strip():
        session.error(
            "Refusing to deploy: working tree is dirty. Commit or stash "
            "before running `nox -s deploy`.\n"
            f"git status --porcelain:\n{status}"
        )

    branch = session.run("git", "branch", "--show-current", silent=True).strip()
    if branch != "main":
        session.error(
            "Refusing to deploy: current branch is "
            f"{branch!r}, not 'main'. Deploy only ships committed main."
        )


def _deploy_format_check(session):
    """Pins and runs black in check mode using the repo's own .venv.

    nox's own `tests` sessions silently skip on this machine (only Python
    3.14 is on PATH, and nox targets 3.10-3.13), which is why every step of
    `deploy` calls .venv binaries directly instead of going through nox's
    session-managed interpreters.
    """
    session.run(str(VENV_PYTHON), "-m", "pip", "install", *FORMAT_DEPENDENCIES)
    session.run(str(VENV_BLACK), *_black_args(check=True))


def _deploy_tests(session):
    """Runs the full suite plus the smoke suite via the repo's own .venv."""
    session.run(
        str(VENV_PYTHON),
        "-m",
        "unittest",
        "discover",
        "--buffer",
        "-s",
        "tests",
        "-p",
        "*_test.py",
    )
    session.run(str(VENV_PYTHON), "-m", "unittest", "tests.smoke.smoke_test")


def _deploy_install(session):
    """Installs the live server from committed main on the real clone."""
    session.run("pipx", "install", "--force", DEPLOY_PACKAGE_SPEC)


def _find_direct_url_json():
    """Locates pipx's direct_url.json for google-ads-mcp.

    pipx's home varies on macOS, so both known locations are tried; whichever
    actually exists is used.
    """
    patterns = [
        "~/.local/pipx/venvs/google-ads-mcp/lib/python*/site-packages/"
        "google_ads_mcp-*.dist-info/direct_url.json",
        "~/Library/Application Support/pipx/venvs/google-ads-mcp/lib/"
        "python*/site-packages/google_ads_mcp-*.dist-info/direct_url.json",
    ]
    matches = []
    for pattern in patterns:
        matches.extend(glob.glob(os.path.expanduser(pattern)))
    return matches


def _deploy_verify_commit(session):
    """Asserts the pipx install actually picked up committed main's HEAD."""
    matches = _find_direct_url_json()
    if not matches:
        session.error(
            "Could not find direct_url.json for the installed google-ads-mcp "
            "pipx package (checked ~/.local/pipx and "
            "~/Library/Application Support/pipx)."
        )

    with open(matches[0], encoding="utf-8") as direct_url_file:
        direct_url = json.load(direct_url_file)
    installed_commit = direct_url.get("vcs_info", {}).get("commit_id")

    expected_commit = session.run(
        "git",
        "-C",
        DEPLOY_SOURCE_REPO,
        "rev-parse",
        "HEAD",
        silent=True,
    ).strip()

    if installed_commit != expected_commit:
        session.error(
            "Deploy verification failed: installed package commit_id "
            f"{installed_commit!r} does not match main HEAD "
            f"{expected_commit!r} ({matches[0]})."
        )
    session.log(
        "Verified: installed commit_id matches main HEAD "
        f"({expected_commit})."
    )


@nox.session(venv_backend="none")
def deploy(session):
    """Local deploy gate for the live pipx-installed server.

    GitHub Actions cannot gate a git+file:// install from this machine's
    local clone, so this session is the CI that matters: guard against a
    dirty or non-main tree, check formatting, run the full test suite, then
    install and verify the live server before reminding you to restart
    Claude Desktop.
    """
    _deploy_guard(session)
    _deploy_format_check(session)
    _deploy_tests(session)
    _deploy_install(session)
    _deploy_verify_commit(session)
    session.log(
        "Restart Claude Desktop (⌘Q) — the client keeps the old "
        "server in memory."
    )
