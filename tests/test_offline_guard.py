"""The offline guard is a load-bearing claim, so it gets its own tests.

The README promises that the default suite cannot reach the network. These tests
check the mechanism rather than trusting it: an unmarked test is refused at both
the DNS and the connect layer, loopback still works, and a `network` test gets a
real socket back. If any of that stops being true, it fails here.
"""

import socket

import pytest

import conftest


def test_guard_is_installed_for_the_default_suite():
    assert conftest._installed, "pytest_configure never installed the offline guard"
    assert conftest._online["depth"] == 0, "default suite should be offline"


def test_outbound_dns_is_refused():
    with pytest.raises(conftest.OfflineViolation) as excinfo:
        socket.getaddrinfo("api.x402.celo.org", 443)
    assert "not marked `network`" in str(excinfo.value)


def test_outbound_connect_is_refused_even_without_dns():
    # A literal address skips getaddrinfo, so the connect wrapper has to catch it
    # on its own — otherwise an IP-literal call would slip through unmarked.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        with pytest.raises(conftest.OfflineViolation):
            sock.connect(("192.0.2.1", 80))
    finally:
        sock.close()


def test_loopback_and_local_bind_stay_legal():
    socket.getaddrinfo("localhost", 0)
    socket.getaddrinfo(None, 0)
    socket.getaddrinfo("127.0.0.1", 0)


def test_loopback_connection_still_works():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=2)
        client.close()
    finally:
        server.close()


def test_guard_reasserts_itself_after_something_restores_the_real_connect():
    # Anything that saves and restores socket.socket.connect around a test (the
    # pytest-socket plugin's teardown did) disarms the guard halfway through a
    # run. The next test setup has to put it back.
    real = conftest._installed["connect"]
    socket.socket.connect = real
    try:
        conftest._install_offline_guard()
        assert socket.socket.connect is conftest._installed["wrapper_connect"]
        with pytest.raises(conftest.OfflineViolation):
            socket.socket().connect(("192.0.2.1", 80))
    finally:
        conftest._install_offline_guard()


def test_network_marker_lifts_the_guard(monkeypatch):
    # `-m network` runs are deliberately online. Prove the lift without needing a
    # working internet connection: a TEST-NET address is unroutable, so the call
    # fails at the OS level, and what matters is that it is *not* our guard.
    monkeypatch.setitem(conftest._online, "depth", 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        with pytest.raises(OSError) as excinfo:
            sock.connect(("192.0.2.1", 80))
        assert not isinstance(excinfo.value, conftest.OfflineViolation)
    finally:
        sock.close()
