"""Performance sampling: CPU / GPU / unified memory utilization, drawn by the UI's "Performance" tab.

- CPU and memory come from psutil;
- GPU comes from the PerformanceStatistics of `ioreg -c IOAccelerator` (no root needed on Apple Silicon;
  `powermetrics` would need sudo, so we do not use it). Recorded as None when unavailable and the UI
  shows "unavailable".

The sampler makes no network or model calls, runs only inside the UI, and the core never references it.
"""
from __future__ import annotations

import re
import subprocess
from collections import deque
from dataclasses import dataclass
from typing import Optional

import psutil

_GPU_UTIL = re.compile(r'"Device Utilization %"=(\d+)')
_GPU_MEM = re.compile(r'"In use system memory"=(\d+)')
_IOREG_CMD = ["/usr/sbin/ioreg", "-r", "-d", "1", "-c", "IOAccelerator"]


@dataclass(frozen=True)
class PerfSample:
    cpu: float                      # 0-100, average over all cores
    gpu: Optional[float]            # 0-100, None when unavailable
    mem_used: int                   # bytes
    mem_total: int
    gpu_mem: Optional[int] = None   # unified memory currently held by the GPU (local model weights mostly live here)


def parse_ioreg(text: str) -> tuple[Optional[float], Optional[int]]:
    """Extract GPU utilization and GPU memory in use from ioreg output; missing fields return None."""
    util = _GPU_UTIL.search(text)
    mem = _GPU_MEM.search(text)
    return (float(util.group(1)) if util else None), (int(mem.group(1)) if mem else None)


_BLOCKS = " ▁▂▃▄▅▆▇█"


def render_graph(values, width: int, height: int, vmax: float = 100.0) -> list[str]:
    """Draw a series of 0-vmax samples as a bar history graph of height rows by width columns
    (newest frame at the far right, older ones scroll left).

    One frame per column, 8 levels of resolution per row; returns the rows top to bottom, each of length width.
    """
    vals = list(values)[-width:] if width > 0 else []
    levels = height * 8
    cols = [max(0, min(levels, round(v / vmax * levels))) if vmax > 0 else 0 for v in vals]
    pad = " " * (width - len(cols))
    rows = []
    for row in range(height - 1, -1, -1):          # top row first
        base = row * 8
        line = "".join(_BLOCKS[max(0, min(8, c - base))] for c in cols)
        rows.append(pad + line)
    return rows


class PerfSampler:
    """Each sample() takes one frame and appends it to a fixed-length history; render_graph draws the history."""

    def __init__(self, history: int = 120):
        self.cpu_history: deque[float] = deque(maxlen=history)
        self.gpu_history: deque[float] = deque(maxlen=history)
        self.last: Optional[PerfSample] = None
        psutil.cpu_percent(interval=None)   # the first call only sets the baseline and returns 0; call it early so the first frame is meaningful

    def _gpu(self) -> tuple[Optional[float], Optional[int]]:
        out = subprocess.run(_IOREG_CMD, capture_output=True, text=True, timeout=2, check=False).stdout
        return parse_ioreg(out)

    def sample(self) -> PerfSample:
        cpu = float(psutil.cpu_percent(interval=None))
        vm = psutil.virtual_memory()
        try:
            gpu, gpu_mem = self._gpu()
        except Exception:
            gpu, gpu_mem = None, None
        s = PerfSample(cpu=cpu, gpu=gpu, mem_used=int(vm.total - vm.available), mem_total=int(vm.total),
                       gpu_mem=gpu_mem)
        self.cpu_history.append(cpu)
        self.gpu_history.append(gpu if gpu is not None else 0.0)
        self.last = s
        return s
