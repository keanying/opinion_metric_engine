# -*- coding: utf-8 -*-
"""全局口径常量。

这里只放「产品规则文档写死的东西」——公式里的阈值、窗口档位、枚举映射。
任何跟环境有关的东西（库连接、跑批区间）都在 settings.py，不要混。
"""

from __future__ import annotations

# ============================================================
# 得分公式（产品规则文档 §4 游客总体评分）
#   周期得分 = 5 + [(正面总分/总数) + (负面总分/总数)] × 5 × min(1, 总数/T)
#            = 5 + (好评率 - 差评率) × 5 × min(1, 总数/T)
#   负面总分 = -1 × 负面数量，T = 100，值域 [0, 10]，中性 = 5
# ============================================================
SCORE_NEUTRAL = 5.0        # 中性基准分
SCORE_SPAN = 5.0           # 净值 ±1 对应的分数幅度
CONFIDENCE_T = 100         # 置信度阈值：总评论数不足 T 时得分向中性收敛
SCORE_MIN = 0.0
SCORE_MAX = 10.0

# 存储精度。规则文档说「保留一位小数」，那是**展示**口径；
# 数据层按 decimal(10,4) 存 4 位，由看板 round(x, 1)。
# 若在数据层就存 1 位，6~9 分区间只剩 30 个取值，会产生大量并列。
SCORE_DECIMALS = 4
RATE_DECIMALS = 4          # 占比/环比字段精度，对齐 decimal(10,4)
CONTENT_RATE_DECIMALS = 6  # 内容表热度占比 decimal(18,6)

# ============================================================
# 窗口档位（严格对齐四张表的 DDL 字段后缀，多一个少一个都会写不进去）
# ============================================================
CORE_WINDOWS = (1, 7, 30, 60, 90, 365)
PLATFORM_WINDOWS = (1, 7, 14, 30, 60, 90, 365)
DIMENSION_WINDOWS = (1, 7, 14, 30, 60, 90, 365)
CONTENT_WINDOWS = (1, 7, 30, 60, 90, 365)

# 看板日期选择器（规则文档 §1）：默认今日，支持本周=近7天、本月=近30天
PERIODS = {"today": 1, "week": 7, "month": 30}
# 趋势图时间跨度随选择器联动（规则文档 §3、§5）
TREND_SPAN = {"today": 7, "week": 14, "month": 60}

# ============================================================
# 情感枚举
# ============================================================
POSITIVE, NEUTRAL, NEGATIVE = 1, 0, -1

SENTIMENT_TYPE = {POSITIVE: "positive", NEUTRAL: "neutral", NEGATIVE: "negative"}

# sentiment_score 为空时的兜底：按 sentiment_label 文本反推。
# 采集侧不同渠道的写法不统一，这里尽量兜住。
SENTIMENT_LABEL_MAP = {
    "正向": POSITIVE, "正面": POSITIVE, "积极": POSITIVE, "好评": POSITIVE,
    "positive": POSITIVE, "pos": POSITIVE,
    "中性": NEUTRAL, "中评": NEUTRAL, "一般": NEUTRAL,
    "neutral": NEUTRAL, "neu": NEUTRAL,
    "负向": NEGATIVE, "负面": NEGATIVE, "消极": NEGATIVE, "差评": NEGATIVE,
    "negative": NEGATIVE, "neg": NEGATIVE,
}

# ============================================================
# 渠道 → 平台
# src 表的 channel 是英文码，ADS 表要同时落 platform_code 与中文 platform_name
# ============================================================
CHANNEL_NAME = {
    "weibo": "微博",
    "douyin": "抖音",
    "kuaishou": "快手",
    "xiaohongshu": "小红书",
    "ctrip": "携程",
    "tongcheng": "同程",
}

# ============================================================
# 一级维度白名单（规则文档 §5 维度评分变化趋势图固定 6 个）
# dimension_tags 里出现的一级维度若不在白名单内，按 UNKNOWN_DIMENSION_POLICY 处理
# ============================================================
L1_DIMENSIONS = ["游玩体验", "服务质量", "餐饮购物", "设施环境", "安全秩序", "交通接驳"]

# 星级均分映射：daily_rating（统计时刻均分，1~5 星口径）
# 与 emotional_score（0~10 情感得分）是两个不同刻度的字段，不要混用。
STAR_BY_SENTIMENT = {POSITIVE: 5.0, NEUTRAL: 3.0, NEGATIVE: 1.0}

DATE_FMT = "%Y%m%d"
