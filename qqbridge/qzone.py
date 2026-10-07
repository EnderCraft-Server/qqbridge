"""QQ 空间与账号资料：SnowLuma 的 OneBot 扩展接口。

接口名来自 SnowLuma 1.14.21 的实测回显（缺参数时服务端会回显所需字段名）：
  send_qzone_msg     {content}                    发说说（纯文本）
  get_qzone_msg_list {targetUin?, pos, num}       机器人自己的说说列表
  get_qzone_feeds    {pageNum?, count?}           好友动态
  delete_qzone_msg   {tid}                        删说说（只能删自己的）
  like_qzone         {tid, targetUin?, like, abstime?}
  comment_qzone      {tid, content, targetUin?}
  set_qq_profile     {nickname}                   改昵称
  send_private_msg   {user_id, message}           单聊

这些**不是** OneBot v11 标准接口，是 SnowLuma 的扩展；换 OneBot 实现时必须重新核对，
不能假定其它实现同样支持。

本模块是 Toolbox 的 mixin：复用宿主已有的 _owner / _once / store / config，
不自己再实现一套鉴权。
"""
from __future__ import annotations

from .config import config


class QzoneMixin:
    # ---------- 单聊 ----------
    async def send_private(self, actor: str, user_id: str, text: str, idempotency_key: str) -> dict:
        if not config.allow_send:
            raise self._deny("ALLOW_SEND=false，发送被禁用。")
        self._owner(actor, "send_private")
        cached = self._once(idempotency_key, self.store)
        if cached is not None:
            return cached
        result = await self.bot.call("send_private_msg", user_id=int(user_id), message=text)
        self.bus.note_own_send()
        out = {"state": "SUCCEEDED", "result": result}
        self.store.remember(idempotency_key, out)
        self.store.audit(actor, "send_private", user_id, {"text": text}, "SUCCEEDED", str(result))
        return out

    # ---------- 账号资料 ----------
    async def set_profile(self, actor: str, nickname: str, idempotency_key: str) -> dict:
        self._owner(actor, "set_profile")
        if not nickname.strip():
            raise self._deny("昵称不能为空；本工具只改昵称，不接受空值。")
        if len(nickname) > 36:
            raise self._deny("昵称最长 36 个字符。")
        cached = self._once(idempotency_key, self.store)
        if cached is not None:
            return cached
        result = await self.bot.call("set_qq_profile", nickname=nickname)
        out = {"state": "SUCCEEDED", "result": result}
        self.store.remember(idempotency_key, out)
        self.store.audit(actor, "set_profile", "self", {"nickname": nickname}, "SUCCEEDED", str(result))
        return out

    # ---------- 空间：读 ----------
    async def qzone_list(self, target_uin: str = "", pos: int = 0, num: int = 20) -> dict:
        payload = {"pos": int(pos), "num": min(max(int(num), 1), 20)}
        if target_uin:
            payload["targetUin"] = int(target_uin)
        return await self.bot.call("get_qzone_msg_list", **payload)

    async def qzone_feeds(self, page: int = 1, count: int = 10) -> dict:
        return await self.bot.call("get_qzone_feeds",
                                   pageNum=max(int(page), 1), count=min(max(int(count), 1), 20))

    # ---------- 空间：写 ----------
    async def qzone_publish(self, actor: str, content: str, idempotency_key: str) -> dict:
        self._owner(actor, "qzone_publish")
        if not content.strip():
            raise self._deny("说说内容不能为空。")
        cached = self._once(idempotency_key, self.store)
        if cached is not None:
            return cached
        result = await self.bot.call("send_qzone_msg", content=content)
        out = {"state": "SUCCEEDED", "result": result}
        self.store.remember(idempotency_key, out)
        self.store.audit(actor, "qzone_publish", "self", {"content": content}, "SUCCEEDED", str(result))
        return out

    async def qzone_delete(self, actor: str, tid: str, idempotency_key: str) -> dict:
        self._owner(actor, "qzone_delete")
        cached = self._once(idempotency_key, self.store)
        if cached is not None:
            return cached
        result = await self.bot.call("delete_qzone_msg", tid=tid)
        out = {"state": "SUCCEEDED", "result": result}
        self.store.remember(idempotency_key, out)
        self.store.audit(actor, "qzone_delete", tid, {}, "SUCCEEDED", str(result))
        return out

    async def qzone_like(self, actor: str, tid: str, idempotency_key: str,
                         target_uin: str = "", like: bool = True, abstime: int = 0) -> dict:
        self._owner(actor, "qzone_like")
        cached = self._once(idempotency_key, self.store)
        if cached is not None:
            return cached
        payload = {"tid": tid, "like": bool(like), "abstime": int(abstime or 0)}
        if target_uin:
            payload["targetUin"] = int(target_uin)
        result = await self.bot.call("like_qzone", **payload)
        out = {"state": "SUCCEEDED", "result": result}
        self.store.remember(idempotency_key, out)
        self.store.audit(actor, "qzone_like", tid, payload, "SUCCEEDED", str(result))
        return out

    async def qzone_comment(self, actor: str, tid: str, content: str, idempotency_key: str,
                            target_uin: str = "") -> dict:
        self._owner(actor, "qzone_comment")
        if not content.strip():
            raise self._deny("评论内容不能为空。")
        cached = self._once(idempotency_key, self.store)
        if cached is not None:
            return cached
        payload = {"tid": tid, "content": content}
        if target_uin:
            payload["targetUin"] = int(target_uin)
        result = await self.bot.call("comment_qzone", **payload)
        out = {"state": "SUCCEEDED", "result": result}
        self.store.remember(idempotency_key, out)
        self.store.audit(actor, "qzone_comment", tid, payload, "SUCCEEDED", str(result))
        return out
