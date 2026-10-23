"""phone: an MCP server that drives this Android phone over adb.

Reads the screen as text (uiautomator) instead of screenshots, taps elements by
label, and refuses to press send/submit/pay/delete-style buttons unless the
caller passes confirm=True.
"""

import io
import os
import re
import shlex
import socket
import subprocess
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image

# Termux's adb works under proot; Ubuntu's fails to pair/connect.
ADB = os.environ.get("PHONE_ADB", "/data/data/com.termux/files/usr/bin/adb")
PHONE_IP = os.environ.get("PHONE_IP", "192.168.0.99")

# Labels that commit something irreversible or outward-facing.
RISKY = re.compile(
    r"\b(send|submit|pay|buy|purchase|order|checkout|delete|remove|uninstall|"
    r"confirm|apply|post|publish|transfer|enviar|candidatar|candidatura|comprar|"
    r"pagar|finalizar|excluir|apagar|remover|confirmar|publicar|transferir)\b",
    re.I,
)

KEYS = {
    "back": 4, "home": 3, "recents": 187, "enter": 66, "tab": 61, "delete": 67,
    "search": 84, "menu": 82, "play_pause": 85, "play": 126, "pause": 127,
    "next": 87, "previous": 88, "volume_up": 24, "volume_down": 25, "power": 26,
}

mcp = MCPServer(
    "phone",
    instructions=(
        "Control the user's Android phone. Call screen() to read the UI as a numbered "
        "element list, then act with tap/type_text/scroll using element numbers or labels. "
        "Prefer screen() over screenshot(); use screenshot() only for images or when the "
        "element list is empty. Pass expect_app to actions so they fail instead of "
        "landing in the wrong app. Tools refuse send/submit/pay/delete-style taps until "
        "you get the user's approval and retry with confirm=True."
    ),
)


@dataclass
class Element:
    idx: int
    cls: str
    text: str
    desc: str
    rid: str
    x: int
    y: int
    clickable: bool
    editable: bool
    scrollable: bool
    checked: bool | None

    @property
    def label(self) -> str:
        return self.text or self.desc or self.rid

    def line(self) -> str:
        kind = self.cls.rsplit(".", 1)[-1]
        parts = [f"[{self.idx}] {kind}"]
        if self.text:
            parts.append(repr(self.text[:120]))
        if self.desc and self.desc != self.text:
            parts.append(f"desc={self.desc[:80]!r}")
        if self.rid and not (self.text or self.desc):
            parts.append(f"id={self.rid}")
        flags = [f for f, on in (("tap", self.clickable), ("edit", self.editable),
                                 ("scroll", self.scrollable)) if on]
        if self.checked is not None:
            flags.append("checked" if self.checked else "unchecked")
        if flags:
            parts.append("{" + ",".join(flags) + "}")
        parts.append(f"@{self.x},{self.y}")
        return " ".join(parts)


_serial: str | None = None
_last: list[Element] = []


# ---------- adb plumbing ----------

def _run(args: list[str], timeout: float = 20, binary: bool = False):
    cmd = [ADB] + (["-s", _serial] if _serial else []) + args
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if binary:
        return r.returncode, r.stdout, r.stderr.decode(errors="replace")
    return r.returncode, r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")


def _devices() -> list[str]:
    r = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=10)
    return [l.split()[0] for l in r.stdout.splitlines()[1:] if l.strip().endswith("device")]


def _scan_ports(ip: str) -> list[int]:
    def chk(p: int) -> int | None:
        with socket.socket() as s:
            s.settimeout(0.3)
            try:
                s.connect((ip, p))
                return p
            except OSError:
                return None
    with ThreadPoolExecutor(400) as ex:
        return [p for p in ex.map(chk, range(30000, 65536)) if p]


