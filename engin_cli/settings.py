# -*- coding: utf-8 -*-
"""运行期配置。

优先级：命令行参数 > 环境变量 > settings_local.py > 这里的默认值。

数据库连接信息**不要**写进本文件（会进版本库），复制 settings_local.example.py
为 settings_local.py 再填，该文件已在 .gitignore 里。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


def _env(key: str, default=None, cast=str):
    v = os.environ.get(key)
    if v is None or v == "":
        return default
    if cast is bool:
        return str(v).strip().lower() in ("1", "true", "yes", "y", "on")
    return cast(v)


@dataclass
class DBConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = ""
    charset: str = "utf8mb4"
    connect_timeout: int = 30
    read_timeout: int = 600
    write_timeout: int = 600

    # 源表与目标表可以不在同一个库。留空表示与 database 相同。
    src_database: Optional[str] = None
    ads_database: Optional[str] = None

    def src_db(self) -> str:
        return self.src_database or self.database

    def ads_db(self) -> str:
        return self.ads_database or self.database


@dataclass
class EtlSettings:
    db: DBConfig = field(default_factory=DBConfig)

    # ---- 取数区间 ----
    # 需求六.2：元数据按 publish_time 拉取，近半年数据参与计算。
    # 注意：365d 窗口要真正跟 90d 分离，历史至少要有 365 天；
    # 半年历史下 90d/365d 会退化成「全部历史均值」而相等，这不是 bug。
    lookback_days: int = 180
    # 上一周期环比需要额外的历史。默认按最长窗口再补一个周期，封顶到 lookback_days。
    growth_extra_days: int = 30

    # ---- 缺数回补（需求六.1；口径见 metric_calc_domain §1.8）----
    # 口径：某个窗口当期没有数据时，把**整个窗口整体往前平移**，一天一天挪，
    # 挪到那一天的窗口有数据就停，用**那一个窗口**的值 —— 不是累计求和。
    #   例：2026-09-11 没有快手 → 挪到 09-10，那天有 1 条 → 09-11 的 1 日值就是 1
    # 回补之后**所有指标（含评论总数）都用补齐后的数据算**，四张表一视同仁。
    #
    # shift  整窗前移取到为止（默认）
    # off    不回补，当期没数据就是 0（用来对拍回补到底影响了多少）
    backfill_mode: str = "shift"
    # 每个窗口最多往前挪几天，挪满仍无数据则该窗口取 0。
    # 不填用 domain.BACKFILL_LOOKBACK 的默认表：
    #   1日→5  7日→10  14日→15  30日→20  60日→30  90日→60  365日→90
    # 只想改其中几档就只写那几档，其余仍用默认，例如 {1: 3, 7: 7}
    backfill_lookback: Optional[dict] = None

    # publish_time 的写入格式。DDL 注释是 'YYYY-MM-DD HH:mm:ss'，
    # 但它进了三张表的唯一索引，默认沿用现有数据的 yyyyMMdd 口径。
    publish_time_format: str = "%Y%m%d"

    # ---- 写入 ----
    batch_size: int = 2000
    dry_run: bool = False
    # 写库策略：六张表一律「按 景区 + publish_time 先删除、再写入」（同一事务，见 loader.py）。
    # dimension_upsert 已不再生效，保留字段只是为了兼容老的 settings_local.py。
    dimension_upsert: bool = False

    # ---- 输出 ----
    output_dir: str = "output"
    write_csv: bool = True
    write_db: bool = True

    # ---- 数据推送（跑批算完后以 HTTP API 推给下游；实现见 pusher.py）----
    # 默认关闭，不配就不推。推送失败**不影响**跑批成败（数据已经落库了），
    # 想让它影响就把 push_strict 打开。
    push_enabled: bool = False
    push_url: str = ""                       # 例：https://www.demo.com/data/sync
    # 请求头。Content-Type 会自动带上，这里主要写鉴权。
    # 例：{"Authorization": "Bearer sk_deduehdueh"}
    push_headers: Optional[Dict[str, str]] = None

    # 报文字段名。默认 {"tableName": ..., "pkId": [...], "data": [...]}，
    # 下游叫别的名字就改这里，例如 {"table": "table", "pk": "keys", "data": "rows"}
    push_body_fields: Optional[Dict[str, str]] = None
    # 额外的顶层字段，原样带进每个请求体（例：{"source": "opinion_metric_engine"}）
    push_extra_fields: Optional[Dict[str, Any]] = None
    # 推给下游的表名映射（本地表名 → 对方的 tableName）。不配就用本地表名。
    push_table_names: Optional[Dict[str, str]] = None
    # 每张表的业务主键（pkId）。不配用 pusher.DEFAULT_PUSH_PK，
    # 那份默认值对齐各表 DDL 的唯一索引。
    push_pk: Optional[Dict[str, List[str]]] = None
    # 只推这几张表。None = 本次跑批产出的全部表。
    push_tables: Optional[List[str]] = None

    push_batch_size: int = 500               # 每个 POST 带多少行
    push_timeout: float = 30.0               # 单次请求超时（秒）
    push_retries: int = 2                    # 可重试错误的重试次数（网络/5xx/429）
    push_retry_backoff: float = 1.0          # 重试退避基数，第 n 次等 backoff×2^n 秒
    push_dry_run: bool = False               # 只组报文不真发，用来验证配置
    push_strict: bool = False                # True: 推送失败也让跑批退出码非 0
    # 网关把业务失败放在响应体里：HTTP 200 + {"status": false, "code": ..., "msg": ...}。
    # 响应体是 JSON 且带 push_success_field 时，它为假就算推送失败（不重试，报文/鉴权问题）。
    # 响应体不是 JSON、或没有这个字段 → 只看 HTTP 状态码。设成 "" 关闭这项检查。
    push_success_field: str = "status"
    push_code_field: str = "code"
    push_message_field: str = "msg"
    push_trace_field: str = "trace_id"

    # ---- 5.2 下钻分析表（ads_trf_social_opinion_drill_analysis_di）----
    enable_drill_analysis: bool = True
    drill_content_limit: int = 200   # 评论摘要截断长度

    # content_snippet 掩码：除命中的关键词外，其余内容一律隐藏（口径见 domain 第 9 章）
    #   原文    我是体力一般，来回两个半小时左右
    #   掩码后  **体力一般**来回两个半小时**
    drill_mask_content: bool = True
    drill_mask_token: str = "**"          # 每段被隐藏的内容换成什么
    # run  连续的一段隐藏内容 → 一个掩码串（默认，看不出原文长度）
    # char 每个隐藏字符 → 一个掩码字符（能看出原文长度，等于泄露了信息量）
    drill_mask_mode: str = "run"
    # comment 保留这条评论的**全部**关键词（默认，同一条评论在各行里长得一样）
    # word    只保留当前行那一个 emotion_word
    drill_mask_scope: str = "comment"
    # 关键词一个都没在原文里找到时（实测约 2% —— 大模型把词抽象过了）：
    # mask_all 整条隐藏（默认）/ keep_raw 原样保留，别存一条没信息量的 `**`
    drill_mask_on_no_match: str = "mask_all"
    drill_mask_ignore_case: bool = True   # 匹配时忽略大小写，输出仍用原文大小写
    # 下钻表跟着内容表一起瘦身：content_top_words 收窄了词表，这张表的词也同步变少

    # ---- 行存在规则（见 metric_calc_domain §1.7）----
    # 配置的渠道全集。每天每个景区都会为这些渠道出一行，当天没数据就是全 0 行，
    # 这样看板的渠道列表稳定，「没人讨论」和「没接入」在数据上都是 0。
    # None = 用 domain 的 CHANNEL_NAME 全集（微博/抖音/快手/小红书/携程/同程）。
    # 数据里出现了但没写进这份配置的渠道也会保留，不会被丢掉。
    platform_codes: Optional[List[str]] = None
    # 内容表铺行的窗口：词在近 N 日内出现过就每天出一行。默认 30，
    # 覆盖看板最长的周期（本月）。调大会让这张表按词数×天数线性膨胀。
    content_row_window: int = 30

    # ---- 指标口径 ----
    # 情感得分/维度得分的口径开关。默认 weighted_v2：
    #   S = 5 × (好评率×1.0 + 中评率×0.9 + 差评率×0.5)，值域 [2.5, 5]
    # 回滚到历史口径填 "confidence_v1"（值域 [0,10]），看板刻度要同步改。
    score_formula: str = "weighted_v2"
    # v2 的三档权重（需求 2.0「正面分值/中性分值/负面分值 支持配置化」）。
    # None = 1.0 / 0.9 / 0.5；只写要改的档，例如 {"neutral": 0.8}。每档须在 [0, 1]。
    # 全引擎一套：core / dimension / macro 三张表同时生效，改了要用 repair 重刷历史分数。
    score_weights: Optional[Dict[str, float]] = None

    # ---- macro 大盘表（ads_trf_social_opinion_macro_gran_metric_di，需求 2.0）----
    enable_macro_metric: bool = True
    # 周期粒度清单。None = domain §1.10 的默认 9 个（time_granularity 的值 / 中文名）：
    #   today 今日 / latest_1d 近一日 / latest_7d 近7日 / this_week 本周 / latest_30d 近30日 /
    #   this_month 本月 / latest_60d 近60日 / latest_90d 近90日 / this_quarter 本季度
    # 每项 {"name": 写进 time_granularity 的编码, "label": 中文名（可选，不落表）,
    #       "type": rolling|week|month|quarter|year,
    #       "days": rolling 的天数, "offset": 锚点往前挪几天（默认 0）}
    # 例：加一个「近14日」→ {"name": "latest_14d", "label": "近14日", "type": "rolling", "days": 14}
    macro_granularities: Optional[List[Dict[str, Any]]] = None
    macro_wordcloud_top_n: int = 30          # 词云好/中/差各取前 N 个

    # ---- 其他 ----
    unknown_dimension_policy: str = "keep"   # keep | drop：非白名单一级维度的处理
    # >0 时内容表只保留每日每情感类型热度 TopN 的词（词云最多展示几十个，
    # 全量落库会让这张表比其他三张加起来还大）。0 = 全量。
    content_top_words: int = 0
    log_level: str = "INFO"

    def to_dict(self):
        return asdict(self)


def load_settings() -> EtlSettings:
    """加载配置：默认值 → settings_local.py → 环境变量。"""
    s = EtlSettings()

    # OPINION_IGNORE_LOCAL_SETTINGS=1 时不读 settings_local.py。
    # 单测靠它隔离：settings_local.py 里是线上库和线上推送地址，
    # 测试里跑一次 `run --push` 就会把测试数据推到真网关。
    settings_local = None
    if not _env("OPINION_IGNORE_LOCAL_SETTINGS", False, bool):
        try:
            import settings_local  # type: ignore
        except Exception:
            settings_local = None

    if settings_local is not None:
        db = getattr(settings_local, "DB", None)
        if isinstance(db, dict):
            for k, v in db.items():
                if hasattr(s.db, k):
                    setattr(s.db, k, v)
        etl = getattr(settings_local, "ETL", None)
        if isinstance(etl, dict):
            for k, v in etl.items():
                if hasattr(s, k) and k != "db":
                    setattr(s, k, v)

    # 环境变量兜底（容器/调度平台常用）
    s.db.host = _env("OPINION_DB_HOST", s.db.host)
    s.db.port = _env("OPINION_DB_PORT", s.db.port, int)
    s.db.user = _env("OPINION_DB_USER", s.db.user)
    s.db.password = _env("OPINION_DB_PASSWORD", s.db.password)
    s.db.database = _env("OPINION_DB_NAME", s.db.database)
    s.db.src_database = _env("OPINION_SRC_DB", s.db.src_database)
    s.db.ads_database = _env("OPINION_ADS_DB", s.db.ads_database)

    s.lookback_days = _env("OPINION_LOOKBACK_DAYS", s.lookback_days, int)
    s.log_level = _env("OPINION_LOG_LEVEL", s.log_level)

    # 推送配置也支持环境变量 —— 鉴权令牌不该写进任何文件。
    # OPINION_PUSH_TOKEN 会拼成 Authorization: Bearer <token>，
    # 想用别的头就直接在 settings_local 的 push_headers 里写全。
    s.push_enabled = _env("OPINION_PUSH_ENABLED", s.push_enabled, bool)
    s.push_url = _env("OPINION_PUSH_URL", s.push_url)
    # 令牌放在哪个请求头、前面带什么前缀，也都能用环境变量指定：
    #   OPINION_PUSH_TOKEN_HEADER=xpftkey  OPINION_PUSH_TOKEN_PREFIX=   → xpftkey: pfk_xxx
    # 前缀**设成空串也算设了**（= 不加前缀），所以这里不能用 _env（它把空串当没设）。
    token = _env("OPINION_PUSH_TOKEN")
    if token:
        header = _env("OPINION_PUSH_TOKEN_HEADER", "Authorization")
        prefix = os.environ.get("OPINION_PUSH_TOKEN_PREFIX", "Bearer ")
        s.push_headers = {**(s.push_headers or {}), header: f"{prefix}{token}"}
    return s
