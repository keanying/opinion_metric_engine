# -*- coding: utf-8 -*-
"""全局测试隔离。

settings_local.py 放的是线上库连接和线上推送地址 + 令牌。测试进程的工作目录就是项目根，
不隔离的话 load_settings() 会把它读进来 —— CLI 测试跑一次 `run --push`
就会把样例数据推到真网关。这里对每个测试都关掉 settings_local，并清掉推送相关的环境变量。
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_local_settings(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("OPINION_IGNORE_LOCAL_SETTINGS", "1")
    # 推送记录写到临时目录，别在项目根下留 logs/
    # （单独的临时目录：测试自己的 tmp_path 常被当作输出目录，会去数里面有几个文件）
    monkeypatch.setenv("OPINION_PUSH_LOG_DIR", str(tmp_path_factory.mktemp("push_logs")))
    for k in ("OPINION_PUSH_URL", "OPINION_PUSH_TOKEN", "OPINION_PUSH_ENABLED",
              "OPINION_PUSH_TOKEN_HEADER", "OPINION_PUSH_TOKEN_PREFIX"):
        monkeypatch.delenv(k, raising=False)
