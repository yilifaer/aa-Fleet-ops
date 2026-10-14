# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Keep the test run off the network.

``install()`` makes every connection to a host other than the loopback address fail with ``NetworkBlocked``,
which names the host. Unix sockets and ``localhost``, ``127.0.0.0/8`` and ``::1`` stay open: Redis and MariaDB
are reached that way, locally and in CI. A test that needs another host has to mock the call.

``NetworkBlocked`` is a ``BaseException`` on purpose: the code under test catches ``Exception`` around its
requests, and a blocked call must not be mistaken for a network failure the code is expected to handle.
"""

import ipaddress
import socket

LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


class NetworkBlocked(BaseException):
    """A test tried to reach a host outside the loopback address."""


def is_loopback(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host is None or host == "":
        return True  # the wildcard / local address of a bind or the default of getaddrinfo
    host = str(host).split("%", 1)[0].strip("[]").lower()
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _block(host):
    raise NetworkBlocked(
        f"Test tried to reach {host!r} over the network. Tests may only use localhost; mock the call instead."
    )


_original = {}


def install() -> None:
    """Patch the socket module; calling it twice does nothing."""
    if _original:
        return
    _original.update(
        connect=socket.socket.connect,
        connect_ex=socket.socket.connect_ex,
        getaddrinfo=socket.getaddrinfo,
    )

    def check_address(sock, address):
        if sock.family == getattr(socket, "AF_UNIX", None) or isinstance(address, (str, bytes)):
            return
        if not is_loopback(address[0]):
            _block(address[0])

    def connect(self, address):
        check_address(self, address)
        return _original["connect"](self, address)

    def connect_ex(self, address):
        check_address(self, address)
        return _original["connect_ex"](self, address)

    def getaddrinfo(host, *args, **kwargs):
        if not is_loopback(host):
            _block(host)
        return _original["getaddrinfo"](host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo


def uninstall() -> None:
    if not _original:
        return
    socket.socket.connect = _original["connect"]
    socket.socket.connect_ex = _original["connect_ex"]
    socket.getaddrinfo = _original["getaddrinfo"]
    _original.clear()
