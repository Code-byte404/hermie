"""Mac toolchain for the executor: the screenshot tool (simulator / Mac screen, image returned to the vision model),
Xcode instructions, risk rules for xcodebuild / simctl / AXe. No simulator needed: the capture command is injected."""
import struct
import zlib
from pathlib import Path

import pytest
from pydantic_ai.messages import BinaryContent, ModelRequest, UserPromptPart

from hermie import agents
from hermie.capabilities import rule_risk
from hermie.config import RunMode
from hermie.events import Approval, Tainted
from hermie.mactools import ScreenCapture
from hermie.recon import summarize_tools

from .conftest import FakeJudge, Script, final, tool


def make_png(w: int, h: int) -> bytes:
    """A valid RGB PNG of the given size (so sips can really downscale it)."""
    raw = b"".join(b"\x00" + bytes([200, 30, 30]) * w for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FakeRun:
    """Stands in for the subprocess: records argv and writes a PNG where the command would."""

    def __init__(self, size=(1600, 1200), code=0, stderr=""):
        self.size, self.code, self.stderr, self.calls = size, code, stderr, []

    async def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if self.code == 0:
            Path(argv[-1]).write_bytes(make_png(*self.size))
        return self.code, self.stderr


@pytest.fixture
def run():
    return FakeRun()


def images_seen(script: Script) -> list[BinaryContent]:
    """Images in the history of the last model request (earlier requests are prefixes of it)."""
    out = []
    for m in (script.seen[-1] if script.seen else []):
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, UserPromptPart) and not isinstance(p.content, str):
                    out.extend(c for c in p.content if isinstance(c, BinaryContent))
    return out


# ---------------------------------------------------------------- ScreenCapture (controller process, fixed argv)

def test_argv_simulator_and_mac(settings):
    sc = ScreenCapture(settings)
    out = Path("/x/shot.png")
    assert sc.argv("simulator", "booted", out) == ["/usr/bin/xcrun", "simctl", "io", "booted", "screenshot", "/x/shot.png"]
    assert sc.argv("simulator", "iPhone 17", out)[3] == "iPhone 17"
    assert sc.argv("mac", "", out) == ["/usr/sbin/screencapture", "-x", "/x/shot.png"]


@pytest.mark.parametrize("device", ["booted; rm -rf /", "a" * 80, "", "$(id)"])
def test_bad_simulator_device_rejected(settings, device):
    with pytest.raises(ValueError):
        ScreenCapture(settings).argv("simulator", device, Path("/x/shot.png"))


async def test_capture_saves_full_image_and_downscales_for_the_model(settings, run):
    settings.screenshot_max_px = 400
    shot = await ScreenCapture(settings, run=run).capture("simulator", "booted")
    assert shot.path.parent == settings.data_dir / "screenshots" and shot.path.suffix == ".png"
    assert shot.path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and len(shot.path.read_bytes()) > len(shot.data)
    assert (shot.width, shot.height) == (400, 300)             # downscaled copy is what the model sees
    assert shot.data[:8] == b"\x89PNG\r\n\x1a\n"
    assert run.calls[0][:5] == ["/usr/bin/xcrun", "simctl", "io", "booted", "screenshot"]


async def test_capture_failure_carries_stderr(settings):
    sc = ScreenCapture(settings, run=FakeRun(code=149, stderr="Unable to lookup in current state: Shutdown"))
    with pytest.raises(RuntimeError, match="Shutdown"):
        await sc.capture("simulator", "booted")


# ---------------------------------------------------------------- the executor tool

async def test_simulator_screenshot_reaches_model_as_image_without_taint(make_agent, settings, run):
    ex = Script([tool("screenshot", target="simulator")], final=final())
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex)
    agent.session.screen = ScreenCapture(settings, run=run)
    r = await agent.run("check the app screen")
    imgs = images_seen(ex)
    assert len(imgs) == 1 and imgs[0].media_type == "image/png" and imgs[0].data[:4] == b"\x89PNG"
    sent = ex.sent_text()
    assert "Screenshot saved" in sent and "screenshots/" in sent
    assert "\\x89PNG" not in sent and "BinaryContent" not in sent     # image bytes never show up as text
    assert not r.tainted and not any(isinstance(e, Tainted) for e in agent.events)
    assert list((settings.data_dir / "screenshots").glob("*.png"))


