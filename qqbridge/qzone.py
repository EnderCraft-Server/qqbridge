"""QQ Qzone and account profile: SnowLuma's OneBot extension API.

The action names come from what SnowLuma 1.14.21 actually echoed back in testing (when a
parameter is missing, the server echoes the name of the field it wants):
  send_qzone_msg     {content}                    publish a post (plain text)
  get_qzone_msg_list {targetUin?, pos, num}       the bot's own posts
  get_qzone_feeds    {pageNum?, count?}           friends' feeds
  delete_qzone_msg   {tid}                        delete a post (own posts only)
  like_qzone         {tid, targetUin?, like, abstime?}
  comment_qzone      {tid, content, targetUin?}
  set_qq_profile     {nickname}                   change the nickname
  send_private_msg   {user_id, message}           private chat

These are **not** OneBot v11 standard actions but SnowLuma extensions; switching to
another OneBot implementation means re-checking them, and the same set must not be assumed
to work elsewhere.

This module is a mixin for Toolbox: it reuses the host's existing _owner / _once / store /
config instead of implementing a second authorization layer of its own.
"""
from __future__ import annotations

from .config import config


class QzoneMixin:
    # ---------- Private chat ----------
    async def send_private(self, actor: str, user_id: str, text: str, idempotency_key: str) -> dict:
        # Same two gates as the group send: the global switch, then the actor allowlist.
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

    # ---------- Account profile ----------
    async def set_profile(self, actor: str, nickname: str, idempotency_key: str) -> dict:
        self._owner(actor, "set_profile")
        # Validate locally: the server would either truncate silently or answer with an
        # error that does not say what was wrong.
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

    # ---------- Qzone: read ----------
    async def qzone_list(self, target_uin: str = "", pos: int = 0, num: int = 20) -> dict:
        # num is clamped to 1..20 because the extension rejects anything outside that
        # range; pos is the paging offset, and an empty target_uin means the bot itself.
        payload = {"pos": int(pos), "num": min(max(int(num), 1), 20)}
        if target_uin:
            payload["targetUin"] = int(target_uin)
        return await self.bot.call("get_qzone_msg_list", **payload)

    async def qzone_feeds(self, page: int = 1, count: int = 10) -> dict:
        # Same clamping as qzone_list(): page numbers start at 1, count tops out at 20.
        return await self.bot.call("get_qzone_feeds",
                                   pageNum=max(int(page), 1), count=min(max(int(count), 1), 20))

    # ---------- Qzone: write ----------
    async def qzone_publish(self, actor: str, content: str, idempotency_key: str) -> dict:
        self._owner(actor, "qzone_publish")
        # Empty content is refused here; the server would only return an opaque error.
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
        # tid is the post id returned by qzone_list(); only the bot's own posts can be
        # deleted, which is a server-side restriction rather than one enforced here.
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
        # abstime defaults to 0 meaning "now"; targetUin is optional and only needed when
        # liking somebody else's post.
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
        # As with likes, targetUin is only required for other people's posts.
        payload = {"tid": tid, "content": content}
        if target_uin:
            payload["targetUin"] = int(target_uin)
        result = await self.bot.call("comment_qzone", **payload)
        out = {"state": "SUCCEEDED", "result": result}
        self.store.remember(idempotency_key, out)
        self.store.audit(actor, "qzone_comment", tid, payload, "SUCCEEDED", str(result))
        return out