def _ensure() -> str:
    """Return a connected device serial, reconnecting over wireless adb if needed."""
    global _serial
    devs = _devices()
    if _serial in devs:
        return _serial
    if devs:
        _serial = devs[0]
        return _serial
    for p in _scan_ports(PHONE_IP):
        r = subprocess.run([ADB, "connect", f"{PHONE_IP}:{p}"], capture_output=True,
                           text=True, timeout=10)
        if r.stdout.startswith("connected"):
            _serial = f"{PHONE_IP}:{p}"
            return _serial
    raise RuntimeError(
        f"No phone connected. Check Wireless debugging is on and on the same Wi-Fi "
        f"({PHONE_IP}); if the phone was never paired, pair it with `adb pair` first."
    )


def _shell(cmd: str, timeout: float = 20) -> str:
    _ensure()
    code, out, err = _run(["shell", cmd], timeout)
    if code != 0 and not out:
        raise RuntimeError(err.strip() or f"adb shell failed: {cmd}")
    return out


def _foreground() -> str:
    out = _shell("dumpsys activity activities | grep -m1 topResumedActivity")
    m = re.search(r"u0 ([\w.]+)/", out)
    return m.group(1) if m else "unknown"


def _check_app(expect_app: str | None) -> None:
    if expect_app:
        fg = _foreground()
        if expect_app.lower() not in fg.lower():
            raise RuntimeError(f"Refusing: foreground app is {fg}, expected {expect_app}.")


# ---------- screen parsing ----------

def _dump_xml() -> str:
    _ensure()
    _, out, _ = _run(["exec-out", "uiautomator", "dump", "/dev/tty"], timeout=30)
    if "<hierarchy" not in out:
        _shell("uiautomator dump /sdcard/.phone_mcp.xml", timeout=30)
        out = _shell("cat /sdcard/.phone_mcp.xml")
    start, end = out.find("<?xml"), out.rfind("</hierarchy>")
    if start < 0 or end < 0:
        raise RuntimeError("Could not read the UI tree (screen may be a game, video or secure view). Try screenshot().")
    return out[start:end + len("</hierarchy>")]


def _parse(xml: str) -> list[Element]:
    els: list[Element] = []
    for n in ET.fromstring(xml).iter("node"):
        a = n.attrib
        text, desc = a.get("text", "").strip(), a.get("content-desc", "").strip()
        clickable = a.get("clickable") == "true" or a.get("long-clickable") == "true"
        editable = "EditText" in a.get("class", "")
        scrollable = a.get("scrollable") == "true"
        checkable = a.get("checkable") == "true"
        if not (text or desc or clickable or editable or scrollable):
            continue
        m = re.findall(r"\d+", a.get("bounds", ""))
        if len(m) != 4:
            continue
        x1, y1, x2, y2 = map(int, m)
        if x2 <= x1 or y2 <= y1:
            continue
        rid = a.get("resource-id", "").rsplit("/", 1)[-1]
        # Skip bare unlabeled containers; they add noise without being targetable by name.
        if not (text or desc or editable) and not rid:
            continue
        els.append(Element(len(els), a.get("class", ""), text, desc, rid,
                           (x1 + x2) // 2, (y1 + y2) // 2, clickable, editable,
                           scrollable, (a.get("checked") == "true") if checkable else None))
    return els


def _read() -> list[Element]:
    global _last
    _last = _parse(_dump_xml())
    return _last


def _find(target: str, els: list[Element]) -> Element:
    t = target.strip().lower()
    for pred in (
        lambda e: e.text.lower() == t or e.desc.lower() == t,
        lambda e: e.rid.lower() == t,
        lambda e: t in e.text.lower() or t in e.desc.lower(),
    ):
        hits = [e for e in els if pred(e)]
        if hits:
            # Prefer something actually tappable when labels repeat.
            return next((e for e in hits if e.clickable or e.editable), hits[0])
    raise RuntimeError(f"No element matching {target!r}. Call screen() to see what's there.")


