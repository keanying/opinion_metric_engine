# -*- coding: utf-8 -*-
"""飞书机器人通知：数据质量检测不通过、指标计算异常时发。

    settings_local.py：
        "notify_feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx",
        "notify_feishu_secret": "",      # 机器人开了「签名校验」才填
    或环境变量 OPINION_FEISHU_WEBHOOK / OPINION_FEISHU_SECRET。

没配 webhook 就不发。发送失败只打一条 warning，**不影响跑批本身**。
测试：python -m engin_cli.cli notify-test
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable, Optional

log = logging.getLogger(__name__)

MAX_LINES = 15          # 正文最多几行（错误清单太长时截断，飞书单条消息有长度上限）
MAX_LINE_CHARS = 300


def _sign(secret: str, timestamp: int) -> str:
    """飞书「签名校验」：base64(HmacSHA256(key=timestamp+"\\n"+secret, msg=空))。"""
    key = f"{timestamp}\n{secret}".encode("utf-8")
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode("utf-8")


def build_text(title: str, lines: Iterable[str]) -> str:
    """标题 + 正文（截断过长的行、过多的行）+ 主机 / 时间。"""
    lines = [str(x) for x in lines if str(x).strip()]
    body = [x if len(x) <= MAX_LINE_CHARS else x[:MAX_LINE_CHARS] + "…" for x in lines[:MAX_LINES]]
    if len(lines) > MAX_LINES:
        body.append(f"…… 另有 {len(lines) - MAX_LINES} 条，详见跑批日志")
    body.append(f"主机：{socket.gethostname()}    时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join([title] + body)


def send_feishu(settings, title: str, lines: Iterable[str] = (),
                timeout: float = 10.0) -> Optional[bool]:
    """发一条飞书文本消息。

    出参：None = 没配 webhook，没发；True = 发送成功；False = 发送失败（已打 warning）。
    """
    url = str(getattr(settings, "notify_feishu_webhook", "") or "").strip()
    if not url:
        return None
    body = {"msg_type": "text", "content": {"text": build_text(title, lines)}}
    secret = str(getattr(settings, "notify_feishu_secret", "") or "").strip()
    if secret:
        ts = int(time.time())
        body.update({"timestamp": str(ts), "sign": _sign(secret, ts)})
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    from .pusher import _NO_PROXY_OPENER, _is_local_host   # 发往本机不走代理（同推送）
    local = _is_local_host(urllib.parse.urlsplit(url).hostname)
    try:
        with (_NO_PROXY_OPENER.open(req, timeout=timeout) if local
              else urllib.request.urlopen(req, timeout=timeout)) as resp:
            text = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("飞书通知发送失败：%s", e)
        return False
    try:
        data = json.loads(text)
    except ValueError:
        data = {}
    # 飞书成功返回 {"code":0,...}（老版本是 {"StatusCode":0,...}）
    code = data.get("code", data.get("StatusCode"))
    if code not in (0, "0"):
        log.warning("飞书通知发送失败：%s", text[:300])
        return False
    log.info("已发送飞书通知：%s", title)
    return True
