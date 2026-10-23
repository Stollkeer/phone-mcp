"""phone: an MCP server that drives this Android phone.

Reads the screen as text through uiautomator2 (a resident helper on the phone,
~0.5 s per read), returns the updated screen after every action, and refuses
send/submit/pay/delete-style taps unless the caller passes confirm=True.
"""

import contextlib
import hashlib
import io
import os
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

# Termux's adb works under proot; Ubuntu's fails to pair/connect. uiautomator2 uses it too.
ADB = os.environ.get("PHONE_ADB", "/data/data/com.termux/files/usr/bin/adb")
os.environ.setdefault("ADBUTILS_ADB_PATH", ADB)

import uiautomator2 as u2  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.utilities.types import Image  # noqa: E402

PHONE_IP = os.environ.get("PHONE_IP", "192.168.0.99")
PHONE_PORT = int(os.environ.get("PHONE_PORT", "5555"))

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
        "Control the user's Android phone. screen() returns the UI as a numbered element "
        "list; every action (tap, type_text, key, scroll, open_app, open_url) waits for the "
        "UI to settle and returns the new screen, so don't call screen() again after an "
        "action. Use filter= to shorten output. Prefer recipes (whatsapp_send, "
        "download_image) over step-by-step taps. screenshot() only for images or when the "
        "element list is empty. Pass expect_app so actions fail instead of landing in the "
        "wrong app. Send/submit/pay/delete-style actions need the user's approval and "
        "confirm=True."
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
    w: int
    h: int
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


_d: u2.Device | None = None
_last: list[Element] = []
_last_hash = ""


# ---------- device plumbing ----------

def _adb(*args: str, timeout: float = 15) -> str:
    try:
        r = subprocess.run([ADB, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"adb timed out after {timeout}s: {' '.join(args)[:80]}")
    return r.stdout + r.stderr


def _dev() -> u2.Device:
    """Return a live uiautomator2 device, reconnecting over wireless adb if needed."""
    global _d
    if _d is not None:
        try:
            _d.info
            return _d
        except Exception:
            _d = None
    serial = f"{PHONE_IP}:{PHONE_PORT}"
    if f"{serial}\tdevice" not in _adb("devices"):
        if not _adb("connect", serial).startswith("connected"):
            raise RuntimeError(
                f"No phone at {serial}. After a reboot, connect once to the Wireless "
                f"debugging port and run `adb tcpip {PHONE_PORT}`.")
    # uiautomator2 may print install progress; stdout belongs to the MCP protocol.
    with contextlib.redirect_stdout(sys.stderr):
        _d = u2.connect(serial)
        _d.info
    return _d


def _shell(cmd: str, timeout: float = 30) -> str:
    return _dev().shell(cmd, timeout=timeout).output


def _foreground() -> str:
    try:
        return _dev().app_current().get("package", "unknown")
    except Exception:
        return "unknown"


def _check_app(expect_app: str | None) -> None:
    if expect_app:
        fg = _foreground()
        if expect_app.lower() not in fg.lower():
            raise RuntimeError(f"Refusing: foreground app is {fg}, expected {expect_app}.")


# ---------- screen parsing ----------

def _dump_xml() -> str:
    try:
        return _dev().dump_hierarchy(compressed=True)
    except Exception as e:
        raise RuntimeError(f"Could not read the UI tree ({e}). Use screenshot() instead.")


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
        m = re.findall(r"-?\d+", a.get("bounds", ""))
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
                           (x1 + x2) // 2, (y1 + y2) // 2, x2 - x1, y2 - y1,
                           clickable, editable, scrollable,
                           (a.get("checked") == "true") if checkable else None))
    return els


def _read() -> list[Element]:
    global _last, _last_hash
    xml = _dump_xml()
    _last_hash = hashlib.md5(xml.encode()).hexdigest()
    _last = _parse(xml)
    return _last


def _render(filter: str | None = None, limit: int = 120) -> str:
    lines = [e.line() for e in _last]
    if filter:
        lines = [l for l in lines if filter.lower() in l.lower()]
    more = f"\n… {len(lines) - limit} more (use filter=)" if len(lines) > limit else ""
    return f"app: {_foreground()} | {len(_last)} elements\n" + "\n".join(lines[:limit]) + more


