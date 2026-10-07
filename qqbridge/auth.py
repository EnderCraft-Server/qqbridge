"""控制台认证与协议。

- 首次访问必须同意《使用须知》与《最终用户许可协议》
- 同意后设置管理员账号密码；之后每次进后台都要登录
- 密码用 PBKDF2-SHA256 加盐存储，永不落明文
- 登录态是内存里的随机 token（重启即失效），放在 HttpOnly Cookie
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from pathlib import Path

log = logging.getLogger("qqbridge.auth")

AGREEMENT_VERSION = "1.0"
SESSION_TTL = 7 * 24 * 3600          # 登录态有效期：7 天

TERMS = """# 使用须知

**版本 1.0 ｜ 生效日期：首次使用之日**

## 一、这是什么

qqbridge（下称「本软件」）是一个把 QQ 消息接入 AI 的本地桥接工具。它运行在你自己的电脑上，
使用你自己的 QQ 账号、你自己配置的模型服务。本软件不提供任何服务器端账号，
也不代管你的任何数据。

## 二、BUG 与可用性

1. **本软件无法保证完全没有 BUG。** 它处于持续开发中，可能存在功能异常、数据处理错误、
   消息丢失、重复发送、请求超时等问题。
2. 因 BUG 导致的任何直接或间接损失（包括但不限于消息误发、账号异常、数据丢失、
   模型费用消耗），**作者不承担赔偿责任**。
3. 部分功能依赖第三方服务（OneBot 实现、模型 API、QQ 官方客户端）。
   这些服务的可用性、接口变更、风控策略均不在本软件控制范围内，
   由此导致的功能失效同样不构成作者的违约。

## 三、使用者的责任

1. 你必须在**合法合规**的前提下使用本软件，遵守所在地法律法规、
   QQ / 微信等平台的用户协议，以及你所使用模型服务商的服务条款。
2. **严禁**将本软件用于：垃圾信息群发、骚扰他人、诈骗、传播违法信息、
   侵犯他人隐私、批量养号、绕过平台风控等任何违法或不当用途。
3. **若有人将本软件用于违法或不当用途，一切后果由使用者自行承担，
   开发者概不负责。** 开发者不参与、不知情、不纵容任何此类行为。
4. 你对自己账号下发生的一切行为负责，包括机器人自动发出的每一条消息。

## 四、数据与隐私

1. 聊天记录、群成员信息、QQ 空间内容等数据保存在**你自己的电脑上**，
   本软件不上传、不备份、不分析。
2. 与 AI 交互时，必要的消息片段会发送到**你自己配置的模型服务**，
   请自行阅读该服务商的隐私政策。
3. 请勿将含有真实聊天记录、账号凭据、API Key 的配置目录分享给他人。

## 五、费用

本软件本身免费。调用模型服务产生的费用由你的模型服务商按你账户的实际用量收取，
与本软件作者无关。

## 六、协议变更

本软件可能随版本更新调整本须知。继续使用即视为接受更新后的条款。
"""

EULA = """# 最终用户许可协议（EULA）

**版本 1.0**

## 一、许可授予

在您同意本协议全部条款的前提下，EnderCraft 授予您一项**非独占、不可转让**的许可，
允许您在自有设备上安装、运行、修改本软件，用于个人或您所在组织的合法用途。

## 二、知识产权与创作归属

1. **本机器人及其相关创作的所有权归属 EnderCraft。**
   包括但不限于：本软件的源代码、架构设计、提示词体系、
   机器人人格设定、自动生成的文字内容及其组织方式。
2. 您可以自由使用和修改本软件，但不得删除或篡改版权声明，
   不得将本软件或其衍生作品**宣称**为由您原创。
3. 二次分发时须保留本协议与原始出处说明。
4. 本软件使用的第三方组件（见 THIRD_PARTY 说明）遵循各自许可证，
   本协议不改变那些许可。

## 三、免责声明

1. **本软件按「现状」提供，不附带任何明示或默示的担保**，
   包括但不限于适销性、特定用途适用性和无侵权的担保。
