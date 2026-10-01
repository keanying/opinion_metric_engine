# -*- coding: utf-8 -*-
"""本地配置（适用 v1.8.3）。该文件已 gitignore，不要提交。

优先级：命令行参数 > 环境变量 > 本文件 > engin_cli/settings.py 默认值
没写的项一律用默认值；每项含义见 settings_local.example.py / 使用说明.md。

推送地址、xpftkey 都在下面 ETL 的「推送」段里指定，不需要设置任何环境变量
（也不要再设 OPINION_PUSH_URL / OPINION_PUSH_TOKEN，否则会覆盖这里）。

常用命令：
    # 核对报文（不发送）
    python -m engin_cli.cli run  --scenic PFTSCA01002434 --date 20260916 --skip-db
    python -m engin_cli.cli push --scenic PFTSCA01002434 --date 20260916 --preview

    # 按景区跑 + 推送全部表（不写 --scenic = 全部景区逐个跑）
    python -m engin_cli.cli run --push all --scenic PFTSCA01002434 --start-date 20260101 --end-date 20260916

    # 只跑/只推一天
    python -m engin_cli.cli run --push all --start-date 20260916 --end-date 20260916
"""

DB = {
    "host": "192.168.110.250",
    "port": 3306,
    "user": "opinionhub",
    "password": "8drfEYnxAdcW5aP3",     # 也可以删掉这行，改用环境变量 OPINION_DB_PASSWORD
    "database": "opinionhub",           # 源表与目标表同库
}

ETL = {
    # ── 取数 ────────────────────────────────────────────────────────
    "lookback_days": 180,               # 每个日期各自往回看 180 天（区间重跑 == 逐日跑）
    "output_dir": "output",             # CSV 按景区写在 output/<景区编码>/ 下

    # ── 写库 ────────────────────────────────────────────────────────
    "batch_size": 2000,
    "dimension_upsert": True,           # ⚠ 前提：已执行 sql/alter_dimension_uniquekey.sql，否则重跑会出重复行

    # ── 行存在规则 / 内容表大小 ─────────────────────────────────────
    "platform_codes": ["weibo", "douyin", "kuaishou", "xiaohongshu", "ctrip", "tongcheng"],
    "content_row_window": 30,           # 单位：天
    "content_top_words": 30,            # 单位：个，每天每种情感只留 Top30 词

    # ── 缺数回补 ────────────────────────────────────────────────────
    "backfill_mode": "shift",
    "backfill_lookback": {1: 5, 7: 10, 14: 15, 30: 20, 60: 30, 90: 60, 365: 90},
    # backfill_window_mode 不配 = core/platform 用 daily_sum（近N日 == 最近N天相加），
    #                              dimension/content 用 window_shift（稀疏，不逐日补）

    # ── 指标口径 ────────────────────────────────────────────────────
    "score_formula": "weighted_v2",
    "unknown_dimension_policy": "drop",
    "publish_time_format": "%Y%m%d",

    # ── 下钻表 ──────────────────────────────────────────────────────
    "enable_drill_analysis": True,
    "drill_content_limit": 30,

    # ══════════════════════════════════════════════════════════════
    # 推送
    # ══════════════════════════════════════════════════════════════
    # 平时不推，命令行加 --push 才推；定时任务想默认就推，改成 True。
    "push_enabled": False,

    # 本地
    # "push_url": "http://localhost:3001/v1/data/opinion/dataSync/crossDomainSync",

    # 线上
    "push_url": "https://data.12301dev.com/v1/data/quickBiData/dataSync/crossDomainSync",

    # 请求头，原样带上：xpftkey: pfk_...（Content-Type 自动加） pfk_d9975a31ee5624fdb741c80f9325a69b728b6c447e70b9c7
    # 本地
    # "push_headers": {"xpftkey": "pfk_d9975a31ee5624fdb741c80f9325a69b728b6c447e70b9c7"},

    # 线上
    "push_headers": {"xpftkey": "pfk_c123996df44e88d58acb27b61ed10a944493993c4bd2dcd5"},

    # 报文就是 {"tableName": ..., "pkId": [...], "data": [...]}，不带其他顶层字段。
    # tableName 用本地表名原样推（网关报错信息里的表名就是 ads_trf_... 这一串）。
    # 如果下游表名确实不一样，再打开下面这段：
    # "push_table_names": {
    #     "ads_trf_social_opinion_comment_core_di": "opinion_core",
    # },

    # pkId = 下游的「更新 + 查重键」，必须能唯一定位一行。发送前会在本地整表校验，
    # 重复或为空直接报错不发。
    "push_pk": {
        # "ads_trf_social_opinion_comment_core_di": ["scenic_spot_code", "publish_time", "travel_date"],
        # "ads_trf_social_opinion_comment_content_di": ["scenic_spot_code", "emotion_type", "emotion_word", "publish_time", "travel_date"],
        # "ads_trf_social_opinion_comment_dimension_score_di": ["scenic_spot_code", "publish_time", "travel_date", "dimension_level1", "dimension_level2", "dimension_level3"],
        # "ads_trf_social_opinion_comment_platform_di": ["scenic_spot_code", "platform_code", "publish_time", "travel_date"],
        "ads_trf_social_opinion_drill_analysis_di": ["scenic_id",  "emotion_word", "comment_id", "publish_time", "travel_date"]},

    # 下游表里没有某些字段时，在这里去掉（例：下游下钻表没建 detail_uk）
    # "push_exclude_columns": {
    #     "ads_trf_social_opinion_drill_analysis_di": ["detail_uk"],
    # },

    "push_batch_size": 500,             # 每个请求 500 行（Top30 下一个景区一年的 content 约 45 个请求）
    "push_timeout": 120,                # 单次请求超时（秒）
    "push_retries": 3,                  # 只重试网络错误 / 5xx / 429
    "push_retry_backoff": 2.0,          # 第 n 次重试等 2×2^n 秒：2、4、8

    # 网关 HTTP 恒为 200，成败看响应体 status（这几项就是默认值，写出来方便对照）
    "push_success_field": "status",
    "push_code_field": "code",
    "push_message_field": "msg",
    "push_trace_field": "trace_id",

    "push_strict": False,               # True = 推送失败时跑批退出码为 1（调度告警）

    "log_level": "INFO",
}