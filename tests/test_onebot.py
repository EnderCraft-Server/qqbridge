# -*- coding: utf-8 -*-
"""OneBot WebSocket event stream self-test.

Three easy-to-get-wrong things are watched here:
  1. connected must always reflect the real state -- the "QQ 已连接" badge in the top
     right of the console is the first place to look when "no messages arrive", so
     if it lies, everything debugged afterwards is wasted effort.
  2. When the server "accepts and closes at once", the client must not turn into a
     tight loop hammering the other side (a normal disconnect used to end naturally
     through async for, never reaching except, so it reconnected without sleeping).
  3. Events must actually reach the callback.

Run: python tests/test_onebot.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets

from qqbridge.onebot import OneBot

ok = []


async def serve(handler):
    # Port 0 lets the OS pick a free port; the real one is read back from the socket.
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def stop_bot(bot, task):
    # Stop the loop both ways, and swallow the cancellation so it never surfaces as a
    # spurious test failure.
    bot._stop.set()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


# ---------- 1. Accept-then-close: no busy loop, and connected must be False ----------
async def case_immediate_close():
    conns = []

    async def handler(ws, *_):
        conns.append(1)
        await ws.close()

    server, port = await serve(handler)
    try:
        bot = OneBot("http://127.0.0.1:1", f"ws://127.0.0.1:{port}")
        # The stop flag may still be set from an earlier case; clear it before starting.
        bot._stop.clear()
        task = asyncio.create_task(bot._ws_loop())
        await asyncio.sleep(1.6)
        n = len(conns)
        assert bot.connected is False, "连接已经断了，connected 还挂着 True"
        await stop_bot(bot, task)
    finally:
        server.close()
        await server.wait_closed()
    # Backoff starts at 1s, so 1.6s allows at most 2-3 reconnects; before the fix it spun
    # without sleeping and produced hundreds.
    assert n <= 4, f"服务端秒关时重连了 {n} 次，说明没有退避（会把自己和对面都拖垮）"
    return n


# ---------- 2. Normal connect: connected=True, events delivered, False after close ----------
async def case_connected_and_events():
    holder = {}
    got = []

    async def handler(ws, *_):
        holder["ws"] = ws
        await ws.send(json.dumps({"post_type": "message", "time": 123, "raw_message": "hi",
                                  "user_id": 1, "group_id": 2}))
        # Hold the connection open so the client stays connected for the assertions.
        await asyncio.sleep(30)

    server, port = await serve(handler)
    try:
        bot = OneBot("http://127.0.0.1:1", f"ws://127.0.0.1:{port}")
        bot.on_event = got.append
        bot._stop.clear()
        task = asyncio.create_task(bot._ws_loop())
        await asyncio.sleep(0.4)
        assert bot.connected is True, "连上了却报告未连接"
        assert got and got[0]["raw_message"] == "hi", f"事件没送到回调：{got}"
        assert bot.last_event_at == 123, f"last_event_at 没更新：{bot.last_event_at}"
        await holder["ws"].close()
        await asyncio.sleep(0.4)
        assert bot.connected is False, "对面关了连接，connected 还是 True（控制台会一直显示已连接）"
        await stop_bot(bot, task)
    finally:
        server.close()
        await server.wait_closed()
    return len(got)


n1 = asyncio.run(case_immediate_close())
ok.append(f"服务端秒关 -> 1.6 秒内只重连 {n1} 次（有退避），connected 正确归 False")
n2 = asyncio.run(case_connected_and_events())
ok.append(f"正常连接 -> connected=True、事件送达回调、对端断开后 connected 归 False（收到 {n2} 条）")

print("\n".join("PASS  " + x for x in ok))
print("ALL PASS")