def _settle(timeout: float = 3.0) -> None:
    """Wait until the UI changes from the pre-action dump and then holds still briefly."""
    before, end = _last_hash, time.time() + timeout
    changed, prev = False, None
    time.sleep(0.25)
    while time.time() < end:
        _read()
        if _last_hash != before:
            changed = True
        if changed and _last_hash == prev:
            return
        prev = _last_hash
        time.sleep(0.25)


def _after(filter: str | None = None, show: bool = True, timeout: float = 3.0) -> str:
    _settle(timeout)
    return _render(filter) if show else f"app: {_foreground()}"


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


def _wait_app(pkg: str, timeout: float = 8) -> None:
    end = time.time() + timeout
    while _foreground() != pkg:
        if time.time() > end:
            raise RuntimeError(f"{pkg} didn't come to the front (foreground: {_foreground()}).")
        time.sleep(0.3)


def _wait(target: str, timeout: float = 10) -> Element:
    end = time.time() + timeout
    while True:
        try:
            return _find(target, _read())
        except RuntimeError:
            if time.time() > end:
                raise RuntimeError(f"Timed out after {timeout}s waiting for {target!r}.")
            time.sleep(0.3)


# ---------- basic tools ----------

@mcp.tool()
def connect() -> str:
    """Connect (or reconnect) to the phone and report model, Android version and battery."""
    d = _dev()
    info = d.device_info
    batt = re.search(r"level: (\d+)", _shell("dumpsys battery"))
    return (f"connected {PHONE_IP}:{PHONE_PORT} | {info.get('model')} Android "
            f"{info.get('version')} | battery {batt[1] if batt else '?'}% | "
            f"foreground: {_foreground()}")


@mcp.tool()
def screen(filter: str | None = None) -> str:
    """Read the current screen as a numbered list of elements (text, flags, center coords).

    Use the numbers with tap/type_text. `filter` keeps only lines containing that text.
    """
    _read()
    return _render(filter)


@mcp.tool()
def screenshot(width: int = 540) -> Image:
    """Take a screenshot, scaled to `width` px to save tokens. Use only when screen() isn't enough."""
    im = _dev().screenshot()
    if width and im.width > width:
        im = im.resize((width, round(im.height * width / im.width)))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=70)
    return Image(data=buf.getvalue(), format="jpeg")


@mcp.tool()
def tap(target: str | None = None, x: int | None = None, y: int | None = None,
        long: bool = False, confirm: bool = False, expect_app: str | None = None,
        filter: str | None = None, show: bool = True) -> str:
    """Tap an element by number from screen(), by label text/description/id, or by x,y.

    Returns the updated screen (filter= to narrow it, show=False to skip it).
    Taps on send/submit/pay/delete-style labels are refused unless confirm=True,
    which you may only pass after the user approved that specific action.
    """
    _check_app(expect_app)
    px, py, label = _resolve(target, x, y)
    if RISKY.search(label) and not confirm:
        return (f"BLOCKED: {label!r} looks like it sends/submits/pays/deletes. Ask the user, "
                f"then retry with confirm=True.")
    d = _dev()
    d.long_click(px, py, 0.8) if long else d.click(px, py)
    return f"tapped {label!r} at {px},{py}\n" + _after(filter, show)


@mcp.tool()
def type_text(text: str, target: str | None = None, clear: bool = False,
              submit: bool = False, expect_app: str | None = None,
              filter: str | None = None, show: bool = True) -> str:
    """Type text (any language/accents), optionally tapping a field first (number or label).

    clear=True empties the field first; submit=True presses Enter afterwards.
    Returns the updated screen.
    """
    _check_app(expect_app)
    d = _dev()
    if target is not None:
        px, py, _ = _resolve(target, None, None)
        d.click(px, py)
        time.sleep(0.4)
    d.send_keys(text, clear=clear)
    if submit:
        d.press("enter")
    return f"typed {len(text)} chars\n" + _after(filter, show)


@mcp.tool()
def key(name: str, filter: str | None = None, show: bool = True) -> str:
    """Press a key: back, home, recents, enter, tab, delete, search, play_pause, play, pause,
    next, previous, volume_up, volume_down, power. Returns the updated screen."""
    code = KEYS.get(name.lower())
    if code is None:
        raise RuntimeError(f"Unknown key {name!r}. Options: {', '.join(KEYS)}")
    _shell(f"input keyevent {code}")
    return f"pressed {name}\n" + _after(filter, show)


