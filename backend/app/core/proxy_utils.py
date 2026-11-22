"""代理 URL 的规范化与脱敏工具。

从 ``tools.fetch_intelligence`` 下沉到 ``app.core``: api-football 的代理/备用
通道 (``app.core.api_football``) 也要用同一套逻辑, 而 app 层不能反向 import
tools。两个函数都是纯字符串处理, 不发任何网络请求。
"""
from __future__ import annotations

from urllib.parse import quote, unquote


def normalize_proxy_url(proxy_url: str) -> str:
    """把代理 URL 里的账号密码做百分号编码, 兼容含 # + & ( ! ~ 等特殊字符的凭证。

    RFC 3986 规定 userinfo 里不允许出现 # (它是 fragment 起始符): httpx/requests
    解析 http://nuD#Z+0-a(&8Y0X:pw@ip:port 时会把 netloc 截断成 "nuD", 于是把
    "nud" 当目标主机去解析 DNS。因此不能指望 urlsplit 之类的标准解析 (它们同样
    按 RFC 截断), 这里按 "scheme://userinfo@hostport" 手工切分: 以最后一个 @
    分界 (密码允许含 @), 用户名取第一个 : 之前 (密码允许含 :), 两侧先 unquote
    再 quote — 对"已经写了编码凭证"的配置是幂等的, 不会二次编码。
    """
    text = (proxy_url or "").strip()
    if "@" not in text or "://" not in text:
        return text
    scheme, _, rest = text.partition("://")
    userinfo, sep, hostport = rest.rpartition("@")
    if not sep or ":" not in userinfo:
        # 没有账号密码 (或只有用户名): 原样返回, 无需编码
        return text
    user, _, password = userinfo.partition(":")
    encoded_user = quote(unquote(user), safe="")
    encoded_password = quote(unquote(password), safe="")
    return f"{scheme}://{encoded_user}:{encoded_password}@{hostport}"


def mask_proxy_url(proxy_url: str) -> str:
    """把代理 URL 里的账号密码抹成 ***, 防止凭证进日志 / api_status.last_error。"""
    text = (proxy_url or "").strip()
    if not text or "@" not in text:
        return text
    scheme, sep, rest = text.partition("://")
    if not sep:
        return text
    userinfo, at, host = rest.rpartition("@")
    if not at:
        return text
    user = userinfo.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}" if user else f"{scheme}://***:***@{host}"
