#!/usr/bin/env python3
"""Flow reverse proxy with country-block bypass for Render.

Routes:
- /flow/* or / -> https://flow.google.com/*
- /accounts/* -> https://accounts.google.com/*
- /apis/* -> https://apis.google.com/*

Patches batchexecute (cPZSdc flag 30 -> True) to bypass region check.
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.request
import urllib.error

PORT = int(os.environ.get("PORT", "8900"))

ROUTES = [
    ("/accounts/", "https://accounts.google.com/", "accounts.google.com"),
    ("/accounts", "https://accounts.google.com/", "accounts.google.com"),
    ("/apis/", "https://apis.google.com/", "apis.google.com"),
    ("/apis", "https://apis.google.com/", "apis.google.com"),
    ("/flow/", "https://flow.google.com/", "flow.google.com"),
    ("/flow", "https://flow.google.com/", "flow.google.com"),
    ("/", "https://flow.google.com/", "flow.google.com"),
]

BATCH_PATH = "/_/AiSandboxAngularFrontend/data/batchexecute"
TAG = "wrb.fr"
RPCID = "cPZSdc"
FLAG_INDEX = 30
MIN_LEN = 32

patched_total = 0

def patch_body(text):
    global patched_total
    lines = text.split("\n")
    changed_any = False
    for i, line in enumerate(lines):
        if not line.startswith("[["):
            continue
        try:
            arr = json.loads(line)
        except Exception:
            continue
        changed = False
        for chunk in arr:
            if not (isinstance(chunk, list) and len(chunk) >= 3):
                continue
            if chunk[0] != TAG or chunk[1] != RPCID:
                continue
            try:
                payload = json.loads(chunk[2])
            except Exception:
                continue
            if not isinstance(payload, list) or len(payload) < MIN_LEN:
                continue
            if payload[FLAG_INDEX] is not None and not isinstance(payload[FLAG_INDEX], bool):
                continue
            payload[FLAG_INDEX] = True
            chunk[2] = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            changed = True
            patched_total += 1
        if changed:
            new_line = json.dumps(arr, separators=(",", ":"), ensure_ascii=False)
            old_line = line[:-1] if line.endswith("\r") else line
            lines[i] = line.replace(old_line, new_line, 1)
            changed_any = True
    if changed_any:
        print(f"[proxy] PATCHED batchexecute (total: {patched_total})", flush=True)
    return "\n".join(lines)

def rewrite_urls(text):
    text = text.replace("https://flow.google.com", "/flow")
    text = text.replace("https://accounts.google.com", "/accounts")
    text = text.replace("https://apis.google.com", "/apis")
    text = text.replace('"//flow.google.com', '"/flow')
    text = text.replace('"//accounts.google.com', '"/accounts')
    return text

def fix_cookies(headers):
    out = []
    for k, v in headers:
        if k.lower() == "set-cookie":
            v = re.sub(r";\s*[Dd]omain=[^;]*", "", v)
            v = re.sub(r";\s*[Ss]ecure", "", v)
        out.append((k, v))
    return out

def rewrite_location(value):
    if not value:
        return value
    value = value.replace("https://flow.google.com", "/flow")
    value = value.replace("https://accounts.google.com", "/accounts")
    value = value.replace("https://apis.google.com", "/apis")
    return value

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self): self.proxy()
    def do_POST(self): self.proxy()
    def do_PUT(self): self.proxy()
    def do_DELETE(self): self.proxy()
    def do_PATCH(self): self.proxy()
    def do_HEAD(self): self.proxy()
    def do_OPTIONS(self): self.proxy()

    def proxy(self):
        # Route based on path prefix
        target_base = None
        target_host = None
        upstream_path = self.path
        for prefix, base, host in ROUTES:
            if self.path == prefix.rstrip("/") or self.path.startswith(prefix):
                target_base = base.rstrip("/")
                target_host = host
                # Strip prefix, keep remainder
                rest = self.path[len(prefix.rstrip("/")):]
                if not rest.startswith("/"):
                    rest = "/" + rest
                upstream_path = rest
                break
        if not target_base:
            target_base = "https://flow.google.com"
            target_host = "flow.google.com"

        target_url = target_base + upstream_path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else None

        req = urllib.request.Request(target_url, data=body, method=self.command)
        skip = {"host", "connection", "content-length", "transfer-encoding", "upgrade"}
        for k, v in self.headers.items():
            if k.lower() not in skip:
                req.add_header(k, v)
        req.add_header("Host", target_host)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "")

                # Patch batchexecute for flow.google.com
                if target_host == "flow.google.com" and BATCH_PATH in upstream_path:
                    if RPCID.encode() in data:
                        try:
                            data = patch_body(data.decode("utf-8", errors="replace")).encode("utf-8")
                        except Exception as e:
                            print(f"[proxy] patch error: {e}", flush=True)

                # Rewrite URLs in text responses
                if "text/html" in ctype or "javascript" in ctype or "text/plain" in ctype:
                    try:
                        text = data.decode("utf-8", errors="replace")
                        new_text = rewrite_urls(text)
                        if new_text != text:
                            data = new_text.encode("utf-8")
                    except Exception:
                        pass

                self.send_response(resp.status)
                headers = fix_cookies(list(resp.headers.items()))
                for k, v in headers:
                    kl = k.lower()
                    if kl in ("content-length", "transfer-encoding", "connection"):
                        continue
                    if kl == "location":
                        v = rewrite_location(v)
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            headers = fix_cookies(list(e.headers.items()))
            for k, v in headers:
                kl = k.lower()
                if kl in ("content-length", "transfer-encoding", "connection"):
                    continue
                if kl == "location":
                    v = rewrite_location(v)
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            print(f"[proxy] error {self.command} {self.path}: {e}", flush=True)
            self.send_response(502)
            self.end_headers()

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[proxy] Listening on {PORT}", flush=True)
    server.serve_forever()
