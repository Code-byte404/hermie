"""Mac toolchain for the executor: screenshots of the iOS simulator or of the Mac screen.

The Xcode toolchain itself (xcodebuild, swift, xcrun simctl, AXe) already runs inside the sandbox through run_command;
this module covers the one thing the sandbox cannot do: capturing an image. Like the web tools it runs in the
controller process with a fixed argv, so the executor's shell keeps its deny rules (screencapture, window server)
and this is the only, logged path to the screen. The full-size PNG goes to data_dir/screenshots (never the
workspace, so it does not show up in diffs or snapshots); a downscaled copy is what the local vision model sees.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import struct
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .config import Settings

Runner = Callable[[list[str]], Awaitable[tuple[int, str]]]

TARGETS = ("simulator", "mac")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# "booted", a simulator name ("iPhone 17 Pro", "iPad (10th generation)") or a UDID; nothing shell-like gets through
_DEVICE_RE = re.compile(r"^[A-Za-z0-9 .()-]{1,64}$")
_SIPS = "/usr/bin/sips"


@lru_cache(maxsize=1)
def xcode_available() -> bool:
    return shutil.which("xcodebuild") is not None


async def run_argv(argv: list[str], timeout: float = 60) -> tuple[int, str]:
    """Run a fixed argv (no shell) in the controller process; returns (exit code, stderr + stdout)."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "timed out"
    return proc.returncode if proc.returncode is not None else -1, (err + out).decode("utf-8", "replace")


def png_size(data: bytes) -> tuple[int, int]:
    if data[:8] != PNG_SIGNATURE or len(data) < 24:
        raise ValueError("not a PNG file")
    w, h = struct.unpack(">II", data[16:24])
    return w, h


@dataclass
class Shot:
    path: Path      # full-size PNG on disk
    data: bytes     # downscaled PNG for the model
    width: int
    height: int


class ScreenCapture:
    """Takes screenshots outside the sandbox. `run` is injectable so tests need no simulator or display."""

    def __init__(self, settings: Settings, *, run: Optional[Runner] = None):
        self.s = settings
        self.run = run or run_argv
        self.dir = settings.data_dir / "screenshots"
        self._n = 0

    def argv(self, target: str, device: str, out: Path) -> list[str]:
        if target == "mac":
            return ["/usr/sbin/screencapture", "-x", str(out)]
        if target == "simulator":
            if not _DEVICE_RE.match(device or ""):
                raise ValueError('device must be "booted", a simulator name or a UDID')
            # simctl resolves relative paths against the CoreSimulator service, not the caller: always absolute
            return ["/usr/bin/xcrun", "simctl", "io", device, "screenshot", str(out)]
        raise ValueError(f"unknown screenshot target {target!r}; use one of {TARGETS}")

    async def capture(self, target: str, device: str = "booted") -> Shot:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._n += 1
        out = self.dir / f"{target}-{time.strftime('%Y%m%d-%H%M%S')}-{self._n}.png"
        argv = self.argv(target, device, out)
        code, err = await self.run(argv)
        if code != 0 or not out.exists():
            raise RuntimeError(err.strip()[-500:] or f"capture command exited with {code}")
        data = await self._downscale(out)
        w, h = png_size(data)
        return Shot(out, data, w, h)

    async def _downscale(self, src: Path) -> bytes:
        small = src.with_name(src.stem + ".small.png")
        try:
            code, _ = await run_argv([_SIPS, "-Z", str(self.s.screenshot_max_px), str(src), "--out", str(small)])
            if code == 0 and small.exists():
                return small.read_bytes()
            return src.read_bytes()
        finally:
            small.unlink(missing_ok=True)
