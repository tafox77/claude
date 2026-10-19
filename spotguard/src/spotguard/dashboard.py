"""Localhost dashboard (stdlib only).

* Binds 127.0.0.1 by default. Any other bind address requires SPOTGUARD_DASHBOARD_TOKEN and every
  request must then carry "Authorization: Bearer <token>".
* Control POSTs always require the X-Spotguard-Token header (a per-run token embedded in the page),
  which blocks cross-site form posts from other origins.
* Controls write to the database; the trading process applies them. Entry pause and flatten are
  separate actions and the page shows their actual state separately.
"""

from __future__ import annotations

import hmac
import html
import ipaddress
import json
import os
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from spotguard.storage import utcnow


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def snapshot(db_path: str, mode: str, policy_version: str) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    q = lambda sql, a=(): [dict(r) for r in conn.execute(sql, a)]  # noqa: E731
    risk = q("SELECT state FROM risk_state WHERE id = 1")
    state = {
        "mode": mode.upper(),
        "policy_version": policy_version,
        "generated_at": utcnow().isoformat(),
        "risk": json.loads(risk[0]["state"]) if risk else None,
        "cash": q("SELECT * FROM balances"),
        "positions": q("SELECT * FROM positions WHERE CAST(qty AS REAL) > 0"),
        "open_orders": q(
            "SELECT client_order_id, asset_key, side, purpose, qty, filled_qty, state, updated_at FROM orders "
            "WHERE state NOT IN ('FILLED','CANCELED','REJECTED') ORDER BY created_at DESC"
        ),
        "recent_fills": q("SELECT asset_key, side, qty, price, fee, at FROM fills ORDER BY at DESC LIMIT 20"),
        "equity": q("SELECT * FROM equity_marks ORDER BY at DESC LIMIT 1"),
        "fees_total": sum(float(r["fee"]) for r in q("SELECT fee FROM fills")),
        "realized_pnl": sum(float(r["realized_pnl"]) for r in q("SELECT realized_pnl FROM positions")),
        "alerts": q("SELECT at, level, message FROM alerts ORDER BY id DESC LIMIT 20"),
        "security": q("SELECT asset_key, verdict, created_at FROM security_reports ORDER BY created_at DESC LIMIT 20"),
        "models": q("SELECT model_id, status, created_at FROM models ORDER BY created_at DESC LIMIT 5"),
        "latency": q("SELECT stage, count(*) n FROM latency GROUP BY stage"),
        "controls": q("SELECT * FROM controls"),
    }
    conn.close()
    return state


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>spotguard</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="sg-token" content="{token}">
<style>body{{font-family:system-ui,sans-serif;margin:16px;background:#fff;color:#111}}
.banner{{font-size:22px;font-weight:700;padding:8px 12px;border-radius:6px;display:inline-block;background:{color};color:#fff}}
pre{{background:#f4f4f4;padding:8px;overflow:auto;max-width:100%}} button{{margin-right:8px;padding:6px 10px}}
@media (prefers-color-scheme: dark){{body{{background:#111;color:#eee}} pre{{background:#222}}}}</style></head>
<body><div class="banner">{mode}</div> <span>policy {policy}</span>
<p>Entry pause blocks new positions. Flatten cancels entry orders, reconciles, then exits all positions within the
reviewed emergency price-impact limit; positions that can't be sold are reported, not hidden.</p>
<button onclick="ctl('block_entries')">Pause entries</button><button onclick="ctl('unblock_entries')">Resume entries</button>
<button onclick="if(confirm('Flatten all positions?'))ctl('flatten')">Flatten</button>
<pre id="s">loading…</pre>
<script>
const tok=document.querySelector('meta[name=sg-token]').content;
async function load(){{const r=await fetch('/api/state');
document.getElementById('s').textContent=JSON.stringify(await r.json(),null,2)}}
async function ctl(a){{await fetch('/api/control',{{method:'POST',
headers:{{'X-Spotguard-Token':tok,'Content-Type':'application/json'}},
body:JSON.stringify({{action:a}})}});load()}}
load();setInterval(load,3000);
</script></body></html>"""


def make_server(db_path: str, mode: str, policy_version: str, host: str = "127.0.0.1", port: int = 8765,
                store=None) -> tuple[ThreadingHTTPServer, str]:  # fmt: skip
    remote_token = os.environ.get("SPOTGUARD_DASHBOARD_TOKEN", "")
    if not is_loopback(host) and len(remote_token) < 24:
        raise RuntimeError("binding the dashboard to a non-loopback address requires SPOTGUARD_DASHBOARD_TOKEN "
                           "(at least 24 characters)")  # fmt: skip
    csrf = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            if is_loopback(host):
                return True
            given = self.headers.get("Authorization", "")
            return hmac.compare_digest(given, f"Bearer {remote_token}")

        def _send(self, code: int, body: str, ctype: str = "application/json") -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                return self._send(401, '{"error":"unauthorized"}')
            if self.path == "/api/state":
                return self._send(200, json.dumps(snapshot(db_path, mode, policy_version), default=str))
            if self.path == "/":
                color = {"PAPER": "#2b6cb0", "SANDBOX": "#b7791f", "LIVE": "#c53030"}.get(mode.upper(), "#555")
                page = PAGE.format(token=csrf, mode=html.escape(mode.upper()), policy=html.escape(policy_version),
                                   color=color)  # fmt: skip
                return self._send(200, page, "text/html; charset=utf-8")
            return self._send(404, '{"error":"not found"}')

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                return self._send(401, '{"error":"unauthorized"}')
            if self.path != "/api/control" or not hmac.compare_digest(self.headers.get("X-Spotguard-Token", ""), csrf):
                return self._send(403, '{"error":"forbidden"}')
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 4096)
                action = json.loads(self.rfile.read(length) or b"{}").get("action")
            except (ValueError, AttributeError):
                return self._send(400, '{"error":"bad request"}')
            now = utcnow()
            if action == "block_entries":
                store.set_control("block_entries", "1", "dashboard", now)
            elif action == "unblock_entries":
                store.set_control("block_entries", "0", "dashboard", now)
            elif action == "flatten":
                store.set_control("flatten", "requested", "dashboard", now)
            else:
                return self._send(400, '{"error":"unknown action"}')
            return self._send(200, json.dumps({"ok": True, "action": action}))

        def log_message(self, fmt: str, *args) -> None:  # keep request logs out of stdout
            return

    return ThreadingHTTPServer((host, port), Handler), csrf
