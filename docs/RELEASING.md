# Releasing

The package publishes to PyPI from a git tag, through GitHub Actions and
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC), so no
API token is stored anywhere.

## One-time setup

1. **PyPI** — as the owner of the package name, add a *pending publisher* at
   <https://pypi.org/manage/account/publishing/>:

   | field | value |
   | --- | --- |
   | PyPI project name | `powerdesigner-mcp` |
   | Owner | `NovolineGast` |
   | Repository name | `powerdesigner-mcp` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   (A pending publisher is enough for the first release; afterwards the
   publisher is listed under the project's *Publishing* settings.)

2. **GitHub** — optionally create an environment named `pypi` under
   *Settings → Environments* and add reviewers, so a release pauses for
   approval before publishing.

## Cutting a release

```powershell
# 1. bump the single source of truth
#    (pyproject.toml has no static version - it reads pd_mcp.__version__)
notepad src\pd_mcp\__init__.py        # __version__ = "0.2.0"

# 2. commit and push
git add -A
git commit -m "release: 0.2.0"
git push

# 3. tag and push the tag - this starts the release workflow
git tag v0.2.0
git push origin v0.2.0
```

The workflow refuses to continue when the tag and `pd_mcp.__version__` disagree,
so a mismatch fails the build instead of publishing the wrong version.

## What the workflow does

| job | purpose |
| --- | --- |
| `build` | tag/version guard, `uv build`, `twine check --strict` |
| `publish` | uploads to PyPI via Trusted Publishing |
| `github-release` | creates a GitHub Release with the artifacts and generated notes |

Running the workflow manually (`workflow_dispatch`) builds and validates
without publishing.

## Verifying a release

```powershell
uvx powerdesigner-mcp version                 # resolves from PyPI
uvx powerdesigner-mcp install --dry-run       # what a one-line install writes
```

## Manual publish (no CI)

```powershell
uv build
uvx twine check --strict dist/*
uvx twine upload dist/*        # prompts for an API token, or set TWINE_PASSWORD
```

Use a per-project API token from <https://pypi.org/manage/account/token/>, and
never commit it.

## If a release is wrong

PyPI does not allow re-uploading a version. **Yank** the bad files in the PyPI
web UI (the version stays resolvable for existing pins but is hidden from new
installs), then publish a patch release. Deleting a version is only possible for
a few hours after upload.

## Troubleshooting

- **`uv tool install git+https://…` fails with no output** — some networks
  (TLS-inspecting proxies) break uv's git transport while the `git` CLI keeps
  working. Build and install from a checkout instead:
  `git clone … && uv build && uv tool install ./dist/*.whl`.
- **`twine check` complains about the README** — that is usually a relative link
  or a missing long description; `tests/test_packaging.py` guards both, so run
  the suite before tagging.
- **The workflow fails at "Tag must match the package version"** — bump
  `__version__` in `src/pd_mcp/__init__.py`, commit, then re-tag (delete the tag
  locally and on the remote first: `git push origin :refs/tags/vX.Y.Z`).
