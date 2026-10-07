# -*- coding: utf-8 -*-
"""OneBot WebSocket 事件流自测。

盯的是三件容易出错的事：
  1. connected 必须随时反映真实状态 —— 控制台右上角那个「QQ 已连接」是排查
     「收不到消息」的第一入口，它撒谎的话后面全是白费功夫。
  2. 服务端「接了立刻关」时不能变成死循环猛敲对面（原来正常断开走的是
     async for 自然结束那条路，不经过 except，于是不 sleep 直接重连）。
  3. 事件要真的送到回调里。

运行：python tests/test_onebot.py
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
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def stop_bot(bot, task):
    bot._stop.set()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


# ---------- 1. 接了立刻关：不能疯转，且 connected 必须是 False ----------
async def case_immediate_close():
    conns = []

    async def handler(ws, *_):
        conns.append(1)
        await ws.close()

    server, port = await serve(handler)
    try:
        bot = OneBot("http://127.0.0.1:1", f"ws://127.0.0.1:{port}")
        bot._stop.clear()
        task = asyncio.create_task(bot._ws_loop())
        await asyncio.sleep(1.6)
        n = len(conns)
        assert bot.connected is False, "连接已经断了，connected 还挂着 True"
        await stop_bot(bot, task)
    finally:
        server.close()
        await server.wait_closed()
    # 退避从 1 秒起：1.6 秒内最多重连 2~3 次。修之前是不 sleep 的疯转，会有几百次
    assert n <= 4, f"服务端秒关时重连了 {n} 次，说明没有退避（会把自己和对面都拖垮）"
    return n


# ---------- 2. 正常连接：connected=True，事件送达，断开后变回 False ----------
async def case_connected_and_events():
    holder = {}
    got = []

    async def handler(ws, *_):
        holder["ws"] = ws
        await ws.send(json.dumps({"post_type": "message", "time": 123, "raw_message": "hi",
                                  "user_id": 1, "group_id": 2}))
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
