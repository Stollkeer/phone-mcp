# phone-mcp

MCP server that lets Claude drive this Android phone over adb, reading the UI as
text (uiautomator) instead of screenshots.

Tools: connect, screen, screenshot, tap, type_text, key, scroll, wait_for,
open_app, open_url, current_app, adb_shell.

Safety: `tap` refuses send/submit/pay/delete-style labels (EN + PT) unless
`confirm=True`; pass `expect_app` to refuse acting in the wrong app.

Setup: Wireless debugging on, phone paired with Termux's adb
(`pkg install android-tools`, `adb pair`). The server reconnects on its own by
scanning PHONE_IP (default 192.168.0.99) for the adb port.

    claude mcp add -s user phone -- /root/phone-mcp/.venv/bin/python /root/phone-mcp/phone_mcp.py

Env: PHONE_ADB (adb path), PHONE_IP.
Limits: adb `input text` is ASCII-only (accents are stripped).
