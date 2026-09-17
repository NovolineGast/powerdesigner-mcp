"""Packaging invariants - the things a bad edit would only reveal after a release.

These are cheap checks against the failure modes that hurt most when publishing:
a version that drifts between the source and the metadata, metadata that loses
the fields PyPI needs, and README links that work on GitHub but break on PyPI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

PROJECT = Path(__file__).resolve().parents[1]
PYPROJECT = PROJECT / "pyproject.toml"

# markdown links that would break when PyPI renders the README standalone
_RELATIVE_LINK = re.compile(r"\]\((?!https?://|#|mailto:)([^)]+)\)")


@pytest.fixture(scope="module")
def project_meta() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]


def test_version_has_a_single_source(project_meta):
    assert project_meta["dynamic"] == ["version"]
    assert "version" not in project_meta, "a static version would shadow the dynamic one"
    dyn = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["setuptools"]["dynamic"]
    assert dyn["version"] == {"attr": "pd_mcp.__version__"}


def test_runtime_version_matches_the_module():
    from pd_mcp import __version__
    assert re.fullmatch(r"\d+\.\d+\.\d+([.-][0-9A-Za-z.]+)?", __version__), __version__


def test_metadata_pypi_needs(project_meta):
    assert project_meta["name"] == "powerdesigner-mcp"
    assert project_meta["readme"] == "README.md"
    assert project_meta["requires-python"] == ">=3.10"
    assert project_meta["license"] == "MIT"
    assert project_meta["license-files"] == ["LICENSE"]
    assert project_meta["description"].strip()
    assert project_meta["keywords"]
    assert len(project_meta["classifiers"]) >= 8
    assert "Operating System :: Microsoft :: Windows" in project_meta["classifiers"]
    for key in ("Homepage", "Repository", "Issues"):
        assert project_meta["urls"][key].startswith("https://")


def test_dependencies_keep_the_platform_marker(project_meta):
    deps = project_meta["dependencies"]
    assert any(d.startswith("mcp") for d in deps)
    # pywin32 must stay Windows-only so the package can be built and imported
    # (mock backend) elsewhere
    assert any("pywin32" in d and "sys_platform == 'win32'" in d for d in deps)


def test_console_script_is_declared(project_meta):
    assert project_meta["scripts"]["powerdesigner-mcp"] == "pd_mcp.__main__:main"


def test_readme_links_survive_pypi():
    text = (PROJECT / "README.md").read_text(encoding="utf-8")
    offending = [m.group(1) for m in _RELATIVE_LINK.finditer(text)]
    assert not offending, (
        "relative links do not resolve on PyPI - make them absolute: "
        f"{offending[:5]}")


def test_license_file_exists(project_meta):
    for name in project_meta["license-files"]:
        assert (PROJECT / name).is_file(), name