@mcp.tool()
def scroll(direction: str = "down", until: str | None = None, max_swipes: int = 8,
           filter: str | None = None) -> str:
    """Scroll up/down/left/right once, or repeatedly until an element containing `until`
    appears. Returns the updated screen."""
    d = _dev()
    w, h = d.window_size()
    cx, cy = w // 2, h // 2
    vec = {"down": (cx, h * .75, cx, h * .3), "up": (cx, h * .3, cx, h * .75),
           "right": (w * .8, cy, w * .2, cy), "left": (w * .2, cy, w * .8, cy)}
    if direction not in vec:
        raise RuntimeError("direction must be up, down, left or right")
    for i in range(max_swipes if until else 1):
        if until:
            try:
                e = _find(until, _read())
                return f"found [{e.idx}] {e.label!r} after {i} swipes\n" + _render(filter)
            except RuntimeError:
                pass
        d.swipe(*vec[direction], duration=0.25)
        _settle(1.5)
    if until:
        try:
            e = _find(until, _last)
            return f"found [{e.idx}] {e.label!r} after {max_swipes} swipes\n" + _render(filter)
        except RuntimeError:
            return f"{until!r} not found after {max_swipes} swipes\n" + _render(filter)
    return f"scrolled {direction}\n" + _render(filter)


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
        time.sleep(0.3)
    return f"timed out after {timeout}s waiting for {text!r}{' to disappear' if gone else ''}"


def _package(name: str) -> str:
    pkgs = _dev().app_list()
    n = name.lower().replace(" ", "")
    match = name if name in pkgs else next(
        (p for p in sorted(pkgs, key=len) if n in p.lower().replace("_", "")), None)
    if not match:
        raise RuntimeError(f"No installed package matches {name!r}.")
    return match


@mcp.tool()
def open_app(name: str, filter: str | None = None, show: bool = True) -> str:
    """Launch an app by package name or a word in it (e.g. 'youtube', 'whatsapp').
    Returns the app's first screen."""
    pkg = _package(name)
    _dev().app_start(pkg)
    return f"launched {pkg}\n" + _after(filter, show, timeout=5)


@mcp.tool()
def open_url(url: str, package: str | None = None, filter: str | None = None,
             show: bool = True) -> str:
    """Open a URL or deep link, optionally in a specific app (e.g. com.google.android.youtube).
    Returns the resulting screen."""
    cmd = f"am start -a android.intent.action.VIEW -d {shlex.quote(url)}"
    if package:
        cmd += f" {shlex.quote(package)}"
    _shell(cmd)
    return f"opened {url}\n" + _after(filter, show, timeout=6)


@mcp.tool()
def current_app() -> str:
    """Return the package of the app currently in the foreground."""
    return _foreground()


@mcp.tool()
def adb_shell(command: str) -> str:
    """Run a shell command on the phone (as the adb shell user). For anything the other tools don't cover."""
    return _shell(command, timeout=60)[-8000:]


# ---------- recipes ----------

def _media_uri(path: str) -> str:
    name = path.rsplit("/", 1)[-1].replace("'", "''")
    out = _shell(f"content query --uri content://media/external/images/media "
                 f"--projection _id --where \"_display_name='{name}'\"")
    ids = re.findall(r"_id=(\d+)", out)
    if not ids:
        # Not indexed yet: ask the media scanner, then retry once.
        _shell(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
               f"-d {shlex.quote('file://' + path)}")
        time.sleep(1.5)
        ids = re.findall(r"_id=(\d+)", _shell(
            f"content query --uri content://media/external/images/media "
            f"--projection _id --where \"_display_name='{name}'\""))
    if not ids:
        raise RuntimeError(f"Image not found in the media store: {path}")
    return f"content://media/external/images/media/{ids[-1]}"


