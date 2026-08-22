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

1.  Add or update unit tests in the `tests` directory.

1.  Run the unit tests for the supported Python versions that are available in
    your environment:

    ```
    nox -s tests*
    ```

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
