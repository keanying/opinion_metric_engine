# -*- coding: utf-8 -*-
"""dbutils 的异常。"""


class ConnectError(ConnectionError):
    """连不上数据库（地址 / 端口 / 账号 / 库名不对、网络不通），或 SSH 隧道建不起来。"""


class TunnelError(ConnectError):
    """SSH 隧道的问题：连不上跳板机、SSH 登录失败、私钥读不出、跳板机连不到数据库。"""
