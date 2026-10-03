# -*- coding: utf-8 -*-
"""本地配置（适用当前版本：v1.9.2 + 需求 2.0）。含数据库密码和推送令牌，不要提交到 Git。

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

    # 补推（不重算，读 output/<景区编码>/ 下的 CSV）
    python -m engin_cli.cli push --scenic PFTSCE01001721 --start-date 20260909 --end-date 20260909
"""

DB = {
    "host": "localhost",
    "port": 3306,
    "user": "opinionhub",
    "password": "8drfEYnxAdcW5aP3",     # 也可以删掉这行，改用环境变量 OPINION_DB_PASSWORD
    "database": "opinionhub",           # 源表与目标表同库
}

ETL = {
    # ── 取数 ────────────────────────────────────────────────────────
    # 每个目标日期往回看 180 天。⚠ 一次跑一段区间时，区间里靠后的日期能看到的历史更多，
    # 365 日字段可能和逐日跑不完全一样；要严格一致就按天循环跑（见使用说明「二.6」）。
    "lookback_days": 180,
    "output_dir": "output",             # CSV 按景区写在 output/<景区编码>/ 下，push 补推读这里

    # ── 写库 ────────────────────────────────────────────────────────
    "batch_size": 2000,
    "dimension_upsert": True,           # ⚠ 前提：已执行 sql/alter_dimension_uniquekey.sql，否则重跑会出重复行
    "write_db": False,
    # ── 行存在规则 / 内容表大小 ─────────────────────────────────────
    "platform_codes": ["weibo", "douyin", "kuaishou", "xiaohongshu", "ctrip", "tongcheng"],
    "content_row_window": 30,           # 单位：天
    "content_top_words": 30,            # 单位：个，每天每种情感只留 Top30 词

    # ── 缺数回补 ────────────────────────────────────────────────────
    # 整窗前移，挪到有数据为止（不是累计）。下面就是默认上限，写出来方便对照。
    # 大盘表的日历周期借用档位：本周 → 7 日档、本月 → 30 日档、本季度 → 90 日档。
    "backfill_mode": "shift",
    "backfill_lookback": {1: 5, 7: 10, 14: 15, 30: 20, 60: 30, 90: 60, 365: 90},

    # ── 指标口径 ────────────────────────────────────────────────────
    "score_formula": "weighted_v2",
    # 得分权重 S = 5 ×（好评率×positive + 中评率×neutral + 差评率×negative），每档须在 [0, 1]。
    # 全引擎一套（core / 维度 / 大盘表同时生效），改了之后要用 repair 重刷历史分数。
    # "score_weights": {"positive": 1.0, "neutral": 0.9, "negative": 0.5},
    "unknown_dimension_policy": "drop",
    "publish_time_format": "%Y%m%d",

    # ── 下钻表 ──────────────────────────────────────────────────────
    "enable_drill_analysis": True,
    "drill_content_limit": 30,

    # ── 2.0 大盘 KPI 表（ads_trf_social_opinion_macro_gran_metric_di）───
    # 一行 = 景区 × 日期 × 周期 × 渠道（6 渠道 + all），默认 9 个周期，每个景区每天 63 行。
    # 同比要去年同期数据，跑批会按 publish_time 额外读一段去年同期的评论。
    "enable_macro_metric": True,
    "macro_wordcloud_top_n": 30,        # 词云好/中/差各取前 30 个词
    # 周期不改就不用写；要增删周期时把整份清单打开再改（name 写进 time_granularity）：
    "macro_granularities": [
        {"name": "td",           "label": "今日",   "type": "rolling", "days": 1},
        {"name": "latest_1d",    "label": "近一日", "type": "rolling", "days": 1, "offset": 1},
        {"name": "latest_7d",    "label": "近7日",  "type": "rolling", "days": 7},
        {"name": "wtd",          "label": "本周",   "type": "week"},
        {"name": "latest_30d",   "label": "近30日", "type": "rolling", "days": 30},
        {"name": "mtd",          "label": "本月",   "type": "month"},
        {"name": "latest_60d",   "label": "近60日", "type": "rolling", "days": 60},
        {"name": "latest_90d",   "label": "近90日", "type": "rolling", "days": 90},
        {"name": "qtd",          "label": "本季度", "type": "quarter"},
    ],

    # ══════════════════════════════════════════════════════════════
    # 推送
    # ══════════════════════════════════════════════════════════════
    # 平时不推，命令行加 --push 才推；定时任务想默认就推，改成 True。
    "push_enabled": False,

    # 本地（切到本地时：本机 3001 端口必须有服务在跑，否则会提示「推送地址连不上」并停止）
    "push_url": "http://localhost:3000/v1/data/opinion/dataSync/crossDomainSync",
    "push_headers": {"xpftkey": "pfk_d9975a31ee5624fdb741c80f9325a69b728b6c447e70b9c7"},

    # 线上（push_url 和 push_headers 要成对切换）
    # "push_url": "https://data.12ev.com/v1/data/quickBiData/dataSync/crossDomainSync",
    # "push_headers": {"xpftkey": "pfk_c123996df44e88d58acb27b61ed10a944493993c4bd2dcd5"},

    # 报文就是 {"tableName": ..., "pkId": [...], "data": [...]}，不带其他顶层字段。
    # tableName 用本地表名原样推（网关报错信息里的表名就是 ads_trf_... 这一串）。
    # 如果下游表名确实不一样，再打开下面这段：
    # "push_table_names": {
    #     "ads_trf_social_opinion_comment_core_di": "opinion_core",
    # },

    # pkId = 下游的「更新 + 查重键」，必须能唯一定位一行。
    # ⚠ 本地不会校验 pkId 是否唯一：重复的行到下游会互相覆盖，数据悄悄变少，改这里要自己确认。
    # 没写的表用默认值（对齐各表建表语句的唯一索引）：
    #   core      scenic_spot_code, travel_date, publish_time
    #   platform  scenic_spot_code, platform_code, travel_date, publish_time
    #   dimension scenic_spot_code, travel_date, dimension_level1/2/3
    #   content   scenic_spot_code, travel_date, emotion_word
    #   macro     scenic_id, travel_date, time_granularity, channel
    "push_pk": {
        # 下钻表：下游没有 detail_uk，改用这组字段。
        # ⚠ comment_id 只在「同一渠道、同一作品」内唯一（源表唯一键是 景区+渠道+作品+评论ID），
        #   不同平台的评论 ID 撞号时会互相覆盖。下游允许的话建议加上 platform_code、work_id。
        "ads_trf_social_opinion_drill_analysis_di": ["scenic_id", "emotion_word", "emotion_type", "platform_code", "work_id", "comment_id", "publish_time", "travel_date"],
        "ads_trf_social_opinion_macro_gran_metric_di": ["scenic_id", "channel", "time_granularity", "publish_time", "travel_date"],
        "ads_trf_social_opinion_comment_core_di": ["scenic_spot_code", "publish_time", "travel_date"],
        "ads_trf_social_opinion_comment_platform_di": ["scenic_spot_code", "platform_code", "publish_time", "travel_date"],
        "ads_trf_social_opinion_comment_dimension_score_di": ["scenic_spot_code", "dimension_level1", "dimension_level2", "dimension_level3", "publish_time", "travel_date"],
        "ads_trf_social_opinion_comment_content_di": ["scenic_spot_code", "emotion_type", "emotion_word", "publish_time", "travel_date"],
    },

    # ⚠ push_exclude_columns（从报文里去掉字段）当前版本还没实现，写了也不生效；
    #   下游表缺字段时，先让下游补上该列。

    "push_batch_size": 500,             # 每个请求 500 行
    "push_timeout": 120,                # 单次请求超时（秒）
    "push_retries": 3,                  # 只重试网络超时 / 5xx / 429；地址连不上会直接停止，不重试到底
    "push_retry_backoff": 2.0,          # 第 n 次重试等 2×2^n 秒：2、4、8

    # 网关 HTTP 恒为 200，成败看响应体 status（这几项就是默认值，写出来方便对照）
    "push_success_field": "status",
    "push_code_field": "code",
    "push_message_field": "msg",
    "push_trace_field": "trace_id",

    "push_strict": False,               # True = 推送失败时跑批退出码为 1（调度告警）

    "log_level": "INFO",
}