def _resolve(target: str | int | None, x: int | None, y: int | None) -> tuple[int, int, str]:
    if x is not None and y is not None:
        return x, y, f"({x},{y})"
    if target is None:
        raise RuntimeError("Give a target (element number or label) or x and y.")
    if isinstance(target, int) or str(target).isdigit():
        i = int(target)
        if not 0 <= i < len(_last):
            raise RuntimeError(f"No element [{i}] in the last screen(); call screen() again.")
        e = _last[i]
    else:
        e = _find(str(target), _read())
    return e.x, e.y, e.label


def _settle(delay: float) -> str:
    time.sleep(delay)
    return f"foreground: {_foreground()}"


# ---------- tools ----------

@mcp.tool()
def connect() -> str:
    """Connect (or reconnect) to the phone and report model, Android version and battery."""
    s = _ensure()
    info = _shell("getprop ro.product.model; getprop ro.build.version.release; "
                  "dumpsys battery | grep -m1 level; wm size").split()
    return f"connected {s} | {' '.join(info)} | foreground: {_foreground()}"


@mcp.tool()
def screen(filter: str | None = None) -> str:
    """Read the current screen as a numbered list of elements (text, flags, center coords).

    Use the numbers with tap/type_text. `filter` keeps only lines containing that text.
    """
    els = _read()
    lines = [e.line() for e in els]
    if filter:
        lines = [l for l in lines if filter.lower() in l.lower()]
    return f"app: {_foreground()} | {len(els)} elements\n" + "\n".join(lines)


@mcp.tool()
def screenshot(width: int = 540) -> Image:
    """Take a screenshot, scaled to `width` px to save tokens. Use only when screen() isn't enough."""
    _ensure()
    _, png, err = _run(["exec-out", "screencap", "-p"], timeout=30, binary=True)
    if not png.startswith(b"\x89PNG"):
        raise RuntimeError(f"screencap failed: {err}")
    try:
        from PIL import Image as PILImage
        im = PILImage.open(io.BytesIO(png))
        if width and im.width > width:
            im = im.resize((width, round(im.height * width / im.width)))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=70)
        return Image(data=buf.getvalue(), format="jpeg")
    except ImportError:
        return Image(data=png, format="png")


@mcp.tool()
def tap(target: str | None = None, x: int | None = None, y: int | None = None,
        long: bool = False, confirm: bool = False, expect_app: str | None = None) -> str:
    """Tap an element by number from screen(), by label text/description/id, or by x,y.

    Taps on send/submit/pay/delete-style labels are refused unless confirm=True,
    which you may only pass after the user approved that specific action.
    """
    _check_app(expect_app)
    px, py, label = _resolve(target, x, y)
    if RISKY.search(label) and not confirm:
        return (f"BLOCKED: {label!r} looks like it sends/submits/pays/deletes. Ask the user, "
                f"then retry with confirm=True.")
    if long:
        _shell(f"input swipe {px} {py} {px} {py} 700")
    else:
        _shell(f"input tap {px} {py}")
    return f"tapped {label!r} at {px},{py} | {_settle(0.8)}"


@mcp.tool()
def type_text(text: str, target: str | None = None, clear: bool = False,
              submit: bool = False, expect_app: str | None = None) -> str:
    """Type text, optionally tapping a field first (number or label) and clearing it.

    submit=True presses Enter afterwards. Only ASCII is supported by adb input;
    accented characters are replaced with their plain letters.
    """
    import unicodedata
    _check_app(expect_app)
    if target is not None:
        px, py, _ = _resolve(target, None, None)
        _shell(f"input tap {px} {py}")
        time.sleep(0.5)
    if clear:
        _shell("input keyevent 123; input keycombination 113 29; input keyevent 67")
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    note = " (non-ASCII characters were simplified)" if plain != text else ""
    # adb `input text` treats %s as space; quote everything else for the device shell.
    for chunk in (plain[i:i + 200] for i in range(0, len(plain), 200)):
        _shell("input text " + shlex.quote(chunk.replace("%", r"\%").replace(" ", "%s")), timeout=60)
    if submit:
        _shell("input keyevent 66")
    return f"typed {len(plain)} chars{note} | {_settle(0.5)}"


