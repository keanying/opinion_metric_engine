# -*- coding: utf-8 -*-
"""连接配置：数据库 + 可选的 SSH 隧道。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class SSHConfig:
    """SSH 跳板机。私钥和密码至少给一个；私钥可以给内容（private_key）或文件路径（private_key_path）。"""
    host: str
    user: str
    port: int = 22
    password: str = ""
    private_key: str = ""          # 私钥文本（-----BEGIN … PRIVATE KEY-----）
    private_key_path: str = ""     # 或者私钥文件路径
    passphrase: str = ""           # 私钥有密码保护时
    timeout: float = 15.0          # 连接 / 认证超时（秒）

    def key_text(self) -> str:
        if self.private_key:
            return self.private_key
        if self.private_key_path:
            with open(self.private_key_path, encoding="utf-8") as f:
                return f.read()
        return ""

    def describe(self) -> str:
        return f"{self.user}@{self.host}:{self.port}"


@dataclass
class DataSourceConfig:
    """MySQL 连接。开了 SSH 隧道时，host / port 是「从跳板机上看到的」数据库地址（如云数据库内网地址）。"""
    host: str
    user: str
    password: str = ""
    database: str = ""
    port: int = 3306
    charset: str = "utf8mb4"
    connect_timeout: int = 10
    read_timeout: int = 600
    write_timeout: int = 600
    ssh: Optional[SSHConfig] = None
    name: str = ""                 # 只用来显示
    extra: Dict[str, Any] = field(default_factory=dict)   # 透传给 pymysql.connect 的其它参数

    def describe(self) -> str:
        """「名称（host:port/库，经 SSH user@跳板机:端口）」，不含密码 / 私钥，日志里用。"""
        where = f"{self.host}:{self.port}/{self.database}"
        if self.ssh:
            where += f"，经 SSH {self.ssh.describe()}"
        return f"{self.name}（{where}）" if self.name else where

    @classmethod
    def from_dict(cls, d: Dict[str, Any], **kw) -> "DataSourceConfig":
        """认「写入数据源」页面存的格式：host/port/user/password/database/name，
        ssh_enabled + ssh_host/ssh_port/ssh_user/ssh_password/ssh_key/ssh_key_passphrase。"""
        ssh = None
        if d.get("ssh_enabled"):
            ssh = SSHConfig(host=str(d.get("ssh_host") or "").strip(), user=str(d.get("ssh_user") or "").strip(),
                            port=int(d.get("ssh_port") or 22), password=str(d.get("ssh_password") or ""),
                            private_key=str(d.get("ssh_key") or ""),
                            private_key_path=str(d.get("ssh_key_path") or ""),
                            passphrase=str(d.get("ssh_key_passphrase") or ""))
        return cls(host=str(d.get("host") or "").strip(), port=int(d.get("port") or 3306),
                   user=str(d.get("user") or "").strip(), password=str(d.get("password") or ""),
                   database=str(d.get("database") or "").strip(), name=str(d.get("name") or ""),
                   ssh=ssh, **kw)
