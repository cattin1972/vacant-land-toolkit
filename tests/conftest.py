"""
Shared pytest setup for the tests/ suite. Kept deliberately separate from
the 16 legacy test_*.py scripts at the repo root (each of those does its
own sys.path.insert and runs as a plain script, not through pytest -- see
TESTING.md for why both exist side by side rather than a risky mass
migration of already-verified assertions).
"""
import os
import socket
import subprocess
import sys
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBAPP_DIR = os.path.join(REPO_ROOT, "webapp")
for _p in (REPO_ROOT, WEBAPP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import vacant_land_search as v  # noqa: E402


@pytest.fixture(scope="session")
def flask_app_module():
    """The webapp/app.py module, imported once per test session."""
    import app as flask_app_module  # noqa: E402
    return flask_app_module


@pytest.fixture
def client(flask_app_module):
    """A werkzeug test client -- exercises real Flask routes with zero
    real network calls, fast, no subprocess. Use this for anything that
    doesn't need actual client-side JavaScript to run (most backend
    contract tests)."""
    return flask_app_module.app.test_client()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server_url():
    """
    Starts the REAL Flask app as a subprocess on a free local port, for
    tests that need actual client-side JavaScript to execute (Playwright)
    -- the werkzeug test client above only exercises server-side routes,
    never runs a single line of index.html's JS.

    Session-scoped: one server for the whole pytest run, torn down at the
    end. FLASK_DEBUG is left unset (defaults to false -- see app.py's own
    security note) so this is a single process with no reloader subprocess,
    safe to .terminate() directly.
    """
    import requests

    port = _free_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(WEBAPP_DIR, "app.py")],
        cwd=WEBAPP_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}/"
    try:
        for _ in range(100):
            try:
                if requests.get(url, timeout=1).status_code == 200:
                    break
            except requests.exceptions.RequestException:
                pass
            time.sleep(0.1)
        else:
            proc.terminate()
            raise RuntimeError("Live test server never became ready")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
