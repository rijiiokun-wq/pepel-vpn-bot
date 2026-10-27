#!/usr/bin/env python3
"""Run pytest with a temporary database, a clean environment and blocked Python sockets.

This is a development test harness, not an OS sandbox. It does not isolate native
code or subprocess networking. The selected application tests mock integrations.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def block_network() -> None:
    original = socket.socket

    class OfflineSocket(original):
        def connect(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                raise RuntimeError("Network access is disabled in offline tests")
            return super().connect(address)

        def connect_ex(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                raise RuntimeError("Network access is disabled in offline tests")
            return super().connect_ex(address)

        def sendto(self, *args, **kwargs):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                raise RuntimeError("Network access is disabled in offline tests")
            return super().sendto(*args, **kwargs)

    def no_dns(*args, **kwargs):
        raise RuntimeError("DNS access is disabled in offline tests")

    socket.socket = OfflineSocket
    socket.getaddrinfo = no_dns
    socket.gethostbyname = no_dns
    socket.gethostbyname_ex = no_dns


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--isolated-child"]:
        if os.environ.get("PEPEL_OFFLINE_CHILD") != "1":
            raise SystemExit("Use the parent offline test command")
        sys.path[:0] = [str(ROOT), str(ROOT / "src")]
        block_network()
        import pytest

        return pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", *args[1:]])

    with tempfile.TemporaryDirectory(prefix="pepel-offline-") as temporary:
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "LANG": "C.UTF-8",
            "NO_COLOR": "true",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "SHOP_DB_FILE": str(Path(temporary) / "test.sqlite3"),
            "BOT_SOURCE": "main",
            "PEPEL_OFFLINE_CHILD": "1",
        }
        completed = subprocess.run(
            [sys.executable, "-B", "-I", str(Path(__file__).resolve()), "--isolated-child", *args],
            cwd=ROOT,
            env=environment,
            check=False,
        )
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
