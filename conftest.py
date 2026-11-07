"""Suite-wide wiring: repo-root imports, an offline guard, and test isolation.

A plain `pytest` has to pass on a clean checkout with no network, no `.env` and
no API keys — that is how a reviewer's sandbox runs it: the *runtime*
requirements plus whatever pytest the machine already has. So the offline
guarantee is implemented here, in the repo, rather than as a pytest.ini flag
that only exists when an optional plugin is installed. `addopts =
--disable-socket` did exactly that, and wherever `pytest-socket` was missing
pytest refused to start at all — the whole run errored with `unrecognized
arguments`, which reads as "the tests fail".
"""

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Loopback and bind-all stay legal: the public HTTP surface is exercised over a
# real socket on 127.0.0.1, and binding a port is not "reaching the network".
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}

# How many `network`-marked frames are currently open (0 = suite is offline).
_online = {"depth": 0}
_installed = {}


class OfflineViolation(RuntimeError):
    """A test tried to reach the network without being marked `network`."""


def _describe(address):
    host = address[0] if isinstance(address, tuple) and address else address
    return repr(host)


def _install_offline_guard():
    """Block outbound connections unless a `network` test has lifted the guard.

    Stdlib only, so the guarantee does not depend on which dev requirements a
    sandbox happens to install. The real functions are captured once and the
    wrappers are re-asserted before every test: anything that saves and restores
    `socket.socket.connect` around a test (pytest-socket's teardown does exactly
    that) would otherwise disarm the guard halfway through the suite.
    """
    if not _installed:
        _installed["connect"] = socket.socket.connect
        _installed["connect_ex"] = socket.socket.connect_ex
        _installed["getaddrinfo"] = socket.getaddrinfo

    if "wrapper_connect" not in _installed:
        _installed["wrapper_connect"] = _make_connect(_installed["connect"])
        _installed["wrapper_connect_ex"] = _make_connect_ex(_installed["connect_ex"])
        _installed["wrapper_getaddrinfo"] = _make_getaddrinfo(_installed["getaddrinfo"])

    socket.socket.connect = _installed["wrapper_connect"]
    socket.socket.connect_ex = _installed["wrapper_connect_ex"]
    socket.getaddrinfo = _installed["wrapper_getaddrinfo"]


def _check_allowed(sock, address):
    if _online["depth"] > 0:
        return
    if getattr(sock, "family", None) == getattr(socket, "AF_UNIX", object()):
        return  # local IPC, not the network
    if _describe(address).strip("'") in _ALLOWED_HOSTS:
        return
    raise OfflineViolation(
        f"blocked outbound connection to {_describe(address)}: this test is not "
        "marked `network`. Mark it and run `pytest -m network`, or stub the call."
    )


def _make_connect(real):
    def connect(self, address, *args, **kwargs):
        _check_allowed(self, address)
        return real(self, address, *args, **kwargs)

    return connect


def _make_connect_ex(real):
    def connect_ex(self, address, *args, **kwargs):
        _check_allowed(self, address)
        return real(self, address, *args, **kwargs)

    return connect_ex


def _make_getaddrinfo(real):
    def getaddrinfo(host, *args, **kwargs):
        # host=None is a local bind; a hostname has to resolve before it can be
        # dialled, so refusing here stops the lookup itself from hanging.
        if _online["depth"] == 0 and host is not None and str(host) not in _ALLOWED_HOSTS:
            raise OfflineViolation(
                f"blocked DNS lookup for {host!r}: this test is not marked `network`."
            )
        return real(host, *args, **kwargs)

    return getaddrinfo


def _uninstall_offline_guard():
    """Put the real socket functions back (used by this module's own tests)."""
    if _installed:
        socket.socket.connect = _installed["connect"]
        socket.socket.connect_ex = _installed["connect_ex"]
        socket.getaddrinfo = _installed["getaddrinfo"]
    _installed.clear()


def pytest_configure(config):
    _install_offline_guard()


def pytest_runtest_setup(item):
    # Re-assert per test: see _install_offline_guard.
    _install_offline_guard()


@pytest.fixture(autouse=True)
def _network_marker_opens_the_guard(request):
    """`-m network` runs are deliberately online.

    A test that asked for the network gets its sockets back for its own
    duration; everything else stays offline, so an accidental outbound call
    fails here instead of passing on a laptop with wifi and hanging in a
    stranger's sandbox.
    """
    if request.node.get_closest_marker("network"):
        _online["depth"] += 1
        try:
            yield
        finally:
            _online["depth"] -= 1
    else:
        yield


@pytest.fixture(autouse=True)
def _claims_snapshot_goes_to_tmp(tmp_path, monkeypatch):
    """A test that settles an invoice must not rewrite the committed evidence.

    store.mark_settled refreshes claims.json from whatever store it just wrote,
    so a test with a temporary store would otherwise truncate the real snapshot
    to its own fixture invoices — the audit would then verify a file no human
    meant to publish.
    """
    monkeypatch.setenv("CLAIMS_PATH", str(tmp_path / "claims.json"))
    yield
