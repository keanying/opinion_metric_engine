# -*- coding: utf-8 -*-
"""全局测试隔离。

settings_local.py 放的是线上库连接和线上推送地址 + 令牌。测试进程的工作目录就是项目根，
不隔离的话 load_settings() 会把它读进来 —— CLI 测试跑一次 `run --push`
就会把样例数据推到真网关。这里对每个测试都关掉 settings_local，并清掉推送相关的环境变量。
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_local_settings(monkeypatch):
    monkeypatch.setenv("OPINION_IGNORE_LOCAL_SETTINGS", "1")
    for k in ("OPINION_PUSH_URL", "OPINION_PUSH_TOKEN", "OPINION_PUSH_ENABLED",
              "OPINION_PUSH_TOKEN_HEADER", "OPINION_PUSH_TOKEN_PREFIX"):
        monkeypatch.delenv(k, raising=False)
