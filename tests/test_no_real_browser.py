"""The test suite must never open a real browser window.

``hermes_cli/auth.py`` calls ``webbrowser.open()`` on six OAuth paths and
``tools/mcp_oauth.py`` on a seventh. Only ``tests/tools/test_mcp_oauth.py``
stubs it; any other test reaching those paths launches a real browser. On
macOS that means ``osascript`` and a live "Authorize Grok Build" consent page
in front of whoever is running the suite.

The guard is ``BROWSER=true``, set at import time in ``tests/conftest.py``.
These tests pin the two properties that make it work, because both are easy
to break by accident:

  * it is in the ENVIRONMENT, so a child interpreter inherits it — the runner
    spawns ``python -m pytest <file>`` per file, and a monkeypatch on the
    in-process ``webbrowser`` module cannot cross that boundary;
  * it resolves to a no-op that consumes the URL rather than displaying it.
"""

from __future__ import annotations

import os
import subprocess
import sys


def test_browser_env_is_set_to_a_noop():
    """conftest sets it, and it is a program that does nothing."""
    assert os.environ.get("BROWSER") == "true"


def test_guard_survives_into_a_child_interpreter():
    """The property a monkeypatch could not give us.

    Per-file subprocess isolation means the guard must be inherited, not
    installed in-process. If someone converts this to a fixture that patches
    ``webbrowser.open``, this test fails — which is the point.
    """
    out = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ.get('BROWSER'))"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert out == "true"


def test_child_interpreter_resolves_browser_to_the_noop():
    """``webbrowser`` in a child resolves to the no-op, not a real browser.

    Checks the resolution rather than calling ``.open()``: a regression here
    should fail an assertion, not open a window on the machine running it.
    """
    code = (
        "import webbrowser\n"
        "b = webbrowser.get()\n"
        "print(type(b).__name__)\n"
        "print(getattr(b, 'name', ''))\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    kind, name = res.stdout.strip().splitlines()[:2]

    # BROWSER=true makes webbrowser hand back a GenericBrowser wrapping
    # /usr/bin/true rather than a platform default (MacOSXOSAScript, Chrome...).
    assert kind == "GenericBrowser", f"expected GenericBrowser, got {kind} ({name})"
    assert name == "true", f"expected the no-op 'true', got {name!r}"


def test_protection_does_not_depend_on_the_repo_capability_gate():
    """The guard must hold even though ``_can_open_graphical_browser()`` says True.

    Worth pinning, because the obvious assumption is wrong. That gate refuses
    only *known console browsers* (w3m/lynx/links via ``_CONSOLE_BROWSER_NAMES``);
    ``true`` is not in that list, and on macOS the function returns True at the
    end regardless. So the gate still reports "a window will open".

    Nothing opens anyway, because the call it gates — ``webbrowser.open()`` —
    resolves to the ``true`` no-op (covered above). The protection lives in the
    resolution, not in the gate.

    If someone later teaches the gate to recognise no-op browsers, this test
    should be updated rather than deleted: the guarantee that matters is that no
    window appears, by whichever path.
    """
    from hermes_cli.auth import _can_open_graphical_browser

    gate = _can_open_graphical_browser()
    assert isinstance(gate, bool)

    # The real guarantee, independent of the gate's opinion.
    code = "import webbrowser; print(getattr(webbrowser.get(), 'name', ''))"
    resolved = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert resolved == "true", (
        f"gate said {gate}, and the browser resolved to {resolved!r} rather than "
        "the no-op — a real window could open"
    )
