#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网关模式启动器：自己建 unix socket（权限和属主自己说了算），建好再交给 uvicorn。

为什么不用 uvicorn 的 --uds
--------------------------
uvicorn 绑完 unix socket 会**无条件**把它 chmod 成 0666 —— 单 worker 那条路径也是
（`uvicorn/server.py` 里 `elif config.uds is not None:` 那段，写死 `uds_perms = 0o666`）。
而网关模式下 /gw 是宿主绑定挂载（/vol1/@appcenter/<app>），路径上每一层对本机其他
用户都可穿越（实测 `/vol1`、`/vol1/@appcenter`、应用目录的 other 位都带 x）。
0666 就等于**本机任何用户都能绕过飞牛网关直连 API**，顺手还能伪造网关注入的身份头。

权限和属主怎么定
----------------
本机实测（setpriv 切 uid，三方各连一次）：

    权限 / 属主                    uid0 网关   uid973 探活   uid1000 其他用户
    0666 root:root        （旧）     连得上       连得上         连得上   <- 漏洞
    0600 maa-fnos:maa-fnos（新）     连得上       连得上         拒绝
    0600 root:root        （错解）   连得上       拒绝           拒绝

第二行才对，三条同时成立才有意义：
* 网关 `trim_http_cgi` 跑在 root 下，root 有 CAP_DAC_OVERRIDE，不是属主也照样连得进来，
  所以收到 0600 不会挡网关。
* 但 `cmd/main` 的探活（status）和优雅停机（stop）是 `curl --unix-socket` 打进来的，
  而那个脚本由飞牛以 package 用户（本机 uid=973）执行 —— **属主必须是它**，否则应用
  自己的健康检查会被自己挡掉，应用中心会一直显示"异常"。
  属主这样定：有 MAA_WEB_SOCKET_OWNER 就用它，没有就取 socket 所在目录的属主
  （/gw 就是 TRIM_APPDEST，属主本来就是 package 用户，实测 973:966）。
* 属主对 + 权限 0600，本机其他用户在内核 connect 阶段就被拒。

顺带打开 --forwarded-allow-ips
-----------------------------
uvicorn 默认只信任 127.0.0.1 来的 X-Forwarded-*。而 unix socket 连接没有客户端地址
（scope["client"] 是 None），它的 ProxyHeadersMiddleware 拿这个去比 trusted_hosts 恒不
命中，转发头一律被丢掉 —— 后果是走 https 访问飞牛时重定向给出的 Location 是 http://，
把浏览器甩回网关外面。unix socket 下 uvicorn 只认 "*"（给具体 IP 匹配不上，没有对端地址）。
socket 既然已经收到「只有网关和应用自己连得进来」，信这两方注入的头就是安全的。

用法：MAA_WEB_SOCKET=/gw/app.sock python3 serve_socket.py
"""
from __future__ import annotations

import os
import socket
import sys

BACKLOG = 128


def resolve_owner(path: str) -> tuple[int, int]:
    """socket 该归谁：优先 MAA_WEB_SOCKET_OWNER，否则用 socket 所在目录的属主。"""
    raw = (os.environ.get("MAA_WEB_SOCKET_OWNER") or "").strip()
    if raw:
        uid_s, _, gid_s = raw.partition(":")
        try:
            return int(uid_s), (int(gid_s) if gid_s else -1)
        except ValueError:
            sys.exit("MAA_WEB_SOCKET_OWNER 要写成 'uid:gid'，收到的是 %r" % raw)

    parent = os.path.dirname(os.path.abspath(path))
    try:
        st = os.stat(parent)
    except OSError as exc:
        sys.exit("拿不到 %s 的属主（%s）；可以用 MAA_WEB_SOCKET_OWNER 显式指定" % (parent, exc))
    return st.st_uid, st.st_gid


def main() -> None:
    path = (os.environ.get("MAA_WEB_SOCKET") or "").strip()
    if not path:
        sys.exit("MAA_WEB_SOCKET 未设置：这是网关模式下应用的 socket 路径")

    try:
        os.unlink(path)
    except FileNotFoundError:
        pass

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # Python 3.4+ 建出来的 fd 默认带 CLOEXEC，execv 之后就没了，必须显式打开继承
    sock.set_inheritable(True)
    sock.bind(path)

    uid, gid = resolve_owner(path)
    try:
        os.chown(path, uid, gid)
    except PermissionError:
        sys.exit("把 socket 改成 %s:%s 失败：需要 root 权限（容器请用 root 起）。" % (uid, gid))

    os.chmod(path, 0o600)
    sock.listen(BACKLOG)

    os.execv(
        sys.executable,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--fd",
            str(sock.fileno()),
            "--forwarded-allow-ips",
            "*",
        ],
    )


if __name__ == "__main__":
    main()
