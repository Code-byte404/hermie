"""Performance sampling: ioreg output parsing and the sampler."""
from hermie.perf import PerfSample, PerfSampler, parse_ioreg, render_graph

IOREG_OUTPUT = """\
+-o AGXAcceleratorG14X  <class AGXAcceleratorG14X, id 0x100000466, registered, matched, active, busy 0 (0 ms), retain 217>
    {
      "IOClass" = "AGXAcceleratorG14X"
      "PerformanceStatistics" = {"Device Utilization %"=37,"Renderer Utilization %"=35,"Tiler Utilization %"=12,"In use system memory"=5368709120,"Alloc system memory"=6000000000}
      "IOAccelIndex" = 0
    }
"""


def test_parse_ioreg_reads_device_utilization_and_memory():
    gpu, mem = parse_ioreg(IOREG_OUTPUT)
    assert gpu == 37.0
    assert mem == 5368709120


def test_parse_ioreg_without_stats_returns_none():
    assert parse_ioreg("") == (None, None)
    assert parse_ioreg('"Renderer Utilization %"=5') == (None, None)


def test_sampler_keeps_bounded_history_and_survives_gpu_failure(monkeypatch):
    sampler = PerfSampler(history=3)
    monkeypatch.setattr(sampler, "_gpu", lambda: (_ for _ in ()).throw(RuntimeError("no ioreg")))
    for _ in range(5):
        s = sampler.sample()
    assert isinstance(s, PerfSample)
    assert 0.0 <= s.cpu <= 100.0
    assert s.gpu is None
    assert s.mem_total > s.mem_used > 0
    assert len(sampler.cpu_history) == 3
    assert len(sampler.gpu_history) == 3 and all(v == 0.0 for v in sampler.gpu_history)


def test_render_graph_columns_and_alignment():
    rows = render_graph([0, 50, 100, 12.5], width=6, height=2)
    assert len(rows) == 2 and all(len(r) == 6 for r in rows)
    # padded with blanks on the left, newest frame on the right; 100% fills both rows, 50% only the bottom
    # row, 12.5% is level 2 of 16
    assert rows[0] == "    █ "
    assert rows[1] == "   ██▂"
    assert render_graph([200], width=1, height=1) == ["█"]      # values above the maximum are clamped
    assert render_graph([], width=3, height=1) == ["   "]
    assert render_graph([50] * 10, width=4, height=1) == ["▄▄▄▄"]   # only the last width frames are kept
