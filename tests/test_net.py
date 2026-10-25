# -*- coding: utf-8 -*-
"""出口 HTTP 策略自测：代理挂了不能把机器人一起拖死。

2026-10-25 的真实故障：梯子关掉之后，httpx 默认 trust_env=True 仍然把所有请求
丢给已经没人监听的 127.0.0.1:7897，于是每一条都
ConnectError: All connection attempts failed —— 模型、看图、抓网页一起哑。

这里钉三件事：
  1. 回环地址永远直连，哪怕环境里塞了个死代理
  2. 远端地址遇到死代理会自动退回直连
  3. 策略会记住上次成功的路径，不会每次都在死代理上耗一个超时

运行：python tests/test_net.py
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
        pass


def serve():
    srv = HTTPServer(("0.0.0.0", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def lan_ip() -> str:
    try:
        ip = socket.gethostbyname(socket.gethostname())
        return "" if net.is_loopback(ip) else ip
    except OSError:
        return ""


# ---------- 1. 回环判定 ----------
assert net.is_loopback("127.0.0.1")
assert net.is_loopback("localhost")
assert net.is_loopback("::1")
assert net.is_loopback("[::1]")
assert not net.is_loopback("api.deepseek.com")
assert not net.is_loopback("192.168.1.10")
ok.append("回环判定正确（127.0.0.1 / localhost / ::1 都算）")

# ---------- 2. 策略顺序 ----------
assert net._order("127.0.0.1") == [False], "回环只该有直连一种可能"
net._REMOTE_MODE = None
assert net._order("api.deepseek.com") == [True, False], "没探过时先尊重系统代理"
net._REMOTE_MODE = "direct"
assert net._order("api.deepseek.com") == [False, True], "记住直连后应当先直连"
net._REMOTE_MODE = "proxy"
assert net._order("api.deepseek.com") == [True, False], "记住代理后应当先走代理"
net._REMOTE_MODE = None
ok.append("出口顺序：回环只有直连；远端按上次成功的路径走")

srv, port = serve()
try:
    # ---------- 3. 环境里塞个死代理，回环照样通 ----------
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:1"
    os.environ["ALL_PROXY"] = "http://127.0.0.1:1"
    os.environ["NO_PROXY"] = ""

    resp = asyncio.run(net.arequest("GET", f"http://127.0.0.1:{port}/", timeout=8))
    assert resp.status_code == 200 and resp.text == "hello-from-fake-server", resp.text
    ok.append("回环：环境里有死代理也直连成功（不会把 127.0.0.1 丢给代理）")

    # ---------- 4. 远端：死代理 -> 自动退回直连 ----------
    ip = lan_ip()
    if ip:
        net._REMOTE_MODE = None
        resp = asyncio.run(net.arequest("GET", f"http://{ip}:{port}/", timeout=8))
        assert resp.status_code == 200, resp.status_code
        assert net.mode() == "direct", f"回退之后应当记住直连，实际 {net.mode()}"
        # 记性：再发一次应当直接走直连，不再先撞代理
        resp = asyncio.run(net.arequest("GET", f"http://{ip}:{port}/", timeout=8))
        assert resp.status_code == 200
        ok.append(f"远端（{ip}）：死代理连不上 -> 自动退回直连，并记住这个选择")
    else:
        ok.append("远端回退：跳过（本机拿不到非回环地址）")

    # ---------- 5. 两种都不通时，报错要说人话 ----------
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
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        os.environ.pop(k, None)
    srv.shutdown()

print("\n".join("PASS  " + x for x in ok))
print("ALL PASS")
