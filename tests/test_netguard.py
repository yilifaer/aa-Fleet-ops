# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The test run cannot reach hosts outside the loopback address."""

import socket
from unittest import TestCase

from testauth import netguard


class NetworkGuardTests(TestCase):
    def setUp(self):
        # runtests.py installs the guard; install it here too so the module also works under other runners.
        was_installed = bool(netguard._original)
        netguard.install()
        if not was_installed:
            self.addCleanup(netguard.uninstall)

    def test_a_remote_host_is_blocked_and_named(self):
        with self.assertRaises(netguard.NetworkBlocked) as caught:
            socket.create_connection(("zkillboard.com", 443), timeout=1)
        self.assertIn("zkillboard.com", str(caught.exception))

    def test_a_remote_address_is_blocked_and_named(self):
        with socket.socket() as sock, self.assertRaises(netguard.NetworkBlocked) as caught:
            sock.connect(("203.0.113.7", 80))
        self.assertIn("203.0.113.7", str(caught.exception))

    def test_loopback_is_allowed(self):
        for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET, "localhost")):
            with self.subTest(host=host), socket.socket(family) as server:
                server.bind(("127.0.0.1", 0))
                server.listen(1)
                with socket.create_connection((host, server.getsockname()[1]), timeout=2):
                    pass

    def test_unix_sockets_are_allowed(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("no Unix sockets")
        with socket.socket(socket.AF_UNIX) as sock:
            with self.assertRaises(OSError):  # refused: nothing listens there, but the guard let it through
                sock.connect("/nonexistent/fleetops-test.sock")

    def test_loopback_names(self):
        for host in ("localhost", "127.0.0.1", "127.1.2.3", "::1", "[::1]"):
            self.assertTrue(netguard.is_loopback(host), host)
        for host in ("zkillboard.com", "10.0.0.1", "esi.evetech.net", "::2"):
            self.assertFalse(netguard.is_loopback(host), host)