async def test_mac_screenshot_taints_and_asks_once_in_default_mode(make_agent, settings, run):
    ex = Script([tool("screenshot", target="mac"), tool("screenshot", target="mac")], final=final())
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, mode=RunMode.DEFAULT)
    agent.session.screen = ScreenCapture(settings, run=run)
    asked = []

    async def approver(req):
        asked.append(req)
        return Approval.ALLOW_SESSION
    agent.bus.approver = approver
    r = await agent.run("what is on my screen")
    assert [a.tool for a in asked] == ["screenshot"] and asked[0].risk == "high" and "Mac screen" in asked[0].reason
    assert len(images_seen(ex)) == 2 and [c[0] for c in run.calls] == ["/usr/sbin/screencapture"] * 2
    assert r.tainted and any("screenshot" in e.reason for e in agent.events if isinstance(e, Tainted))


async def test_mac_screenshot_denied_without_approver(make_agent, settings, run):
    ex = Script([tool("screenshot", target="mac")], final=final())
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, mode=RunMode.DEFAULT)
    agent.session.screen = ScreenCapture(settings, run=run)
    r = await agent.run("what is on my screen")
    assert "denied" in ex.sent_text() and not images_seen(ex) and not run.calls and not r.tainted


async def test_mac_screenshot_can_be_switched_off(make_agent, settings, run):
    ex = Script([tool("screenshot", target="mac")], final=final())
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, screenshot_mac=False)
    agent.session.screen = ScreenCapture(settings, run=run)
    r = await agent.run("what is on my screen")
    assert "SCREENSHOT_MAC" in ex.sent_text() and not run.calls and not r.tainted


async def test_screenshot_failure_is_reported_to_the_model(make_agent, settings):
    ex = Script([tool("screenshot", target="simulator", device="iPhone 99")], final=final())
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex)
    agent.session.screen = ScreenCapture(settings, run=FakeRun(code=149, stderr="Invalid device: iPhone 99"))
    await agent.run("check the app screen")
    assert "Screenshot failed" in ex.sent_text() and "Invalid device" in ex.sent_text()


async def test_screenshot_tool_absent_when_mac_tools_off(make_agent, settings):
    ex = Script([tool("screenshot", target="simulator")], final=final())
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, mac_tools=False)
    assert agent.session.screen is None
    await agent.run("check the app screen")
    assert "Unknown tool" in ex.sent_text() or "screenshot" not in ex.sent_text().lower()


# ---------------------------------------------------------------- instructions + recon + risk rules

@pytest.mark.parametrize("available", [True, False])
async def test_xcode_instructions_follow_detection(make_agent, monkeypatch, available):
    monkeypatch.setattr(agents, "xcode_available", lambda: available)
    ex = Script(final=final())
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex)
    await agent.run("hello")
    instr = [m.instructions for msgs in ex.seen for m in msgs if isinstance(m, ModelRequest) and m.instructions][-1]
    assert ("xcodebuild" in instr) is available and ("axe describe-ui" in instr) is available
    assert "screenshot(target, device)" in instr                  # the screenshot tool is documented either way


def test_recon_reports_axe_and_booted_simulators():
    out = summarize_tools("Python 3.12.1\nXcode 26.4.1\naxe=1.8.0\nbooted=2\n")
    assert "AXe 1.8.0" in out and "2 booted simulator" in out
    assert "axe" not in summarize_tools("Python 3.12.1\naxe=\nbooted=0\n").lower()


@pytest.mark.parametrize("cmd,risk", [
    ("xcodebuild -scheme App -destination 'platform=iOS Simulator,name=iPhone 17' -derivedDataPath build", "low"),
    ("xcrun simctl boot 'iPhone 17'", "low"),
    ("xcrun simctl install booted build/App.app && xcrun simctl launch booted com.example.App", "low"),
    ("swift build && swift test", "low"),
    ("xcrun simctl boot 'iPhone 17' 2>&1; xcrun simctl list devices booted 2>&1", "low"),   # 2>&1 is not a segment
    ("xcodebuild -scheme App build 2>&1 | tail -40", "low"),
    ("xcodebuild -scheme App build 2>&1 > log.txt", None),                                # a real redirect still counts
    ("axe tap -x 100 -y 200 --udid ABC", "low"),
    ("xcrun simctl erase all", None),
    ("xcrun simctl delete unavailable", None),
])
def test_rule_risk_mac_toolchain(cmd, risk):
    assert rule_risk(cmd) == risk
