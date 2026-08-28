# How to Contribute

Contributions are welcome. There are just a few small guidelines to follow.

## Code reviews

All submissions, including submissions by project members, require review. We
use pull requests for this purpose.

By submitting a pull request you agree that your contribution is licensed
under the same [Apache License 2.0](LICENSE) as the rest of the project.

## Code Style

This library conforms to [PEP 8](https://www.python.org/dev/peps/pep-0008/)
style guidelines and enforces an 80 character line width. It's recommended that
any contributor run the auto-formatter [`black`](https://github.com/psf/black).
To get started, first install `nox` and `black`:

```
pip install -e .[dev]
```

Then run the formatter on all Python files:

```
nox -s format
```

## Test changes

1.  Add or update unit tests in the `tests` directory. Name the file
    `*_test.py`, not `test_*.py` — `unittest discover`'s pattern only
    matches the former, and a misnamed file is silently never collected
    (no error, no warning; it just never ran).

1.  This project's tests import `ads_mcp`, so the package must be
    installed first: either into an editable virtual environment
    (`pip install -e .`, e.g. into `.venv`) for a manual run, or left to
    nox's own `session.install(".")`, which every session below already
    does for you.

1.  Run the unit tests for the supported Python versions that are available in
    your environment:

    ```
    nox -s tests
    ```

    This is one parametrized session per supported Python version
    (`tests-3.10` … `tests-3.13`); `nox -s tests` alone already selects
    all of them by matching the base session name — no glob needed, and
    none is supported: `-s` does exact/parametrized-name matching only,
    so quoting a wildcard (`nox -s "tests*"`) fails with `Sessions not
    found: tests*` instead of matching anything. If you see `tests*`
    (unquoted) recommended elsewhere, it only happens to work by the
    shell glob-expanding it to a path that exists in the current
    directory (here, the `tests/` folder) — don't rely on that
    coincidence, and quote any *_test.py-style pattern you type directly
    against `unittest discover` (see below), since an unquoted, unmatched
    glob makes zsh abort with "no matches found" before the command even
    runs.

    `error_on_missing_interpreters` is on, so a Python version nox can't
    find fails loudly instead of silently skipping: a missing interpreter
    reports `Session tests-3.10 failed: Python interpreter 3.10 not
    found.` per version, and the overall run exits non-zero even when
    every interpreter you do have installed passes. Install the missing
    versions, or fall back to the direct invocation for whichever
    interpreter your environment actually has:

    ```
    .venv/bin/python -m unittest discover -s tests -p "*_test.py"
    ```

1.  `tests/smoke/` has no `__init__.py`, so `unittest discover` silently
    skips the whole smoke package — no failure, just zero smoke tests run
    without saying so. Run it separately:

    ```
    nox -s smoke_tests
    ```

    (or, in an environment with the package already installed:
    `python -m unittest tests.smoke.smoke_test`).

1.  Changing a tool's name, description or schema breaks the smoke
    tests' golden file (`tests/smoke/golden_tools_list.json`) until it is
    regenerated. Do that once, last, after every schema change is in
    place:

    ```
    nox -s update_smoke_golden
    ```

    It pins `GOOGLE_ADS_MCP_TOOLS_CONFIG` to the bundled
    `tools_config.yaml` itself before generating, so an ambient value
    left over in your shell cannot make the goldens depend on your
    machine.

### Test against a local checkout

To test changes by issuing prompts to an agent, point your MCP client at the
server built from your local source files instead of a published package.
Replace `PATH_TO_REPO` with the path where you cloned the repo:

```json
{
  "mcpServers": {
    "google-ads-mcp": {
      "command": "PATH_TO_REPO/.venv/bin/google-ads-mcp"
    }
  }
}
```

If your MCP client offers a debug or verbose mode, enable it so you can see how
prompts are turned into tool calls.

### Test from a branch on your remote

After you push changes, use `pipx` to run the server for a specific branch, and
use the `--no-cache` option so `pipx` gets the latest changes.

Here's an example of an `mcpServers` entry that runs the latest code from a
branch named `awesome-feature-42`. Replace the repository URL with your own:

```json
{
  "mcpServers": {
    "ads-mcp": {
      "command": "pipx",
      "args": [
        "run",
        "--no-cache",
        "--spec",
        "git+https://github.com/YOUR_ORG/google-ads-mcp-extended.git@awesome-feature-42",
        "google-ads-mcp"
      ]
    }
  }
}
```
