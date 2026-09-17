"""AUTHOR_MAP entries for the teamnebula-ai fork's own commit authors.

The ``check-attribution`` job in ``.github/workflows/contributor-check.yml``
fails a PR when any commit author email is missing from ``scripts/release.py``.
It does not import the module: it runs ``grep -F '"<email>"'`` over the file,
so an entry only counts if the exact double-quoted email appears in the source.
Its skip rule for GitHub noreply addresses needs a ``<id>+`` prefix, which the
Nebby GitHub App's commit email lacks, so that address needs an entry too.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

RELEASE_PY = Path(__file__).resolve().parents[2] / "scripts" / "release.py"

# The noreply skip rule from contributor-check.yml.
CI_NOREPLY_SKIP = re.compile(r"\+.*@users\.noreply\.github\.com")

FORK_AUTHORS = [
    ("shawn.reddy1@gmail.com", "Screddyice"),
    ("Shawn.reddy1@gmail.com", "Screddyice"),
    ("tm-nebby[bot]@users.noreply.github.com", "tm-nebby[bot]"),
]


def _load_release_module():
    spec = importlib.util.spec_from_file_location(
        "_release_author_map_under_test", RELEASE_PY
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("email", "handle"), FORK_AUTHORS)
def test_fork_author_resolves_to_github_handle(email, handle):
    release = _load_release_module()
    assert release.AUTHOR_MAP[email] == handle
    assert release.resolve_author("ignored", email) == f"@{handle}"


@pytest.mark.parametrize(("email", "_handle"), FORK_AUTHORS)
def test_fork_author_passes_ci_literal_match(email, _handle):
    # CI skips noreply addresses with an id prefix and greps for everything else.
    assert not CI_NOREPLY_SKIP.search(email)
    assert f'"{email}"' in RELEASE_PY.read_text(encoding="utf-8")
