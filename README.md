# phone-mcp

MCP server that lets Claude drive this Android phone. Reads the UI as text via
uiautomator2 (~1 s per read, resident helper app on the phone) instead of
screenshots, and returns the updated screen after every action.

Tools: connect, screen, screenshot, tap, type_text, key, scroll, wait_for,
open_app, open_url, current_app, adb_shell.
Recipes: whatsapp_send (stages; sends only with confirm=True), download_image.

Safety: `tap` refuses send/submit/pay/delete-style labels (EN + PT) unless
`confirm=True`; `expect_app` refuses to act in the wrong app.

Setup: Wireless debugging on, phone paired with Termux's adb
(`pkg install android-tools`, `adb pair`), then pin the port once per boot:
`adb connect <ip>:<wireless-port> && adb tcpip 5555`.

    claude mcp add -s user phone -- /root/phone-mcp/.venv/bin/python /root/phone-mcp/phone_mcp.py

Env: PHONE_ADB (adb path), PHONE_IP, PHONE_PORT (default 5555).
Deps: mcp>=2, uiautomator2, pillow.
