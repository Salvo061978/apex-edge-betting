#!/usr/bin/env python3
"""
APEX EDGE — Server Locale & Archivio Unico Centrale (Zero Dipendenze Esterne)
- Serve index.html in locale (http://127.0.0.1:8787) e via Cloudflare Quick Tunnel
- Mantiene un unico archivio persistente in data/apex_archive.json (PC + Smartphone sincronizzati)
- Legge le credenziali solo dal file .env locale (Telegram + Betfair)
- Espone proxy server-side per Betfair / ESPN (zero errori CORS) e invio Telegram sicuro
"""

import argparse
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ARCHIVE_FILE = DATA_DIR / "apex_archive.json"
INDEX_FILE = BASE_DIR / "index.html"
ENV_FILE = BASE_DIR / ".env"

ARCHIVE_LOCK = threading.Lock()
TUNNEL_URL = ""
TUNNEL_PROC = None

ALLOWED_PROXY_HOSTS = {
    "ips.betfair.it",
    "ips.betfair.com",
    "ero.betfair.it",
    "ero.betfair.com",
    "www.betfair.it",
    "site.api.espn.com",
    "www.thesportsdb.com",
}


def resolve_env_path(path: Path = ENV_FILE) -> Path:
    if path.exists():
        return path
    alt = path.parent / ".env.txt"
    if alt.exists():
        print("[!] Trovato '.env.txt' invece di '.env' (estensione nascosta di Windows): lo carico comunque.")
        return alt
    return path


def load_env_file(path: Path = ENV_FILE, override: bool = False) -> dict:
    env = {}
    actual = resolve_env_path(path)
    if not actual.exists():
        return env
    for raw_line in actual.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, val = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        val = val.strip()
        if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].strip()
        env[key] = val
        if override or not os.environ.get(key):
            os.environ[key] = val
    return env


def env_get(key: str, default: str = "") -> str:
    return str(os.environ.get(key, default) or "").strip()


def _empty_archive() -> dict:
    return {
        "version": 1,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "prematch": [],
        "live": [],
        "deletedLiveIds": [],
    }