@mcp.tool()
def key(name: str) -> str:
    """Press a key: back, home, recents, enter, tab, delete, search, play_pause, play, pause,
    next, previous, volume_up, volume_down, power."""
    code = KEYS.get(name.lower())
    if code is None:
        raise RuntimeError(f"Unknown key {name!r}. Options: {', '.join(KEYS)}")
    _shell(f"input keyevent {code}")
    return f"pressed {name} | {_settle(0.6)}"


@mcp.tool()
def scroll(direction: str = "down", until: str | None = None, max_swipes: int = 8) -> str:
    """Scroll up/down/left/right once, or repeatedly until an element containing `until` appears."""
    w, h = 1080, 2340
    m = re.search(r"(\d+)x(\d+)", _shell("wm size"))
    if m:
        w, h = int(m[1]), int(m[2])
    cx, cy = w // 2, h // 2
    vec = {"down": (cx, int(h * .75), cx, int(h * .3)), "up": (cx, int(h * .3), cx, int(h * .75)),
           "right": (int(w * .8), cy, int(w * .2), cy), "left": (int(w * .2), cy, int(w * .8), cy)}
    if direction not in vec:
        raise RuntimeError("direction must be up, down, left or right")
    swipe = "input swipe {} {} {} {} 350".format(*vec[direction])
    for i in range(max_swipes if until else 1):
        if until:
            try:
                e = _find(until, _read())
                return f"found [{e.idx}] {e.label!r} after {i} swipes"
            except RuntimeError:
                pass
        _shell(swipe)
        time.sleep(0.7)
    if until:
        try:
            e = _find(until, _read())
            return f"found [{e.idx}] {e.label!r} after {max_swipes} swipes"
        except RuntimeError:
            return f"{until!r} not found after {max_swipes} swipes"
    return f"scrolled {direction}"


@mcp.tool()
def wait_for(text: str, timeout: float = 10, gone: bool = False) -> str:
    """Wait until an element containing `text` appears (or disappears if gone=True)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            e = _find(text, _read())
            if not gone:
                return f"found [{e.idx}] {e.label!r}"
        except RuntimeError:
            if gone:
                return f"{text!r} is gone"
        time.sleep(0.7)
    return f"timed out after {timeout}s waiting for {text!r}{' to disappear' if gone else ''}"


@mcp.tool()
def open_app(name: str) -> str:
    """Launch an app by package name or by a word in its package (e.g. 'youtube', 'whatsapp')."""
    pkgs = [l.removeprefix("package:").strip() for l in _shell("pm list packages").splitlines()]
    n = name.lower().replace(" ", "")
    match = name if name in pkgs else next(
        (p for p in sorted(pkgs, key=len) if n in p.lower().replace("_", "")), None)
    if not match:
        raise RuntimeError(f"No installed package matches {name!r}.")
    _shell(f"monkey -p {match} -c android.intent.category.LAUNCHER 1")
    return f"launched {match} | {_settle(2)}"


@mcp.tool()
def open_url(url: str, package: str | None = None) -> str:
    """Open a URL or deep link, optionally in a specific app (e.g. com.google.android.youtube)."""
    cmd = f"am start -a android.intent.action.VIEW -d {shlex.quote(url)}"
    if package:
        cmd += f" {shlex.quote(package)}"
    _shell(cmd)
    return f"opened {url} | {_settle(2)}"


@mcp.tool()
def current_app() -> str:
    """Return the package of the app currently in the foreground."""
    return _foreground()


@mcp.tool()
def adb_shell(command: str) -> str:
    """Run a shell command on the phone (as the adb shell user). For anything the other tools don't cover."""
    return _shell(command, timeout=60)[-8000:]


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
