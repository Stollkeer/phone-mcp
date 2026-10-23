"""phone: an MCP server that drives this Android phone.

Talks to the Mobilerun Portal accessibility service over its local HTTP API
(~15 ms per screen read, no adb or Wi-Fi needed), returns the updated screen
after every action, and refuses send/submit/pay/delete-style taps unless the
caller passes confirm=True. adb is only a fallback for intents with extras
(sharing an image), media keys, and re-enabling Portal if Android kills it.
"""

import base64
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image


def _load_env(path: Path) -> None:
    if path.exists():
        for line in path.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip())


_load_env(Path.home() / ".config/phone-mcp/portal.env")

PORTAL_URL = os.environ.get("PORTAL_URL", "http://127.0.0.1:8080").rstrip("/")
PORTAL_TOKEN = os.environ.get("PORTAL_TOKEN", "")
PORTAL_PKG = "com.mobilerun.portal"
PORTAL_A11Y = f"{PORTAL_PKG}/{PORTAL_PKG}.service.MobilerunAccessibilityService"

# Termux's adb works under proot; Ubuntu's fails to pair/connect.
ADB = os.environ.get("PHONE_ADB", "/data/data/com.termux/files/usr/bin/adb")
# Loopback reaches adbd from Termux on any Wi-Fi network (adb tcpip must be on).
ADB_SERIAL = os.environ.get("PHONE_ADB_SERIAL", "127.0.0.1:5555")

# Labels that commit something irreversible or outward-facing.
RISKY = re.compile(
    r"\b(send|submit|pay|buy|purchase|order|checkout|delete|remove|uninstall|"
    r"confirm|apply|post|publish|transfer|enviar|candidatar|candidatura|comprar|"
    r"pagar|finalizar|excluir|apagar|remover|confirmar|publicar|transferir)\b",
    re.I,
)

# Navigation goes through accessibility global actions; text keys through Portal's
# keyboard; media/hardware keys need adb.
GLOBAL_ACTIONS = {"back": 1, "home": 2, "recents": 3, "notifications": 4,
                  "quick_settings": 5, "lock": 8, "screenshot_save": 9}
TEXT_KEYS = {"enter": 66, "tab": 61, "delete": 67, "search": 84}
ADB_KEYS = {"menu": 82, "play_pause": 85, "play": 126, "pause": 127, "next": 87,
            "previous": 88, "volume_up": 24, "volume_down": 25, "power": 26}

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


_last: list[Element] = []
_last_hash = ""
_last_app = "unknown"
_screen = (1080, 2340)


# ---------- Portal HTTP ----------

