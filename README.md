# phone-mcp

MCP server that lets Claude drive this Android phone through the Mobilerun
Portal accessibility service's local HTTP API: ~15 ms per screen read, no adb
or Wi-Fi needed for normal control. Returns the updated screen after every
action. adb is only a fallback (image sharing intents, media keys, reviving
Portal if Android kills it).

Tools: connect, screen, screenshot, tap, type_text, key, scroll, wait_for,
open_app, open_url, current_app, adb_shell.
Recipes: whatsapp_send (stages; sends only with confirm=True), download_image.

Safety: `tap` refuses send/submit/pay/delete-style labels (EN + PT) unless
`confirm=True`; `expect_app` refuses to act in the wrong app.

Setup:
1. Install Mobilerun Portal (https://github.com/droidrun/mobilerun-portal/releases).
2. Enable its accessibility service. Append it to `enabled_accessibility_services`;
   don't replace the list (`mobilerun setup` does, and switches other services off).
3. Enable its HTTP server (app settings, or
   `content insert --uri content://com.mobilerun.portal/toggle_socket_server --bind enabled:b:true --bind port:i:8080`).
4. Put `PORTAL_TOKEN=<token>` in `~/.config/phone-mcp/portal.env`
   (token: `content query --uri content://com.mobilerun.portal/auth_token`).

    claude mcp add -s user phone -- /root/phone-mcp/.venv/bin/python /root/phone-mcp/phone_mcp.py

Env: PORTAL_URL (default http://127.0.0.1:8080), PORTAL_TOKEN, PHONE_ADB,
PHONE_ADB_SERIAL (default 127.0.0.1:5555).
Deps: mcp>=2, pillow.