def load_archive() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not ARCHIVE_FILE.exists():
        return _empty_archive()
    try:
        data = json.loads(ARCHIVE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty_archive()
        data.setdefault("version", 1)
        data.setdefault("prematch", [])
        data.setdefault("live", [])
        data.setdefault("deletedLiveIds", [])
        return data
    except Exception:
        return _empty_archive()


def save_archive_atomic(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data["updatedAt"] = datetime.now(timezone.utc).isoformat()
    tmp_path = ARCHIVE_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, ARCHIVE_FILE)


def _outcome_rank(outcome: str) -> int:
    o = str(outcome or "").upper()
    if o in ("WON", "LOST", "VOID", "SAVED"):
        return 3
    if o == "PENDING":
        return 1
    return 0


def _status_rank(status: str) -> int:
    s = str(status or "").upper()
    if s == "FT":
        return 3
    if s == "LIVE":
        return 2
    if s == "PRE-MATCH":
        return 1
    return 0


def merge_archives(server_data: dict, incoming: dict) -> dict:
    deleted_live = set(server_data.get("deletedLiveIds") or [])
    for did in (incoming.get("deletedLiveIds") or []):
        if isinstance(did, str) and did:
            deleted_live.add(did)

    # 1) Merge LIVE_SIGNALS_HISTORY by id
    live_map = {}
    for item in (server_data.get("live") or []):
        if isinstance(item, dict) and item.get("id") and item["id"] not in deleted_live:
            live_map[item["id"]] = item

    for item in (incoming.get("live") or []):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        iid = item["id"]
        if iid in deleted_live:
            continue
        if iid not in live_map:
            live_map[iid] = item
        else:
            prev = live_map[iid]
            new_r = _outcome_rank(item.get("outcome"))
            old_r = _outcome_rank(prev.get("outcome"))
            if new_r > old_r:
                merged = {**prev, **item}
                merged["telegramNotified"] = bool(prev.get("telegramNotified") or item.get("telegramNotified"))
                live_map[iid] = merged
            elif new_r == old_r:
                merged = {**prev}
                if item.get("telegramNotified"):
                    merged["telegramNotified"] = True
                if (item.get("recordedAt") or 0) > (prev.get("recordedAt") or 0):
                    merged["recordedAt"] = item["recordedAt"]
                if item.get("finalScore") and item.get("finalScore") != "IN CORSO" and prev.get("finalScore") in (None, "", "IN CORSO"):
                    merged["finalScore"] = item["finalScore"]
                live_map[iid] = merged

    # 2) Merge PREMATCH_GAMES (graded / synced) by id
    pm_map = {}
    for g in (server_data.get("prematch") or []):
        if isinstance(g, dict) and g.get("id"):
            pm_map[g["id"]] = g

    for g in (incoming.get("prematch") or []):
        if not isinstance(g, dict) or not g.get("id"):
            continue
        gid = g["id"]
        if gid not in pm_map:
            pm_map[gid] = g
        else:
            prev = pm_map[gid]
            new_score = (_outcome_rank(g.get("outcome")), _status_rank(g.get("status")))
            old_score = (_outcome_rank(prev.get("outcome")), _status_rank(prev.get("status")))
            if new_score > old_score:
                pm_map[gid] = {**prev, **g}
            elif new_score == old_score and g.get("postMortem") and not prev.get("postMortem"):
                pm_map[gid] = {**prev, **g}

    merged_live = list(live_map.values())
    merged_live.sort(key=lambda x: (x.get("recordedAt") or 0), reverse=True)
    merged_pm = list(pm_map.values())

    return {
        "version": 1,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "prematch": merged_pm,
        "live": merged_live,
        "deletedLiveIds": sorted(deleted_live)[-300:],
    }


def _clean_tg_override(val: str) -> str:
    s = str(val or "").strip()
    if s.upper() in ("__ENV__", "ENV"):
        return ""
    return s


def _is_ssl_cert_error(exc: Exception) -> bool:
    msg = str(exc or "")
    reason = getattr(exc, "reason", None)
    return (
        isinstance(exc, ssl.SSLError)
        or isinstance(reason, ssl.SSLError)
        or "CERTIFICATE_VERIFY_FAILED" in msg
        or "self-signed certificate" in msg.lower()
        or "unable to get local issuer" in msg.lower()
    )


def _http_request_with_os_trust(url: str, data: bytes = None, headers: dict = None, method: str = "GET", timeout: int = 10):
    """Esegue una richiesta HTTPS con urllib; se Windows/Antivirus causa CERTIFICATE_VERIFY_FAILED,
    usa il curl di sistema (Windows Schannel = archivio certificati nativo Windows)."""
    hdrs = dict(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), (resp.headers.get("Content-Type") or "application/octet-stream")
    except urllib.error.HTTPError as he:
        return he.code, he.read(), (he.headers.get("Content-Type") or "application/json")
    except Exception as exc:
        if not _is_ssl_cert_error(exc):
            raise
        curl_bin = shutil.which("curl.exe") or shutil.which("curl")
        if not curl_bin:
            raise
        cmd = [curl_bin, "-sS", "--max-time", str(timeout), "-X", method]
        if os.name == "nt":
            cmd.append("--ssl-no-revoke")
        for k, v in hdrs.items():
            cmd.extend(["-H", f"{k}: {v}"])
        if data is not None:
            cmd.extend(["--data-binary", "@-"])
        cmd.extend(["-w", "\n%{http_code}\n%{content_type}", url])
        proc = subprocess.run(
            cmd,
            input=data if data is not None else b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 5,
        )
        if proc.returncode != 0:
            err_txt = proc.stderr.decode("utf-8", errors="replace").strip() or f"curl exit {proc.returncode}"
            raise RuntimeError(f"SSL fallback (curl) fallito: {err_txt}")
        parts = proc.stdout.rsplit(b"\n", 2)
        if len(parts) != 3:
            raise RuntimeError("Risposta curl non valida")
        body_bytes, status_b, ctype_b = parts
        status_code = int(status_b.decode("ascii", errors="ignore").strip() or "0")
        ctype = ctype_b.decode("utf-8", errors="replace").strip() or "application/octet-stream"
        return status_code, body_bytes, ctype


def send_telegram_via_env(html_text: str, inline_buttons=None, override_token="", override_chat="", override_thread="") -> dict:
    if not env_get("TELEGRAM_BOT_TOKEN") or not env_get("TELEGRAM_CHAT_ID"):
        load_env_file(ENV_FILE, override=True)

    clean_override_tok = _clean_tg_override(override_token)
    clean_override_chat = _clean_tg_override(override_chat)
    clean_override_thread = _clean_tg_override(override_thread)

    token = (env_get("TELEGRAM_BOT_TOKEN") or clean_override_tok or "").strip()
    token = re.sub(r"^bot", "", token, flags=re.IGNORECASE)
    token = re.sub(r"\s+", "", token)
    chat_id = (env_get("TELEGRAM_CHAT_ID") or clean_override_chat or "").strip()
    chat_id = re.sub(r"\s+", "", chat_id)
    thread_id = (env_get("TELEGRAM_THREAD_ID") or clean_override_thread or "").strip()

    if not token or not chat_id:
        err = "TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID mancanti nel file .env (o nel pannello browser)"
        print(f"[Telegram] ❌ {err}")
        return {"ok": False, "error": err}

    api_url = f"https://api.telegram.org/bot{token}/sendMessage"

    def _post(payload_obj: dict) -> dict:
        body = json.dumps(payload_obj).encode("utf-8")
        try:
            status_code, raw_bytes, _ = _http_request_with_os_trust(
                api_url,
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "APEX-EDGE-Local/1.0"},
                method="POST",
                timeout=10,
            )
            raw = raw_bytes.decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except Exception:
                return {"ok": False, "status": status_code, "error": f"HTTP {status_code}: {raw[:160]}"}
            if status_code >= 400 or not data.get("ok"):
                return {"ok": False, "status": status_code, "error": data.get("description") or f"HTTP {status_code}"}
            return {"ok": True, "data": data, "error": None}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    payload = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if thread_id.isdigit():
        payload["message_thread_id"] = int(thread_id)
    if isinstance(inline_buttons, list) and len(inline_buttons) > 0:
        payload["reply_markup"] = {"inline_keyboard": inline_buttons}

    res = _post(payload)
    if not res.get("ok") and re.search(r"parse entities|can't find end|unsupported start tag|BUTTON_URL_INVALID", str(res.get("error") or ""), re.IGNORECASE):
        fb = {
            "chat_id": chat_id,
            "text": re.sub(r"<[^>]+>", "", str(html_text or "")),
            "disable_web_page_preview": True,
        }
        if "message_thread_id" in payload:
            fb["message_thread_id"] = payload["message_thread_id"]
        if "BUTTON_URL_INVALID" not in str(res.get("error") or "").upper() and "reply_markup" in payload:
            fb["reply_markup"] = payload["reply_markup"]
        res = _post(fb)
    if res.get("ok"):
        print(f"[Telegram] ✅ Messaggio inviato correttamente (chat_id={chat_id})")
    else:
        print(f"[Telegram] ❌ Errore invio Telegram: {res.get('error')}")
    return res


class ApexHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        ts = datetime.now().strftime("%H:%M:%S")
        sys.stdout.write(f"[{ts}] {self.address_string()} {fmt % args}\n")
        sys.stdout.flush()

    def _send_json(self, status: int, obj: dict):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Apex-Pin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 25 * 1024 * 1024:
            raise ValueError("Payload troppo grande")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _check_pin(self) -> bool:
        pin = env_get("APEX_ACCESS_PIN")
        if not pin:
            return True
        hdr = (self.headers.get("X-Apex-Pin") or "").strip()
        if hdr == pin:
            return True
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get("pin", [""])[0].strip() == pin:
            return True
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Apex-Pin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            if not INDEX_FILE.exists():
                self._send_json(404, {"ok": False, "error": "index.html non trovato"})
                return
            raw = INDEX_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/api/health":
            with ARCHIVE_LOCK:
                arc = load_archive()
            self._send_json(200, {
                "ok": True,
                "mode": "local-server",
                "hasEnvTelegram": bool(env_get("TELEGRAM_BOT_TOKEN") and env_get("TELEGRAM_CHAT_ID")),
                "hasEnvBetfair": bool(env_get("BETFAIR_APP_KEY") and env_get("BETFAIR_SSOID")),
                "pinRequired": bool(env_get("APEX_ACCESS_PIN")),
                "tunnelUrl": TUNNEL_URL,
                "counts": {
                    "prematch": len(arc.get("prematch") or []),
                    "live": len(arc.get("live") or []),
                },
                "updatedAt": arc.get("updatedAt"),
            })
            return

        if path == "/api/archive":
            if not self._check_pin():
                self._send_json(401, {"ok": False, "error": "PIN di accesso richiesto"})
                return
            with ARCHIVE_LOCK:
                arc = load_archive()
            self._send_json(200, {
                "ok": True,
                "prematch": arc.get("prematch") or [],
                "live": arc.get("live") or [],
                "deletedLiveIds": arc.get("deletedLiveIds") or [],
                "counts": {
                    "prematch": len(arc.get("prematch") or []),
                    "live": len(arc.get("live") or []),
                },
                "updatedAt": arc.get("updatedAt"),
            })
            return

        if path == "/api/archive/download":
            if not self._check_pin():
                self._send_json(401, {"ok": False, "error": "PIN di accesso richiesto"})
                return
            with ARCHIVE_LOCK:
                arc = load_archive()
            raw = json.dumps(arc, ensure_ascii=False, indent=2).encode("utf-8")
            fname = f"apex_archive_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/api/proxy":
            target = qs.get("url", [""])[0].strip()
            u = urllib.parse.urlparse(target)
            if u.scheme not in ("http", "https") or (u.hostname or "").lower() not in ALLOWED_PROXY_HOSTS:
                self._send_json(403, {"ok": False, "error": "Host non consentito nel proxy"})
                return
            try:
                status_code, raw, ctype = _http_request_with_os_trust(
                    target,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
                        "Accept": "application/json, text/plain, */*",
                    },
                    method="GET",
                    timeout=8,
                )
                self.send_response(status_code or 200)
                self.send_header("Content-Type", ctype or "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(raw)
            except Exception as exc:
                self._send_json(502, {"ok": False, "error": f"Proxy error: {exc}"})
            return

        self._send_json(404, {"ok": False, "error": "Endpoint non trovato"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if not self._check_pin():
            self._send_json(401, {"ok": False, "error": "PIN di accesso richiesto"})
            return

        try:
            body = self._read_json_body()
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": f"JSON non valido: {exc}"})
            return

        if path == "/api/archive/sync":
            with ARCHIVE_LOCK:
                current = load_archive()
                merged = merge_archives(current, body)
                save_archive_atomic(merged)
            self._send_json(200, {
                "ok": True,
                "prematch": merged["prematch"],
                "live": merged["live"],
                "deletedLiveIds": merged["deletedLiveIds"],
                "counts": {
                    "prematch": len(merged["prematch"]),
                    "live": len(merged["live"]),
                },
                "updatedAt": merged["updatedAt"],
            })
            return

        if path == "/api/telegram/send":
            res = send_telegram_via_env(
                html_text=str(body.get("text") or ""),
                inline_buttons=body.get("inlineButtons"),
                override_token=str(body.get("botToken") or ""),
                override_chat=str(body.get("chatId") or ""),
                override_thread=str(body.get("threadId") or ""),
            )
            self._send_json(200 if res.get("ok") else 502, res)
            return

        if path == "/api/betfair/rpc":
            app_key = env_get("BETFAIR_APP_KEY") or str(body.get("appKey") or "").strip()
            ssoid = env_get("BETFAIR_SSOID") or str(body.get("ssoid") or "").strip()
            rpc_payload = body.get("payload")
            if not app_key or not ssoid or rpc_payload is None:
                self._send_json(400, {"ok": False, "error": "BETFAIR_APP_KEY, BETFAIR_SSOID o payload mancanti"})
                return
            try:
                status_code, raw_bytes, _ = _http_request_with_os_trust(
                    "https://api.betfair.com/exchange/betting/json-rpc/v1",
                    data=json.dumps(rpc_payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "X-Application": app_key,
                        "X-Authentication": ssoid,
                    },
                    method="POST",
                    timeout=10,
                )
                raw = raw_bytes.decode("utf-8", errors="replace")
                if status_code >= 400:
                    self._send_json(502, {"ok": False, "error": f"HTTP {status_code}: {raw[:160]}"})
                else:
                    self._send_json(200, {"ok": True, "result": json.loads(raw)})
            except Exception as exc:
                self._send_json(502, {"ok": False, "error": str(exc)})
            return

        self._send_json(404, {"ok": False, "error": "Endpoint POST non trovato"})


def find_cloudflared() -> str:
    custom = env_get("CLOUDFLARED_PATH")
    if custom and (Path(custom).exists() or shutil.which(custom)):
        return custom
    for candidate in (BASE_DIR / "cloudflared.exe", BASE_DIR / "cloudflared"):
        if candidate.exists():
            return str(candidate)
    found = shutil.which("cloudflared")
    return found or ""


def start_cloudflare_quick_tunnel(port: int):
    global TUNNEL_URL, TUNNEL_PROC
    cf_bin = find_cloudflared()
    if not cf_bin:
        print("\n[!] cloudflared non trovato. L'app resta comunque attiva in locale su http://127.0.0.1:%d" % port)
        return

    cmd = [cf_bin, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"]
    print(f"[Cloudflare] Avvio Quick Tunnel: {' '.join(cmd)}")
    try:
        TUNNEL_PROC = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        print(f"[Cloudflare] Errore avvio cloudflared: {exc}")
        return

    url_re = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
    notified = False
    for line in TUNNEL_PROC.stdout:
        m = url_re.search(line)
        if m and not notified:
            TUNNEL_URL = m.group(0)
            notified = True
            print("\n" + "=" * 64)
            print(f"  🌐 APEX EDGE ONLINE (PC + SMARTPHONE): {TUNNEL_URL}")
            print("=" * 64 + "\n")
            if env_get("SEND_TUNNEL_URL_TO_TELEGRAM", "true").lower() != "false":
                if env_get("TELEGRAM_BOT_TOKEN") and env_get("TELEGRAM_CHAT_ID"):
                    msg = (
                        "🌐 <b>APEX EDGE Server Locale Attivo!</b>\n"
                        "📦 Archivio Unico sincronizzato (PC + Smartphone).\n\n"
                        f"👉 <a href=\"{TUNNEL_URL}\">{TUNNEL_URL}</a>"
                    )
                    res = send_telegram_via_env(msg, [[{"text": "📲 APRI APEX EDGE", "url": TUNNEL_URL}]])
                    if res.get("ok"):
                        print("[Telegram] Link Cloudflare inviato automaticamente su Telegram!")
                    else:
                        print(f"[Telegram] Avviso: impossibile inviare link ({res.get('error')})")


def main():
    parser = argparse.ArgumentParser(description="APEX EDGE Local Server & Unified Archive")
    parser.add_argument("--port", type=int, default=0, help="Porta HTTP locale (default da .env o 8787)")
    parser.add_argument("--tunnel", action="store_true", help="Avvia anche Cloudflare Quick Tunnel")
    args = parser.parse_args()

    load_env_file(ENV_FILE)
    port = args.port or int(env_get("PORT", "8787") or 8787)

    with ARCHIVE_LOCK:
        arc = load_archive()
        save_archive_atomic(arc)

    httpd = ThreadingHTTPServer(("0.0.0.0", port), ApexHandler)
    print("=" * 64)
    print(f"  ⚡ APEX EDGE Local Server attivo su http://127.0.0.1:{port}")
    print(f"  📦 Archivio Unico: {ARCHIVE_FILE} ({len(arc['prematch'])} Pre-Match, {len(arc['live'])} Live)")
    print(f"  🔐 .env Telegram: {'OK' if (env_get('TELEGRAM_BOT_TOKEN') and env_get('TELEGRAM_CHAT_ID')) else 'Non configurato'}")
    print(f"  🔐 .env Betfair:  {'OK' if (env_get('BETFAIR_APP_KEY') and env_get('BETFAIR_SSOID')) else 'Non configurato'}")
    print("=" * 64)

    if args.tunnel:
        t = threading.Thread(target=start_cloudflare_quick_tunnel, args=(port,), daemon=True)
        t.start()

    def _shutdown(signum, frame):
        print("\n[APEX EDGE] Chiusura server in corso...")
        if TUNNEL_PROC and TUNNEL_PROC.poll() is None:
            TUNNEL_PROC.terminate()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        httpd.serve_forever()
    finally:
        if TUNNEL_PROC and TUNNEL_PROC.poll() is None:
            TUNNEL_PROC.terminate()
        httpd.server_close()


if __name__ == "__main__":
    main()
