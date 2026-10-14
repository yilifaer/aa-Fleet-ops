# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Read the scripts that FleetOps pages load, for tests that check what a script does."""

from pathlib import Path

import fleetops

STATIC_DIR = Path(fleetops.__file__).resolve().parent / "static" / "fleetops"


def static_source(name: str) -> str:
    """The text of ``fleetops/static/fleetops/<name>``."""
    return (STATIC_DIR / name).read_text(encoding="utf-8")
