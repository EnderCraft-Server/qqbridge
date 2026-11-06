# -*- coding: utf-8 -*-
"""Outbound HTTP policy self-test: a dead proxy must not drag the bot down with it.

The real incident on 2026-10-25: after the VPN was turned off, httpx still had
trust_env=True by default and kept handing every request to 127.0.0.1:7897, where
nothing was listening any more, so every single call ended in
ConnectError: All connection attempts failed -- the model, image viewing and web
fetching all went silent at once.

Three things are pinned down here:
  1. Loopback addresses always go direct, even with a dead proxy in the environment
  2. Remote addresses fall back to a direct connection when the proxy is dead
  3. The policy remembers the last successful path, so it does not burn a timeout on
     the dead proxy every time

Run: python tests/test_net.py
"""
import asyncio
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqbridge import net

ok = []


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"hello-from-fake-server"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        # Silence the default stderr access log so the test output stays readable.
        pass


def serve():
    srv = HTTPServer(("0.0.0.0", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# Best-effort non-loopback IPv4 address: only the remote-path cases need it, so a
# machine without one simply skips them.
def lan_ip() -> str:
    try:
        ip = socket.gethostbyname(socket.gethostname())
        return "" if net.is_loopback(ip) else ip
    except OSError:
        return ""


# ---------- 1. Loopback detection ----------
assert net.is_loopback("127.0.0.1")
assert net.is_loopback("localhost")
assert net.is_loopback("::1")
assert net.is_loopback("[::1]")
assert not net.is_loopback("api.deepseek.com")
assert not net.is_loopback("192.168.1.10")
ok.append("回环判定正确（127.0.0.1 / localhost / ::1 都算）")

# ---------- 2. Policy order ----------
assert net._order("127.0.0.1") == [False], "回环只该有直连一种可能"
net._REMOTE_MODE = None
assert net._order("api.deepseek.com") == [True, False], "没探过时先尊重系统代理"
net._REMOTE_MODE = "direct"
assert net._order("api.deepseek.com") == [False, True], "记住直连后应当先直连"
net._REMOTE_MODE = "proxy"
assert net._order("api.deepseek.com") == [True, False], "记住代理后应当先走代理"
net._REMOTE_MODE = None
ok.append("出口顺序：回环只有直连；远端按上次成功的路径走")

# Bound on 0.0.0.0 so the same server is also reachable through the LAN IP.
srv, port = serve()
try:
    # ---------- 3. A dead proxy in the env, loopback still works ----------
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:1"
    os.environ["ALL_PROXY"] = "http://127.0.0.1:1"
    os.environ["NO_PROXY"] = ""

    resp = asyncio.run(net.arequest("GET", f"http://127.0.0.1:{port}/", timeout=8))
    assert resp.status_code == 200 and resp.text == "hello-from-fake-server", resp.text
    ok.append("回环：环境里有死代理也直连成功（不会把 127.0.0.1 丢给代理）")

    # ---------- 4. Remote: dead proxy -> automatic fallback to direct ----------
    ip = lan_ip()
    if ip:
        # Reset the memoized mode: it is process-global state left over from case 2.
        net._REMOTE_MODE = None
        resp = asyncio.run(net.arequest("GET", f"http://{ip}:{port}/", timeout=8))
        assert resp.status_code == 200, resp.status_code
        assert net.mode() == "direct", f"回退之后应当记住直连，实际 {net.mode()}"
        # Memory: the next request should go direct at once, without hitting the proxy first
        resp = asyncio.run(net.arequest("GET", f"http://{ip}:{port}/", timeout=8))
        assert resp.status_code == 200
        ok.append(f"远端（{ip}）：死代理连不上 -> 自动退回直连，并记住这个选择")
    else:
        ok.append("远端回退：跳过（本机拿不到非回环地址）")

    # ---------- 5. When neither path works, the error must still be readable ----------
    # The AssertionError raised below must not be swallowed by the broad handler, which
    # is why it is caught and re-raised separately first.
    try:
        asyncio.run(net.arequest("GET", "http://127.0.0.1:1/", timeout=2))
        raise AssertionError("对着没人监听的端口居然成功了？")
    except AssertionError:
        raise
    except Exception as exc:
        msg = str(exc)
        assert "Connect" in msg or "连接失败" in msg, msg
        ok.append("全都不通时抛出可读的错误，而不是一句 All connection attempts failed")
finally:
    # Restore the environment even if an assertion fired above.
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        os.environ.pop(k, None)
    srv.shutdown()

print("\n".join("PASS  " + x for x in ok))
print("ALL PASS")