@mcp.tool()
def whatsapp_send(contact: str, text: str = "", image_path: str | None = None,
                  confirm: bool = False) -> str:
    """Send a WhatsApp message and/or image (with `text` as caption) to a chat by its exact name.

    Without confirm=True it stages everything and stops before Send, returning the
    staged screen for you to show the user. Only pass confirm=True after the user
    approved sending this message to this contact.
    """
    d = _dev()
    if image_path:
        uri = _media_uri(image_path)
        cmd = (f"am start -a android.intent.action.SEND -t image/* --eu android.intent.extra.STREAM "
               f"{uri} --grant-read-uri-permission -p com.whatsapp")
        if text:
            cmd += f" --es android.intent.extra.TEXT {shlex.quote(text)}"
        _shell(cmd)
    else:
        d.app_start("com.whatsapp")
    _wait_app("com.whatsapp")
    # Both the share picker and the chat list have a search control (PT or EN UI).
    el, end = None, time.time() + 8
    while el is None and time.time() < end:
        time.sleep(0.4)
        el = next((e for e in _read() if e.clickable and
                   re.search(r"pesquis|search", e.text + e.desc + e.rid, re.I)), None)
    if el is None:
        raise RuntimeError("Couldn't find WhatsApp's search control.\n" + _render())
    d.click(el.x, el.y)
    time.sleep(0.6)
    d.send_keys(contact, clear=True)
    time.sleep(1.2)
    hits = [e for e in _read() if e.text.strip().lower() == contact.strip().lower() and e.y > 300]
    if not hits:
        raise RuntimeError(f"No chat named exactly {contact!r}. Visible:\n" + _render())
    d.click(hits[0].x, hits[0].y)
    time.sleep(1.2)
    if not image_path:
        entry = d(resourceId="com.whatsapp:id/entry")
        if not entry.wait(timeout=5):
            raise RuntimeError("Opened the chat but couldn't find the message box.")
        entry.click()
        d.send_keys(text, clear=True)
        time.sleep(0.4)
    send = d(resourceId="com.whatsapp:id/send")
    if not send.wait(timeout=5):
        send = d(descriptionMatches="(?i)^(enviar|send)$")
        if not send.wait(timeout=3):
            raise RuntimeError("Staged, but couldn't find the Send button.\n" + _render())
    _read()
    staged = f"STAGED for {contact!r}: text={text!r} image={image_path or '-'}"
    if not confirm:
        return staged + "\nNot sent. Retry with confirm=True once the user approves.\n" + _render()
    send.click()
    time.sleep(1.5)
    _read()
    return staged + "\nSENT.\n" + _render(limit=25)


def _downloads() -> list[str]:
    # toybox ls escapes spaces ("a\ b") when not on a tty; undo that.
    return [re.sub(r"\\(.)", r"\1", f) for f in _shell("ls -1 /sdcard/Download").split("\n") if f]


@mcp.tool()
def download_image(query: str, pick: int = 1) -> str:
    """Search Google Images in Chrome for `query` and download result number `pick`
    (1 = first) via Chrome's long-press menu. Returns the saved file path."""
    d = _dev()
    before = set(_downloads())
    url = "https://www.google.com/search?tbm=isch&q=" + re.sub(r"\s+", "+", query.strip())
    _shell(f"am start -a android.intent.action.VIEW -d {shlex.quote(url)} com.android.chrome")
    _wait_app("com.android.chrome")
    time.sleep(1.5)
    w, _ = d.window_size()
    imgs, end = [], time.time() + 15
    while time.time() < end:
        # Chrome exposes Google Images results as large Buttons/Images titled by the page.
        imgs = [e for e in _read() if re.search(r"(Button|Image)$", e.cls) and e.w > w * 0.3
                and e.h > 200 and e.y > 600]
        if len(imgs) >= pick:
            break
        time.sleep(0.8)
    if len(imgs) < pick:
        raise RuntimeError(f"Found only {len(imgs)} image results. Use screenshot() to check.")
    img = sorted(imgs, key=lambda e: (e.y, e.x))[pick - 1]
    d.long_click(img.x, img.y, 0.9)
    item = None
    for label in ("Baixar imagem", "Download image", "Fazer download da imagem"):
        try:
            item = _wait(label, 3)
            break
        except RuntimeError:
            continue
    if item is None:
        raise RuntimeError("Long-press menu had no download option.\n" + _render())
    d.click(item.x, item.y)
    for _ in range(20):
        time.sleep(0.5)
        new = [f for f in _downloads() if f not in before]
        if new:
            return f"/sdcard/Download/{new[0]}\n(downloaded: {img.desc or img.text or 'image'})"
    raise RuntimeError("Tapped download but no new file appeared in /sdcard/Download.")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