def _portal(method: str, path: str, payload: dict | None = None,
            params: dict | None = None, timeout: float = 10, raw: bool = False):
    url = PORTAL_URL + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {PORTAL_TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Portal {path} failed: HTTP {e.code} {e.read()[:200]!r}")
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        raise PortalDown(f"Portal unreachable at {PORTAL_URL} ({e}).")
    if raw:
        return body
    try:
        env = json.loads(body)
    except json.JSONDecodeError:
        return body
    if isinstance(env, dict):
        if env.get("status") == "error":
            raise RuntimeError(f"Portal {path} error: {env.get('message') or env.get('error')}")
        inner = env.get("result", env.get("data", env))
        if isinstance(inner, str):
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                return inner
        return inner
    return env


class PortalDown(RuntimeError):
    pass


def _call(method: str, path: str, payload: dict | None = None, **kw):
    """Portal call that tries once to revive Portal over adb if it's down."""
    try:
        return _portal(method, path, payload, **kw)
    except PortalDown:
        _revive_portal()
        return _portal(method, path, payload, **kw)


def _revive_portal() -> None:
    """Re-enable Portal's accessibility service and HTTP server (needs adb)."""
    try:
        cur = _adb_shell("settings get secure enabled_accessibility_services").strip()
    except RuntimeError as e:
        raise PortalDown(
            f"Portal is down and adb is unavailable ({e}). Open the Mobilerun Portal app, "
            "check its accessibility service is on and HTTP Server is enabled.")
    if PORTAL_A11Y not in cur:
        # Append, never replace: other accessibility services (e.g. ScrollGuard) stay on.
        new = PORTAL_A11Y if cur in ("", "null") else f"{cur}:{PORTAL_A11Y}"
        _adb_shell(f"settings put secure enabled_accessibility_services {shlex.quote(new)}; "
                   "settings put secure accessibility_enabled 1")
        time.sleep(1.5)
    _adb_shell(f"content insert --uri content://{PORTAL_PKG}/toggle_socket_server "
               f"--bind enabled:b:true --bind port:i:{urllib.parse.urlparse(PORTAL_URL).port or 8080}")
    time.sleep(0.8)


# ---------- adb fallback ----------

def _adb_shell(cmd: str, timeout: float = 30) -> str:
    def run(*args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([ADB, *args], capture_output=True, text=True,
                                  errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"adb timed out after {timeout}s")
    if f"{ADB_SERIAL}\tdevice" not in run("devices").stdout:
        if not run("connect", ADB_SERIAL).stdout.startswith("connected"):
            raise RuntimeError(
                f"adb not connected at {ADB_SERIAL}. Turn on Wireless debugging, connect to "
                "its port once and run `adb tcpip 5555`.")
    r = run("-s", ADB_SERIAL, "shell", cmd)
    if r.returncode != 0 and not r.stdout:
        raise RuntimeError(r.stderr.strip() or f"adb shell failed: {cmd}")
    return r.stdout


# ---------- screen parsing ----------

def _state() -> dict:
    global _screen
    st = _call("GET", "/state_full")
    if not isinstance(st, dict) or "a11y_tree" not in st:
        raise RuntimeError(f"Unexpected Portal state: {str(st)[:200]}")
    sb = (st.get("device_context") or {}).get("screen_bounds") or {}
    if sb.get("width"):
        _screen = (sb["width"], sb["height"])
    return st


def _parse(tree) -> list[Element]:
    els: list[Element] = []
    stack = list(reversed(tree if isinstance(tree, list) else [tree]))
    while stack:
        n = stack.pop()
        stack.extend(reversed(n.get("children") or []))
        text = (n.get("text") or "").strip()
        desc = (n.get("contentDescription") or "").strip()
        cls = n.get("className") or ""
        # Portal fills `text` with the class/id name when a node has none; drop that echo.
        if text in (cls.rsplit(".", 1)[-1], (n.get("resourceId") or "").rsplit("/", 1)[-1]):
            text = ""
        clickable = bool(n.get("isClickable") or n.get("isLongClickable"))
        editable = bool(n.get("isEditable"))
        scrollable = bool(n.get("isScrollable"))
        if not (text or desc or clickable or editable or scrollable):
            continue
        if n.get("isVisibleToUser") is False:
            continue
        b = n.get("boundsInScreen") or {}
        x1, y1, x2, y2 = b.get("left", 0), b.get("top", 0), b.get("right", 0), b.get("bottom", 0)
        if x2 <= x1 or y2 <= y1:
            continue
        rid = (n.get("resourceId") or "").rsplit("/", 1)[-1]
        # Skip bare unlabeled containers; they add noise without being targetable by name.
        if not (text or desc or editable) and not rid:
            continue
        els.append(Element(len(els), cls, text, desc, rid, (x1 + x2) // 2, (y1 + y2) // 2,
                           x2 - x1, y2 - y1, clickable, editable, scrollable,
                           bool(n.get("isChecked")) if n.get("isCheckable") else None))
    return els


def _read() -> list[Element]:
    global _last, _last_hash, _last_app
    st = _state()
    _last_app = (st.get("phone_state") or {}).get("packageName") or "unknown"
    _last = _parse(st["a11y_tree"])
    # Fingerprint what matters for "has the UI settled", ignoring playback progress and
    # clocks, which tick forever on video/music screens.
    stable = [(e.cls, e.text, e.rid, e.x, e.y) for e in _last
              if not re.search(r"SeekBar|ProgressBar", e.cls)
              and not re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", e.text)]
    _last_hash = hashlib.md5(repr((_last_app, stable)).encode()).hexdigest()
    return _last


def _foreground() -> str:
    try:
        ps = _call("GET", "/phone_state")
        return (ps or {}).get("packageName") or "unknown"
    except RuntimeError:
        return "unknown"


def _check_app(expect_app: str | None) -> None:
    if expect_app:
        fg = _foreground()
        if expect_app.lower() not in fg.lower():
            raise RuntimeError(f"Refusing: foreground app is {fg}, expected {expect_app}.")


def _render(filter: str | None = None, limit: int = 120) -> str:
    lines = [e.line() for e in _last]
    if filter:
        lines = [l for l in lines if filter.lower() in l.lower()]
    more = f"\n… {len(lines) - limit} more (use filter=)" if len(lines) > limit else ""
    return f"app: {_last_app} | {len(_last)} elements\n" + "\n".join(lines[:limit]) + more


def _settle(timeout: float = 3.0) -> None:
    """Wait until the UI changes from the pre-action state and then holds still briefly."""
    before, end = _last_hash, time.time() + timeout
    changed, prev = False, None
    while time.time() < end:
        time.sleep(0.12)
        _read()
        if _last_hash != before:
            changed = True
        if changed and _last_hash == prev:
            return
        prev = _last_hash


def _after(filter: str | None = None, show: bool = True, timeout: float = 3.0) -> str:
    _settle(timeout)
    return _render(filter) if show else f"app: {_last_app}"


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


def _wait_el(pred, timeout: float = 8, what: str = "element") -> Element:
    end = time.time() + timeout
    while True:
        hit = next((e for e in _read() if pred(e)), None)
        if hit:
            return hit
        if time.time() > end:
            raise RuntimeError(f"Timed out after {timeout}s waiting for {what}.\n" + _render())
        time.sleep(0.12)


def _wait_app(pkg: str, timeout: float = 8) -> None:
    end = time.time() + timeout
    while _foreground() != pkg:
        if time.time() > end:
            raise RuntimeError(f"{pkg} didn't come to the front (foreground: {_foreground()}).")
        time.sleep(0.12)


# ---------- low-level actions ----------

def _tap(x: int, y: int) -> None:
    _call("POST", "/tap", {"x": int(x), "y": int(y)})


def _swipe(x1, y1, x2, y2, ms: float = 300) -> None:
    _call("POST", "/swipe", {"startX": int(x1), "startY": int(y1), "endX": int(x2),
                             "endY": int(y2), "duration": float(ms)})


def _long_press(x: int, y: int, ms: float = 650) -> None:
    # Portal has no long-press route; a stationary swipe is a long press.
    _swipe(x, y, x, y, ms)


def _type(text: str, clear: bool = False) -> None:
    payload = {"base64_text": base64.b64encode(text.encode()).decode(), "clear": clear}
    try:
        _call("POST", "/keyboard/input", payload)
    except RuntimeError as e:
        # Without Portal's own keyboard selected, input goes through accessibility and
        # needs a focused field: focus the (first) editable field and retry once.
        if "IME not active" not in str(e):
            raise
        field = next((el for el in _read() if el.editable), None)
        if field is None:
            raise RuntimeError("Nothing to type into: no text field on screen.") from e
        _tap(field.x, field.y)
        time.sleep(0.3)
        _call("POST", "/keyboard/input", payload)


def _deep_link(url: str, package: str | None = None) -> None:
    payload = {"deepLink": url}
    if package:
        payload["package"] = package
    _call("POST", "/app/deep-link", payload)


# ---------- basic tools ----------

@mcp.tool()
def connect() -> str:
    """Check the phone link and report Portal version, screen size and foreground app."""
    ver = _call("GET", "/version")
    _read()
    return (f"Portal {ver} at {PORTAL_URL} | screen {_screen[0]}x{_screen[1]} | "
            f"foreground: {_last_app}")


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
    png = _call("GET", "/screenshot", params={"hideOverlay": "true"}, raw=True, timeout=20)
    if not png.startswith(b"\x89PNG"):
        png = base64.b64decode(json.loads(png).get("result", ""))
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
    _long_press(px, py) if long else _tap(px, py)
    return f"tapped {label!r} at {px},{py}\n" + _after(filter, show)


@mcp.tool()
def type_text(text: str, target: str | None = None, clear: bool = False,
              submit: bool = False, expect_app: str | None = None,
              filter: str | None = None, show: bool = True) -> str:
    """Type text (any language/accents/emoji), optionally tapping a field first (number or label).

    clear=True empties the field first; submit=True presses Enter afterwards.
    Returns the updated screen.
    """
    _check_app(expect_app)
    if target is not None:
        px, py, _ = _resolve(target, None, None)
        _tap(px, py)
        time.sleep(0.25)
    _type(text, clear)
    if submit:
        _call("POST", "/keyboard/key", {"key_code": TEXT_KEYS["enter"]})
    return f"typed {len(text)} chars\n" + _after(filter, show)


@mcp.tool()
def key(name: str, filter: str | None = None, show: bool = True) -> str:
    """Press a key: back, home, recents, notifications, quick_settings, lock, enter, tab,
    delete, search, menu, play_pause, play, pause, next, previous, volume_up, volume_down,
    power. Returns the updated screen."""
    n = name.lower()
    if n in GLOBAL_ACTIONS:
        _call("POST", "/global", {"action": GLOBAL_ACTIONS[n]})
    elif n in TEXT_KEYS:
        _call("POST", "/keyboard/key", {"key_code": TEXT_KEYS[n]})
    elif n in ADB_KEYS:
        _adb_shell(f"input keyevent {ADB_KEYS[n]}")
    else:
        raise RuntimeError(f"Unknown key {name!r}. Options: "
                           f"{', '.join([*GLOBAL_ACTIONS, *TEXT_KEYS, *ADB_KEYS])}")
    return f"pressed {name}\n" + _after(filter, show)


@mcp.tool()
def scroll(direction: str = "down", until: str | None = None, max_swipes: int = 8,
           filter: str | None = None) -> str:
    """Scroll up/down/left/right once, or repeatedly until an element containing `until`
    appears. Returns the updated screen."""
    _read()
    w, h = _screen
    cx, cy = w // 2, h // 2
    vec = {"down": (cx, h * .75, cx, h * .3), "up": (cx, h * .3, cx, h * .75),
           "right": (w * .8, cy, w * .2, cy), "left": (w * .2, cy, w * .8, cy)}
    if direction not in vec:
        raise RuntimeError("direction must be up, down, left or right")
    for i in range(max_swipes if until else 1):
        if until:
            try:
                e = _find(until, _last)
                return f"found [{e.idx}] {e.label!r} after {i} swipes\n" + _render(filter)
            except RuntimeError:
                pass
        _swipe(*vec[direction], ms=250)
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
        time.sleep(0.12)
    return f"timed out after {timeout}s waiting for {text!r}{' to disappear' if gone else ''}"


def _package(name: str) -> str:
    pkgs = _call("GET", "/packages")
    if isinstance(pkgs, dict):
        pkgs = pkgs.get("packages", [])
    names = [p.get("packageName", p) if isinstance(p, dict) else p for p in pkgs]
    labels = {(p.get("packageName"), (p.get("label") or "").lower())
              for p in pkgs if isinstance(p, dict)}
    if name in names:
        return name
    n = name.lower().replace(" ", "")
    by_label = [pk for pk, lb in labels if lb.replace(" ", "") == n]
    if by_label:
        return by_label[0]
    match = next((p for p in sorted(names, key=len) if n in p.lower().replace("_", "")), None)
    if not match:
        raise RuntimeError(f"No installed app matches {name!r}.")
    return match


@mcp.tool()
def open_app(name: str, filter: str | None = None, show: bool = True) -> str:
    """Launch an app by package name, app label, or a word in its package (e.g. 'youtube').
    Returns the app's first screen."""
    pkg = _package(name)
    _call("POST", "/app", {"package": pkg})
    _wait_app(pkg)
    return f"launched {pkg}\n" + _after(filter, show, timeout=4)


@mcp.tool()
def open_url(url: str, package: str | None = None, filter: str | None = None,
             show: bool = True) -> str:
    """Open a URL or deep link, optionally in a specific app (e.g. com.google.android.youtube).
    Returns the resulting screen."""
    _deep_link(url, package)
    return f"opened {url}\n" + _after(filter, show, timeout=5)


@mcp.tool()
def current_app() -> str:
    """Return the package of the app currently in the foreground."""
    return _foreground()


@mcp.tool()
def adb_shell(command: str) -> str:
    """Run a shell command on the phone over adb (needs Wi-Fi + adb). For what the other
    tools don't cover."""
    return _adb_shell(command, timeout=60)[-8000:]


# ---------- recipes ----------

def _media_uri(path: str) -> str:
    name = path.rsplit("/", 1)[-1].replace("'", "''")
    q = (f"content query --uri content://media/external/images/media "
         f"--projection _id --where \"_display_name='{name}'\"")
    ids = re.findall(r"_id=(\d+)", _adb_shell(q))
    if not ids:
        # Not indexed yet: ask the media scanner, then retry once.
        _adb_shell(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
                   f"-d {shlex.quote('file://' + path)}")
        time.sleep(1.5)
        ids = re.findall(r"_id=(\d+)", _adb_shell(q))
    if not ids:
        raise RuntimeError(f"Image not found in the media store: {path}")
    return f"content://media/external/images/media/{ids[-1]}"


def _contact_number(name: str) -> str | None:
    """Digits of the contact's only phone number, or None if missing/ambiguous/no adb."""
    q = name.replace("'", "''")
    try:
        out = _adb_shell("content query --uri content://com.android.contacts/data/phones "
                         f"--projection data1 --where \"display_name='{q}'\"")
    except RuntimeError:
        return None
    nums = {re.sub(r"\D", "", m) for m in re.findall(r"data1=([^,\n]+)", out)}
    nums = {("55" + n if len(n) in (10, 11) else n) for n in nums if len(n) >= 10}
    return nums.pop() if len(nums) == 1 else None


def _is_send(e: Element) -> bool:
    return e.rid == "send" or bool(re.fullmatch(r"(?i)enviar|send", e.desc))


@mcp.tool()
def whatsapp_send(contact: str, text: str = "", image_path: str | None = None,
                  confirm: bool = False) -> str:
    """Send a WhatsApp message and/or image (with `text` as caption) to a chat by its exact name.

    Without confirm=True it stages everything and stops before Send, returning the
    staged screen for you to show the user. Only pass confirm=True after the user
    approved sending this message to this contact. Images need adb (Wi-Fi).
    """
    number = _contact_number(contact)
    if image_path:
        cmd = (f"am start -a android.intent.action.SEND -t image/* --eu "
               f"android.intent.extra.STREAM {_media_uri(image_path)} "
               f"--grant-read-uri-permission -p com.whatsapp")
        if number:
            # Jump straight into this contact's chat, no picker.
            cmd += f" -e jid {number}@s.whatsapp.net"
        if text:
            cmd += f" --es android.intent.extra.TEXT {shlex.quote(text)}"
        _adb_shell(cmd)
        if not number:
            _pick_in_share_sheet(contact)
    elif number:
        _deep_link(f"https://wa.me/{number}?text={urllib.parse.quote(text)}", "com.whatsapp")
    else:
        _open_chat_by_search(contact)
        entry = _wait_el(lambda e: e.rid == "entry", 6, "the message box")
        _tap(entry.x, entry.y)
        time.sleep(0.2)
        _type(text, clear=True)
    send = _wait_el(_is_send, 10, "WhatsApp's Send button")
    # Make sure the staged screen is addressed to the right chat before going further.
    shown = " ".join(e.text + " " + e.desc for e in _last).lower()
    if contact.lower() not in shown:
        raise RuntimeError(f"Staged screen doesn't mention {contact!r}; not sending.\n" + _render())
    # In the share picker, every ticked row must be exactly this contact.
    ticked = [e.desc for e in _last if e.checked and e.desc]
    if any(t.strip().lower() != contact.strip().lower() for t in ticked):
        raise RuntimeError(f"Other chats are selected too ({ticked}); not sending.\n" + _render())
    staged = (f"STAGED for {contact!r}{' via ' + number if number else ''}: text={text!r} "
              f"image={image_path or '-'}")
    if not confirm:
        return staged + "\nNot sent. Retry with confirm=True once the user approves.\n" + _render()
    _tap(send.x, send.y)
    end = time.time() + 5
    while time.time() < end:
        time.sleep(0.15)
        if not any(_is_send(e) and e.x == send.x and e.y == send.y for e in _read()):
            return staged + "\nSENT."
    return staged + "\nTapped Send; Send button still visible, check.\n" + _render(limit=25)


def _search_and_type(contact: str) -> None:
    search = _wait_el(lambda e: e.clickable and re.search(
        r"pesquis|search", e.text + e.desc + e.rid, re.I), 8, "WhatsApp's search")
    _tap(search.x, search.y)
    field = _wait_el(lambda e: e.editable, 4, "the search field")
    _tap(field.x, field.y)
    time.sleep(0.2)
    _type(contact, clear=True)


def _pick_result(contact: str, min_y: int = 250) -> None:
    c = contact.strip().lower()
    # Chat-list rows carry the name as text; share-picker rows as a RadioButton description.
    row = _wait_el(lambda e: (e.text.strip().lower() == c or e.desc.strip().lower() == c)
                   and not e.editable and e.y > min_y, 5, f"a chat named {contact!r}")
    _tap(row.x, row.y)


def _pick_in_share_sheet(contact: str) -> None:
    _wait_app("com.whatsapp")
    _search_and_type(contact)
    _pick_result(contact)


def _open_chat_by_search(contact: str) -> None:
    _call("POST", "/app", {"package": "com.whatsapp"})
    _wait_app("com.whatsapp")
    _search_and_type(contact)
    _pick_result(contact)


def _downloads() -> list[str]:
    # toybox ls escapes spaces ("a\ b") when not on a tty; undo that.
    return [re.sub(r"\\(.)", r"\1", f) for f in _adb_shell("ls -1 /sdcard/Download").split("\n") if f]


@mcp.tool()
def download_image(query: str, pick: int = 1) -> str:
    """Search Google Images in Chrome for `query` and download result number `pick`
    (1 = first) via Chrome's long-press menu. Returns the saved file path. Needs adb."""
    before = set(_downloads())
    _deep_link("https://www.google.com/search?tbm=isch&q=" + urllib.parse.quote_plus(query),
               "com.android.chrome")
    _wait_app("com.android.chrome")
    w = _screen[0]
    imgs, end = [], time.time() + 15
    while time.time() < end:
        # Chrome exposes Google Images results as large Buttons/Images titled by the page.
        imgs = [e for e in _read() if re.search(r"(Button|Image)$", e.cls) and e.w > w * 0.3
                and e.h > 200 and e.y > 600]
        if len(imgs) >= pick:
            break
        time.sleep(0.2)
    if len(imgs) < pick:
        raise RuntimeError(f"Found only {len(imgs)} image results. Use screenshot() to check.")
    img = sorted(imgs, key=lambda e: (e.y, e.x))[pick - 1]
    _long_press(img.x, img.y)
    item = _wait_el(lambda e: re.fullmatch(
        r"(?i)baixar imagem|download image|fazer download da imagem", e.text), 4,
        "Chrome's download option")
    _tap(item.x, item.y)
    end = time.time() + 10
    while time.time() < end:
        time.sleep(0.3)
        # Chrome asks "Download again?" when a same-named file was fetched before.
        again = next((e for e in _read() if e.clickable and re.search(
            r"(?i)download novamente|download again|baixar novamente", e.text)), None)
        if again:
            _tap(again.x, again.y)
        new = [f for f in _downloads() if f not in before]
        if new:
            return f"/sdcard/Download/{new[0]}\n(downloaded: {img.desc or img.text or 'image'})"
    raise RuntimeError("Tapped download but no new file appeared in /sdcard/Download.")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