2. **作者不保证本软件没有 BUG，也不保证其运行不中断、不出错。**
3. 在适用法律允许的最大范围内，**作者对因使用或无法使用本软件而产生的
   任何损害（包括但不限于利润损失、数据丢失、业务中断、
   账号被限制或封禁、第三方索赔）概不负责**，即便已被告知此类损害的可能性。
4. **使用者若将本软件用于任何违法或违反平台规则的行为，
   由使用者独立承担全部法律责任，与作者无关。**

## 四、责任限制

在适用法律允许的最大范围内，作者就本协议承担的累计责任总额，
不超过您为获得本软件实际支付的金额（本软件免费，即为零）。

## 五、终止

若您违反本协议任何条款，本许可自动终止。终止后您应停止使用并删除本软件。
本协议中关于知识产权、免责声明、责任限制的条款在终止后继续有效。

## 六、其他

1. 本协议的解释与争议解决适用中华人民共和国法律。
2. 若某条款被认定无效，其余条款仍然有效。

---

**点击「同意」或继续使用本软件，即表示您已完整阅读、理解并同意上述全部条款。**
"""


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               bytes.fromhex(salt), 200_000).hex()


class Auth:
    def __init__(self, path: Path):
        self.path = path
        self.sessions: dict[str, float] = {}
        self.data: dict = {}
        self.load()

    # ---------- 持久化 ----------
    def load(self):
        if self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("auth.json 损坏，按未初始化处理")
                self.data = {}
        return self.data

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------- 状态 ----------
    @property
    def agreed(self) -> bool:
        return bool(self.data.get("agreed_at")) and \
            self.data.get("agreement_version") == AGREEMENT_VERSION

    @property
    def has_account(self) -> bool:
        return bool(self.data.get("username") and self.data.get("pwd_hash"))

    def state(self) -> dict:
        return {
            "agreed": self.agreed,
            "has_account": self.has_account,
            "username": self.data.get("username", ""),
            "agreement_version": AGREEMENT_VERSION,
            "agreed_at": self.data.get("agreed_at"),
        }

    # ---------- 首次初始化 ----------
    def accept(self, username: str, password: str):
        if self.agreed and self.has_account:
            raise ValueError("已初始化，不能重复设置。")
        username = (username or "").strip()
        if len(username) < 2 or len(username) > 32:
            raise ValueError("用户名长度需在 2~32 之间。")
        if len(password or "") < 8:
            raise ValueError("密码至少 8 位。")
        salt = secrets.token_hex(16)
        self.data.update({
            "username": username,
            "salt": salt,
            "pwd_hash": _hash(password, salt),
            "agreed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "agreement_version": AGREEMENT_VERSION,
        })
        self.save()
        return self.state()

    # ---------- 登录 ----------
    def login(self, username: str, password: str) -> str:
        if not (self.agreed and self.has_account):
            raise ValueError("尚未完成初始化。")
        if (username or "").strip() != self.data.get("username"):
            raise ValueError("用户名或密码错误。")
        expect = self.data.get("pwd_hash", "")
        got = _hash(password or "", self.data.get("salt", ""))
        if not hmac.compare_digest(expect, got):
            raise ValueError("用户名或密码错误。")
        token = secrets.token_urlsafe(32)
        self.sessions[token] = time.time() + SESSION_TTL
        return token

    def check(self, token: str) -> bool:
        if not token:
            return False
        exp = self.sessions.get(token)
        if exp is None:
            return False
        if exp < time.time():
            self.sessions.pop(token, None)
            return False
        return True

    def logout(self, token: str):
        self.sessions.pop(token, None)

    def change_password(self, old: str, new: str):
        expect = self.data.get("pwd_hash", "")
        if not hmac.compare_digest(expect, _hash(old or "", self.data.get("salt", ""))):
            raise ValueError("原密码错误。")
        if len(new or "") < 8:
            raise ValueError("新密码至少 8 位。")
        salt = secrets.token_hex(16)
        self.data.update({"salt": salt, "pwd_hash": _hash(new, salt)})
        self.save()
        self.sessions.clear()          # 改密后强制重新登录
