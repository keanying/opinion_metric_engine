# -*- coding: utf-8 -*-
"""SSH 隧道（本地端口转发），直接用 paramiko 实现。

    本机 127.0.0.1:随机端口 ──SSH──▶ 跳板机 ──▶ 数据库地址:端口（从跳板机看）

为什么不用 sshtunnel：它认证失败时关闭会卡住 / 留下线程，报错也只有一句 Could not establish session，
分不清「网络不通」还是「密钥不对」。这里自己做，错误分得清、关得干净（转发线程都是 daemon，不会拖住进程退出）。

不校验跳板机的主机指纹（和 sshtunnel 默认一样）；对安全要求高的环境请在跳板机侧限制来源 IP。
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import select
import socket
import threading
from typing import List, Optional

from .config import SSHConfig
from .errors import TunnelError

log = logging.getLogger(__name__)


def _paramiko():
    try:
        import paramiko
        return paramiko
    except ImportError as e:
        raise TunnelError("服务器上没装 paramiko，用不了 SSH 隧道：pip install paramiko") from e


def load_private_key(text: str, passphrase: str = ""):
    """私钥文本 → paramiko 密钥对象。支持 OpenSSH / PEM 格式的 RSA、ECDSA、Ed25519（DSA 看 paramiko 版本）。

    出错抛 TunnelError，说人话：有密码保护没给密码、密码不对、不是私钥、是 PuTTY 的 .ppk。
    """
    paramiko = _paramiko()
    text = str(text or "").replace("\r\n", "\n").strip()
    if not text:
        raise TunnelError("没有私钥")
    if text.startswith("PuTTY-User-Key-File"):
        raise TunnelError("这是 PuTTY 的 .ppk 密钥，请先用 PuTTYgen 导出成 OpenSSH 格式（Conversions → Export OpenSSH key）")
    if "PRIVATE KEY" not in text:
        raise TunnelError("这不是私钥（应以 -----BEGIN … PRIVATE KEY----- 开头）；公钥 .pub 不行")
    kinds = [getattr(paramiko, k) for k in ("RSAKey", "ECDSAKey", "Ed25519Key", "DSSKey") if hasattr(paramiko, k)]
    need_pw = False
    for kind in kinds:
        try:
            return kind.from_private_key(io.StringIO(text + "\n"), password=passphrase or None)
        except paramiko.PasswordRequiredException:
            need_pw = True
        except paramiko.SSHException as e:
            m = str(e).lower()
            if passphrase and ("password" in m or "decrypt" in m or "bad" in m or "checkints" in m):
                need_pw = True
        except Exception:                            # noqa: BLE001 —— 换下一种密钥类型
            continue
    if need_pw:
        raise TunnelError("私钥有密码保护：请填密钥密码" if not passphrase else "密钥密码不对")
    raise TunnelError("读不出这个私钥（支持 OpenSSH / PEM 格式的 RSA、ECDSA、Ed25519）")


def key_fingerprint(text: str, passphrase: str = "") -> str:
    """「RSA 2048 · SHA256:xxxx」：认得出传的是哪把钥匙（和 ssh-keygen -lf 的写法一样）。"""
    k = load_private_key(text, passphrase)
    fp = base64.b64encode(hashlib.sha256(k.asbytes()).digest()).decode().rstrip("=")
    bits = k.get_bits() if hasattr(k, "get_bits") else 0
    name = k.get_name().replace("ssh-", "").replace("ecdsa-sha2-nistp", "ECDSA ").upper()
    return f"{name}{f' {bits}' if bits else ''} · SHA256:{fp}"


class SSHTunnel:
    """建一条本地端口转发。start() 返回本机端口；stop() 关掉（可重复调用）；也能 with 用。"""

    def __init__(self, ssh: SSHConfig, remote_host: str, remote_port: int):
        self.ssh = ssh
        self.remote = (str(remote_host), int(remote_port))
        self.local_port: Optional[int] = None
        self._transport = None
        self._server: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._socks: List = []
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 建立
    def start(self) -> int:
        if self.local_port is not None:
            return self.local_port
        self._stop = threading.Event()               # stop() 之后可以再 start()
        paramiko = _paramiko()
        s = self.ssh
        where = s.describe()
        pkey = load_private_key(s.key_text(), s.passphrase) if (s.private_key or s.private_key_path) else None
        if pkey is None and not s.password:
            raise TunnelError("SSH 隧道要私钥或 SSH 密码，至少给一个")
        # 1) 先连 TCP：区分「网络 / 端口不通」和后面的「认证不过」
        try:
            sock = socket.create_connection((s.host, int(s.port)), timeout=s.timeout)
        except OSError as e:
            raise TunnelError(f"连不上 SSH 服务器 {s.host}:{s.port}（{e.strerror or e}）："
                              "检查 SSH 地址、端口，以及本机到跳板机的网络 / 防火墙") from e
        t = paramiko.Transport(sock)
        t.banner_timeout = t.auth_timeout = t.handshake_timeout = s.timeout
        try:
            # 2) SSH 握手
            try:
                t.start_client(timeout=s.timeout)
            except Exception as e:                   # noqa: BLE001
                raise TunnelError(f"{s.host}:{s.port} 不是 SSH 服务，或握手失败（{e or type(e).__name__}）") from e
            # 3) 认证：先私钥，不行再密码
            err = None
            if pkey is not None:
                try:
                    t.auth_publickey(s.user, pkey)
                except paramiko.SSHException as e:
                    err = e
            if not t.is_authenticated() and s.password:
                try:
                    t.auth_password(s.user, s.password)
                except paramiko.SSHException as e:
                    err = e
            if not t.is_authenticated():
                how = "、".join(x for x, ok in (("私钥", pkey is not None), ("密码", bool(s.password))) if ok)
                raise TunnelError(f"SSH 登录失败 {where}：用户名或{how}不对（跳板机拒绝了）") from err
            t.set_keepalive(30)
        except BaseException:
            t.close()
            raise
        self._transport = t
        # 4) 本机监听，来一个连接就开一个 direct-tcpip 通道转发过去
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        srv.settimeout(0.5)
        self._server = srv
        self.local_port = srv.getsockname()[1]
        threading.Thread(target=self._accept_loop, name=f"ssh-tunnel-{self.local_port}", daemon=True).start()
        log.info("SSH 隧道已建立：127.0.0.1:%s → %s → %s:%s", self.local_port, where, *self.remote)
        return self.local_port

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                client, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                chan = self._transport.open_channel("direct-tcpip", self.remote, addr, timeout=self.ssh.timeout)
            except Exception as e:                   # noqa: BLE001 —— 跳板机连不到数据库：关掉客户端，MySQL 那边会报连接断开
                log.warning("SSH 隧道：跳板机连不到 %s:%s（%s）", *self.remote, e)
                client.close()
                continue
            with self._lock:
                self._socks += [client, chan]
            threading.Thread(target=self._pump, args=(client, chan), daemon=True,
                             name=f"ssh-tunnel-{self.local_port}-pump").start()

    def _pump(self, client, chan):
        try:
            while not self._stop.is_set():
                r, _, _ = select.select([client, chan], [], [], 0.5)
                if client in r:
                    data = client.recv(65536)
                    if not data:
                        break
                    chan.sendall(data)
                if chan in r:
                    data = chan.recv(65536)
                    if not data:
                        break
                    client.sendall(data)
        except Exception:                            # noqa: BLE001 —— 一端断了就结束
            pass
        finally:
            for x in (chan, client):
                try:
                    x.close()
                except Exception:                    # noqa: BLE001
                    pass

    # ---------------------------------------------------------------- 关闭
    def stop(self):
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        with self._lock:
            socks, self._socks = self._socks, []
        for x in socks:
            try:
                x.close()
            except Exception:                        # noqa: BLE001
                pass
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:                        # noqa: BLE001
                pass
        self._server = self._transport = None
        self.local_port = None

    @property
    def is_active(self) -> bool:
        return bool(self._transport is not None and self._transport.is_active())

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
