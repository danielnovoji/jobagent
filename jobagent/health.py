"""Is the agent alive, and is the laptop on its charger? Read locally, costs nothing.

Docker mounts the host's /sys read-only, so the container sees the laptop's
power supply (checked on Daniel's Latitude 7440, 2026-11-11).
"""
from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

POWER = Path("/sys/class/power_supply")


def _read(name: str) -> str | None:
    try:
        return (POWER / name).read_text().strip()
    except OSError:
        return None


def power_state() -> tuple[bool | None, int | None]:
    """(on the charger?, battery %). None where the machine doesn't say."""
    ac, cap = _read("AC/online"), _read("BAT0/capacity")
    return (None if ac is None else ac == "1", int(cap) if cap and cap.isdigit() else None)


def power_line() -> str:
    on_ac, cap = power_state()
    pct = f" {cap}%" if cap is not None else ""
    if on_ac:
        return f"🔌 On the charger, battery{pct}"
    if on_ac is False:
        return f"🔋 On battery{pct}: the laptop sleeps when idle or folded, and the agent with it"
    return "Power: unknown"


def duration(span: timedelta) -> str:
    """"2d 3h", "3h 12m", "7m"."""
    minutes = max(0, int(span.total_seconds() // 60))
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {mins}m" if hours else f"{mins}m"


def disk_free_gb(path) -> float:
    return shutil.disk_usage(path).free / 1e9
