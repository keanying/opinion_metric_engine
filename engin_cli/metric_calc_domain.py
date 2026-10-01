# -*- coding: utf-8 -*-
"""指标口径域 —— 全部计算口径的唯一落地处。

╔══════════════════════════════════════════════════════════════════════════╗
║  这个文件是整套引擎的「口径唯一真源」。                                    ║
║  改口径只改这里，其余模块一律 import 引用，不允许在别处另算一遍。          ║
╚══════════════════════════════════════════════════════════════════════════╝

其余模块的分工（它们只做搬运，不做口径判断）：

    normalize.py   解析 JSON、把评论炸成明细        → 调用本文件的 judge_sentiment
    windows.py     日粒度事实 → 多窗口累计 + 整窗前移回补 → 纯机械，上限来自 §1.8
    builders/*.py  分组 → 滚动 → **调用本文件算指标** → 拼 travel_date 等时间字段
    validate.py    自检                              → 断言用的公式也来自本文件
    analytics.py   看板取数                          → 字段名映射来自本文件

目录
----
  第 1 章  口径常量（公式阈值、精度、窗口档位、枚举映射）
  第 2 章  原子公式（情感得分 v2/v1 / 环比 / 占比 / 星级均分 / 情感判定）
  第 3 章  日粒度事实口径（什么算一条评论、什么算一次维度提及）
  第 4 章  字段名注册表（窗口档 → 各表 DDL 字段名 + 列清单）
  第 5 章  指标计算：core 表
  第 6 章  指标计算：platform 表
  第 7 章  指标计算：dimension_score 表
  第 8 章  指标计算：content 表
  第 9 章  下钻明细表的评论原文掩码（需求 5.2 展示口径）

第 5~8 章每个函数的 docstring 都写清了 **公式 / 入参 / 出参**，
入参列名是 builder 与本文件之间的契约，改动要两边同步。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Union

import numpy as np
import pandas as pd

# ══════════════════════════════════════════════════════════════════════════
# 第 1 章  口径常量
# ══════════════════════════════════════════════════════════════════════════

# ---- 1.1 情感得分公式 ----
#
# 【v2 · 现行口径】加权占比法，值域 [2.5, 5]
#
#     S = 5 × (好评率 × 1.0 + 中评率 × 0.9 + 差评率 × 0.5)
#
#   · 好评 1.0、中评 0.9（文旅场景的中评多为弱正面）、差评 0.5（给底分，
#     避免单条差评把分数击穿）
#   · 三个占比之和恒为 1，所以 S 天然落在 [5×0.5, 5×1.0] = [2.5, 5]，
#     再做一次 clip 只是防浮点/回补带来的边界毛刺
#   · **没有置信度权重**：样本量小的问题由缺数回补（§1.8 整窗前移）解决，
#     不再在公式里对小样本打折
#
# 【v1 · 历史口径】净值 + 置信度，值域 [0, 10]，中性 = 5
#
#     S = 5 + [(正面总分/总数) + (负面总分/总数)] × 5 × min(1, 总数/T)
#       = 5 + (好评率 - 差评率) × 5 × min(1, 总数/100)
#
#   保留实现仅为对比与回滚（settings.score_formula = "confidence_v1"），
#   线上默认走 v2。两套口径的值域不同（[0,10] vs [2.5,5]），
#   **切换口径必须同步改看板刻度**，否则同一个 4.6 分会被读成「不及格」。

# v2 权重
W_POSITIVE = 1.0           # 好评权重
W_NEUTRAL = 0.9            # 中评权重（文旅中评多为弱正面）
W_NEGATIVE = 0.5           # 差评权重（底分，避免单差评击穿）
SCORE_SCALE = 5.0          # 满分刻度
SCORE_MIN = 0.0
SCORE_MAX = 5.0            # 上限：全部好评 = 5×1.0 = 5，超过一律按 5
# 无任何评论时的取值。等价于「全部按中评计」（5×0.9），
# 与 v1 时代「没有评论 → 中性基准分」的处理保持同一个语义：
# 没有评论不等于评价很差，返回 0 会让空数据景区在榜单垫底。
SCORE_EMPTY = round(SCORE_SCALE * W_NEUTRAL, 4)

# v1 常量（仅 confidence_v1 口径使用）
V1_SCORE_NEUTRAL = 5.0     # 中性基准分
V1_SCORE_SPAN = 5.0        # 情感净值 ±1 对应的分数幅度
V1_SCORE_MAX = 10.0
CONFIDENCE_T = 100         # 置信度阈值：总数不足 T 时得分向中性收敛

# 现行口径。可被 settings.score_formula 覆盖。
SCORE_FORMULA_V2 = "weighted_v2"
SCORE_FORMULA_V1 = "confidence_v1"
DEFAULT_SCORE_FORMULA = SCORE_FORMULA_V2

# ---- 1.2 存储精度 ----
# 规则文档说「保留一位小数」，那是**展示**口径；数据层按 decimal(10,4) 存 4 位，
# 由看板 round(x,1)。数据层只存 1 位会让 6~9 分区间只剩 30 个取值，排名大量并列。
SCORE_DECIMALS = 4
RATE_DECIMALS = 4          # 占比/环比，对齐 decimal(10,4)
CONTENT_RATE_DECIMALS = 6  # 内容表热度占比，对齐 decimal(18,6)

# ---- 1.3 窗口档位（严格对齐各表 DDL 的字段后缀，多一个少一个都写不进去）----
CORE_WINDOWS = (1, 7, 30, 60, 90, 365)
PLATFORM_WINDOWS = (1, 7, 14, 30, 60, 90, 365)
DIMENSION_WINDOWS = (1, 7, 14, 30, 60, 90, 365)
CONTENT_WINDOWS = (1, 7, 30, 60, 90, 365)

# ---- 1.4 看板周期（规则文档 §1 日期选择器）----
PERIODS = {"today": 1, "week": 7, "month": 30}     # 默认今日，支持近7天/近30天
TREND_SPAN = {"today": 7, "week": 14, "month": 60}  # 趋势图跨度随选择器联动
TOP_DIM_N = 3          # 规则文档 §4：维度好评取前三
SURGE_TOP_N = 10       # 规则文档 §8：负面突增词展示 10 个

# ---- 1.5 情感枚举与判定 ----
POSITIVE, NEUTRAL, NEGATIVE = 1, 0, -1
SENTIMENT_TYPE = {POSITIVE: "positive", NEUTRAL: "neutral", NEGATIVE: "negative"}

# sentiment_score 为空时按 sentiment_label 文本反推。各渠道写法不统一，尽量兜住。
SENTIMENT_LABEL_MAP = {
    "正向": POSITIVE, "正面": POSITIVE, "积极": POSITIVE, "好评": POSITIVE,
    "positive": POSITIVE, "pos": POSITIVE,
    "中性": NEUTRAL, "中评": NEUTRAL, "一般": NEUTRAL,
    "neutral": NEUTRAL, "neu": NEUTRAL,
    "负向": NEGATIVE, "负面": NEGATIVE, "消极": NEGATIVE, "差评": NEGATIVE,
    "negative": NEGATIVE, "neg": NEGATIVE,
}

# 星级均分映射：daily_rating（统计时刻均分，1~5 星刻度）
STAR_BY_SENTIMENT = {POSITIVE: 5.0, NEUTRAL: 3.0, NEGATIVE: 1.0}

# ---- 1.6 维度与渠道字典 ----
# 一级维度白名单（规则文档 §5 维度评分变化趋势图固定 6 个）
L1_DIMENSIONS = ["游玩体验", "服务质量", "餐饮购物", "设施环境", "安全秩序", "交通接驳"]

CHANNEL_NAME = {
    "weibo": "微博", "douyin": "抖音", "kuaishou": "快手",
    "xiaohongshu": "小红书", "ctrip": "携程", "tongcheng": "同程",
}

# ---- 1.7 行存在规则（「当天没有数据」要不要出一行）----
#
# 源表里「某平台/某维度/某个词当天一条都没有」表现为**这一行根本不存在**。
# 但看板要看的是近 7/14/30/60/90 日指标 —— 窗口里明明有值，行没了就查不到。
# 所以产出的行存在规则不是「当日有数据」，而是：
#
#     只要该实体在**任一窗口**内有数据，当天就出一行（当日计数为 0，窗口指标照算）
#
# 三张表的具体口径：
#   core       景区在 365 日窗口内有过评论 → 每个目标日期都出一行
#   platform   **配置的渠道全部出行**（PLATFORM_CODES / settings.platform_codes），
#              哪怕这个渠道从来没有数据，也出一条全 0 的行 —— 这样看板的渠道列表稳定，
#              「这个平台没人讨论」和「这个平台没接入」在数据上是同一种表达：0
#   dimension  维度路径在 365 日窗口内有过提及 → 出行（路径数量有限，全量铺开无压力）
#   content    词在近 CONTENT_ROW_WINDOW 日内出现过 → 出行
#
# 内容表为什么只铺 30 天而不是 365 天：词的数量没有上限，
# 「一年内出现过的所有词 × 每天一行」会让这张表比其他三张加起来还大几个量级。
# 30 天正好覆盖看板最长的周期（本月），60/90/365 日词云不在产品需求里。
CONTENT_ROW_WINDOW = 30
DIMENSION_ROW_WINDOW = 365
CORE_ROW_WINDOW = 365

# 配置的渠道全集。settings.platform_codes 可覆盖（比如某景区只接了三个平台）。
PLATFORM_CODES = list(CHANNEL_NAME)

# ---- 1.8 缺数回补：整窗前移，取到为止 ----
#
# 规则（业务方定义）：某个窗口当期没有数据时，**把整个窗口整体往前平移**，
# 一天一天挪，挪到那一天的窗口有数据就停，用**那一个窗口**的值。
#
#   2026-09-11 没有快手 → 往前挪一天到 09-10，那天有 1 条 → 09-11 的 1 日值就是 1
#
# 关键：**不是累计**。挪到 09-10 就用 09-10 这一天的数，不是 09-10 + 09-11 相加。
# 7 日档同理：[09-05, 09-11] 没数据就整体挪成 [09-04, 09-10]，再没有就继续挪。
#
# 每个窗口的最大平移天数不同（挪过头就失去时效性了）：
BACKFILL_LOOKBACK = {
    1: 5,        # 近 1 日：最多往前找 5 天
    7: 10,       # 近 7 日：最多往前挪 10 天
    14: 15,
    30: 20,
    60: 30,
    90: 60,
    365: 90,     # 业务方未指定，按递增趋势暂定 90，可配置
}
# 挪满上限仍然没有数据 → 该窗口取 0（而不是继续往前找）。

# 回补口径开关（settings.backfill_mode）：
#   shift  整窗前移取到为止（现行口径，业务方定义，默认）
#   off    不回补，当期没数据就是 0（用来对拍「回补到底影响了多少」）
BACKFILL_MODE_SHIFT = "shift"
BACKFILL_MODE_OFF = "off"
BACKFILL_MODES = (BACKFILL_MODE_SHIFT, BACKFILL_MODE_OFF)
DEFAULT_BACKFILL_MODE = BACKFILL_MODE_SHIFT

# 回补之后，**所有指标都用补齐后的数据算**，包括评论总数本身 ——
# 业务方明确要求「总数需要对数据补齐后再计算，其他指标同理，每一张表都是」。


# ---- 1.9 回补的原子粒度：渠道 ----
#
# 缺的是「快手」这一个**渠道**，不是整个景区，所以补也只能补在渠道上。
# 由此定下一条口径：
#
#   **景区粒度的计数 = 各渠道补齐后的值相加**，不是在景区粒度上另算一份。
#
# 拿业务方给的例子说：
#
#     09-10  抖音15 小红书22 微博2 携程4 快手3      合计 46
#     09-11  抖音10 小红书20 微博5 携程3 （无快手）  原始合计 38
#            ↓ 快手前移 1 天补 3
#     09-11  抖音10 小红书20 微博5 携程3 快手3      合计 41
#
# core.comment_count(09-11) = **41**，不是 38。
# 如果 core 自己在景区粒度上判断「09-11 有 38 条，不算缺数，不用挪」，
# 就会出现 core=38 而各渠道之和=41 的劈叉 —— 同一天两个总数，对不上账。
#
# 所以 build_core 吃的是**渠道粒度**的日事实（daily_core_facts_by_platform），
# 在渠道粒度上做整窗前移，再按景区汇总。好处是这两条恒等式重新硬成立：
#
#     core.comment_count            == Σ 各渠道 comment_cnt
#     platform 的 total_ 全平台字段  == Σ 各渠道对应字段
#
# 代价（要知道）：好/中/差的拆分也跟着渠道走 —— 快手补进来的 3 条带着
# 09-10 快手自己的情感分布，而不是 09-11 全景区的分布。这是对的：
# 补的就是那一天那个渠道的样本。
#
# 维度表与内容表各自在**自己的粒度**上回补（维度路径 / 词），不跟随渠道，
# 因为一条评论会命中多个维度和多个词，按渠道拆没有意义。

DATE_FMT = "%Y%m%d"


# ══════════════════════════════════════════════════════════════════════════
# 第 2 章  原子公式
# ══════════════════════════════════════════════════════════════════════════

def safe_div(num, den, default=None):
    """除法兜底。

    公式：num / den，分母为 0 或非有限值时取 default。
    入参：num/den 标量或数组；default 分母无效时的取值（None → NaN）
    出参：ndarray[float]
    """
    num = np.asarray(num, dtype="float64")
    den = np.asarray(den, dtype="float64")
    out = np.full(np.broadcast(num, den).shape, np.nan, dtype="float64")
    mask = np.isfinite(den) & (den != 0)
    np.divide(num, den, out=out, where=mask)
    if default is not None:
        out = np.where(mask, out, float(default))
    return out


def confidence(total, T: int = CONFIDENCE_T):
    """置信度权重。

    公式：min(1, 总数 / T)，T = 100
    入参：total 样本量（评论数 / 维度提及数）
    出参：[0,1] 的权重。样本不足 T 时把得分拉向中性 5，这是规则文档要求的行为。
    """
    total = np.asarray(total, dtype="float64")
    return np.clip(np.nan_to_num(total, nan=0.0) / float(T), 0.0, 1.0)


def weighted_score_from_counts(pos, neu, neg, total, decimals: int = SCORE_DECIMALS):
    """情感得分 v2（加权占比法）—— **现行口径，全引擎唯一的得分入口**。

    公式
    ----
        S = 5 × (好评率 × 1.0 + 中评率 × 0.9 + 差评率 × 0.5)
          = 5 × (好评数×1.0 + 中评数×0.9 + 差评数×0.5) / 总数

      好评 1.0 / 中评 0.9（文旅中评多为弱正面）/ 差评 0.5（底分，避免单差评击穿）。
      三个占比之和恒为 1 ⇒ 值域天然是 [2.5, 5]：全差评 2.5，全好评 5。

    入参
    ----
      pos   好评数（sentiment_score > 0）
      neu   中评数（= 0）
      neg   差评数（< 0）
      total 总数。传 pos+neu+neg 即可；显式传是为了在有回补/脏数据时
            让「分母是谁」这件事留在调用方，公式本身不猜。
      decimals 精度，默认 4（对齐 decimal(10,4)）

    出参
    ----
      [0, 5] 的得分。**不会超过 5**，超出一律按 5 截断。

    边界
    ----
      · total = 0 → 返回 SCORE_EMPTY(4.5)，等价于「全部按中评计」。
        没有评论不等于评价很差，返回 0 会让空数据景区在榜单垫底。
      · 维度得分用**同一个函数**，只把分子分母换成该维度的正/中/负/总提及数。
      · 与 v1 不同：**没有置信度权重**，小样本不再被拉向中性，
        样本量问题交给缺数回补（§1.8 整窗前移）。
    """
    pos = np.asarray(pos, dtype="float64")
    neu = np.asarray(neu, dtype="float64")
    neg = np.asarray(neg, dtype="float64")
    total = np.asarray(total, dtype="float64")

    weighted = pos * W_POSITIVE + neu * W_NEUTRAL + neg * W_NEGATIVE
    shape = np.broadcast(weighted, total).shape
    score = np.zeros(shape, dtype="float64")
    mask = np.isfinite(total) & (total > 0)
    np.divide(weighted, total, out=score, where=mask)
    score = np.clip(score * SCORE_SCALE, SCORE_MIN, SCORE_MAX)
    return np.round(np.where(mask, score, SCORE_EMPTY), decimals)


def official_score(net, total, T: int = CONFIDENCE_T, decimals: int = SCORE_DECIMALS):
    """情感得分 v1（净值版）—— 历史口径，仅供对比/回滚。

    公式：得分 = 5 + net × 5 × min(1, 总数/T)，net = 好评率 - 差评率
    入参：net 情感净值 [-1,1]；total 样本量；T 置信度阈值；decimals 精度
    出参：[0,10] 的得分
    """
    net = np.asarray(net, dtype="float64")
    score = V1_SCORE_NEUTRAL + net * V1_SCORE_SPAN * confidence(total, T)
    score = np.clip(np.nan_to_num(score, nan=V1_SCORE_NEUTRAL), SCORE_MIN, V1_SCORE_MAX)
    return np.round(score, decimals)


def official_score_from_counts(pos, neg, total, T: int = CONFIDENCE_T,
                               decimals: int = SCORE_DECIMALS):
    """情感得分 v1（计数版）—— 历史口径，仅供对比/回滚。

    公式（旧规则文档 §4 原文）：
        周期得分 = 5 + [(正面总分/总评论数) + (负面总分/总评论数)] × 5 × min(1, 总评论数/T)
        其中 负面总分 = -1 × 负面评价数量，展开即 5 + (好评率 - 差评率) × 5 × conf

    入参：pos 正面数；neg 负面数；total 总数；T=100；decimals=4
    出参：[0,10] 的得分；total = 0 时返回 5（中性）。
    """
    pos = np.asarray(pos, dtype="float64")
    neg = np.asarray(neg, dtype="float64")
    total = np.asarray(total, dtype="float64")
    shape = np.broadcast(pos, neg, total).shape
    net = np.zeros(shape, dtype="float64")
    mask = np.isfinite(total) & (total > 0)
    np.divide(pos - neg, total, out=net, where=mask)
    return np.where(mask, official_score(net, total, T, decimals),
                    round(V1_SCORE_NEUTRAL, decimals))


def sentiment_score_from_counts(pos, neu, neg, total,
                                formula: str = DEFAULT_SCORE_FORMULA,
                                decimals: int = SCORE_DECIMALS):
    """得分口径分发器 —— 引擎里所有算分的地方都走这里。

    入参：pos/neu/neg/total 计数；formula 口径开关
          （"weighted_v2" 现行 / "confidence_v1" 历史，来自 settings.score_formula）
    出参：得分。v2 值域 [0,5]，v1 值域 [0,10] —— 切换口径要同步改看板刻度。
    """
    if formula == SCORE_FORMULA_V1:
        return official_score_from_counts(pos, neg, total, decimals=decimals)
    if formula != SCORE_FORMULA_V2:
        raise ValueError(f"未知的得分口径 score_formula={formula!r}，"
                         f"可选 {SCORE_FORMULA_V2!r} / {SCORE_FORMULA_V1!r}")
    return weighted_score_from_counts(pos, neu, neg, total, decimals=decimals)


def growth_rate(cur, prev, default=0.0, decimals: int = RATE_DECIMALS):
    """环比增长率。

    公式：(本期 - 上期) / 上期
    入参：cur 本期值；prev 上期值；default 上期为 0 时的取值；decimals=4
    出参：环比比率（0.2 表示 +20%）

    边界：上期为 0 时返回 **default(0) 而不是 inf** ——
          decimal(10,4) 存不下 inf；「从 0 涨到 100」的正确表达是「新增」，
          由展示层根据「上期=0 且 本期>0」自行标注。
    """
    cur = np.asarray(cur, dtype="float64")
    prev = np.asarray(prev, dtype="float64")
    out = np.full(np.broadcast(cur, prev).shape, float(default), dtype="float64")
    mask = np.isfinite(prev) & (prev != 0)
    np.divide(cur - prev, prev, out=out, where=mask)
    return np.round(out, decimals)


def ratio(num, den, decimals: int = RATE_DECIMALS, default=0.0):
    """占比（好评率 / 差评率 / 平台占比 / 热度占比 都走这里）。

    公式：分子 / 分母
    入参：num 分子；den 分母；decimals 精度；default 分母为 0 时的取值
    出参：[0,1] 的占比（分子分母同源时）
    """
    return np.round(safe_div(num, den, default=default), decimals)


def star_rating(pos, neu, neg, total, decimals: int = SCORE_DECIMALS):
    """daily_rating —— 统计时刻均分。

    公式：(正面数×5 + 中性数×3 + 负面数×1) / 总数
    入参：pos/neu/neg/total 各类计数
    出参：[1,5] 的星级均分，总数为 0 时取 3（中性）

    注意：它与 emotional_score 是**两个不同的东西**，虽然 v2 之后数值范围看着接近
    （均分 [1,5] vs 情感得分 [2.5,5]）：前者是星级均分（正5/中3/差1），
    后者是情感得分（权重 1.0/0.9/0.5 再 ×5）。看板并排展示时要标清楚，不要互相校验。
    DDL 只写了「均分」没给公式，这是本引擎的定义，要改口径改这一处。
    """
    pos = np.asarray(pos, dtype="float64")
    neu = np.asarray(neu, dtype="float64")
    neg = np.asarray(neg, dtype="float64")
    total = np.asarray(total, dtype="float64")
    num = (pos * STAR_BY_SENTIMENT[POSITIVE]
           + neu * STAR_BY_SENTIMENT[NEUTRAL]
           + neg * STAR_BY_SENTIMENT[NEGATIVE])
    out = np.full(np.broadcast(num, total).shape, np.nan, dtype="float64")
    mask = np.isfinite(total) & (total > 0)
    np.divide(num, total, out=out, where=mask)
    return np.round(np.where(mask, out, 3.0), decimals)


def judge_sentiment(score: Any, label: Any = None) -> int:
    """好评 / 中评 / 差评的判定 —— **全引擎唯一的情感判定口径**。

    规则：优先 sentiment_score（数值口径，跨渠道一致）：>0 好评、=0 中评、<0 差评；
          score 为空才回退 sentiment_label 文本映射（先精确后包含）；
          两者都拿不到按中性处理。

    入参：score 源表 sentiment_score；label 源表 sentiment_label
    出参：1 / 0 / -1

    边界：丢弃判不出情感的评论会让「评论总数」对不上源表，比归中性更糟。
    """
    if score is not None and not (isinstance(score, float) and np.isnan(score)):
        try:
            v = float(score)
            return POSITIVE if v > 0 else (NEGATIVE if v < 0 else NEUTRAL)
        except (TypeError, ValueError):
            pass
    if label is not None:
        key = str(label).strip().lower()
        for k, v in SENTIMENT_LABEL_MAP.items():
            if key == k.lower():
                return v
        for k, v in SENTIMENT_LABEL_MAP.items():
            if k.lower() in key:
                return v
    return NEUTRAL


# ---- Series 便捷包装：builder 里直接对列用，返回值带回原 index ----
def s_score(pos: pd.Series, neu: pd.Series, neg: pd.Series, total: pd.Series,
            formula: str = DEFAULT_SCORE_FORMULA) -> pd.Series:
    return pd.Series(
        sentiment_score_from_counts(pos.to_numpy(), neu.to_numpy(), neg.to_numpy(),
                                    total.to_numpy(), formula=formula),
        index=pos.index)


def s_ratio(num: pd.Series, den: pd.Series, decimals: int = RATE_DECIMALS,
            default=0.0) -> pd.Series:
    return pd.Series(ratio(num.to_numpy(), den.to_numpy(), decimals, default),
                     index=num.index)


def s_growth(cur: pd.Series, prev: pd.Series, default=0.0) -> pd.Series:
    return pd.Series(growth_rate(cur.to_numpy(), prev.to_numpy(), default=default),
                     index=cur.index)


def has_window_data(w: pd.DataFrame, field: str, window: int) -> pd.Series:
    """行存在规则（见 §1.7）：该实体在近 window 日窗口内**真的**有数据。

    入参：w 滚动后的宽表；field 计数字段名（如 comment_count / word_cnt）；window 窗口天数
    出参：bool Series。True = 当天要出行，哪怕当日计数是 0。

    口径要点：开了整窗前移回补时，优先看 `raw_<field>_<N>d`（**回补之前**的真实值）。
    看回补后的值会让铺行范围被悄悄放大 —— 「近 30 日出现过的词」变成
    「近 30+20 日出现过的词」，内容表按词数×天数线性膨胀。
    """
    n = int(window)
    for col in (f"raw_{field}_{n}d", f"{field}_{n}d"):
        if col in w.columns:
            return pd.to_numeric(w[col], errors="coerce").fillna(0.0) > 0
    raise KeyError(f"行存在规则需要 raw_{field}_{n}d 或 {field}_{n}d，但滚动结果里都没有")


# ══════════════════════════════════════════════════════════════════════════
# 第 3 章  日粒度事实口径
#
# 「什么算一条评论、什么算一次维度提及、什么算一次词出现」——
# 这三条计数规则决定了后面所有指标的分子分母，所以也放在本文件里。
# ══════════════════════════════════════════════════════════════════════════

def daily_core_facts(comment_facts: pd.DataFrame) -> pd.DataFrame:
    """景区 × 日 的评论计数。

    口径：规则文档 §2「主子评论均作为独立个体计数」——
          level_2 子评论不折叠，一条就是一条。

    入参：comment_facts（评论粒度明细，需含 scenic_spot_code / travel_date /
          is_positive / is_neutral / is_negative）
    出参：scenic_spot_code, travel_date, scenic_spot_name,
          comment_count, positive_count, neutral_count, negative_count
    """
    cols = ["scenic_spot_code", "travel_date", "scenic_spot_name",
            "comment_count", "positive_count", "neutral_count", "negative_count"]
    if comment_facts.empty:
        return pd.DataFrame(columns=cols)
    return (comment_facts
            .groupby(["scenic_spot_code", "travel_date"], as_index=False)
            .agg(scenic_spot_name=("scenic_spot_name", "first"),
                 comment_count=("sentiment", "size"),
                 positive_count=("is_positive", "sum"),
                 neutral_count=("is_neutral", "sum"),
                 negative_count=("is_negative", "sum")))[cols]


def daily_core_facts_by_platform(comment_facts: pd.DataFrame) -> pd.DataFrame:
    """景区 × **平台** × 日 的评论计数（字段与 daily_core_facts 同名）。

    为什么 core 表要按平台粒度取事实：**回补的原子粒度是渠道**（domain §1.9）。
    09-11 缺的是「快手」这一个渠道，不是整个景区，所以补也只能在渠道上补；
    景区总数是各渠道补齐后相加出来的结果，不是另算一份。

    入参：comment_facts（评论粒度明细）
    出参：scenic_spot_code, platform_code, travel_date, scenic_spot_name,
          platform_name, comment_count, positive_count, neutral_count, negative_count
    """
    cols = ["scenic_spot_code", "platform_code", "travel_date", "scenic_spot_name",
            "platform_name"] + CORE_COUNT_FIELDS
    if comment_facts.empty:
        return pd.DataFrame(columns=cols)
    return (comment_facts
            .groupby(["scenic_spot_code", "platform_code", "travel_date"], as_index=False)
            .agg(scenic_spot_name=("scenic_spot_name", "first"),
                 platform_name=("platform_name", "first"),
                 comment_count=("sentiment", "size"),
                 positive_count=("is_positive", "sum"),
                 neutral_count=("is_neutral", "sum"),
                 negative_count=("is_negative", "sum")))[cols]


def daily_platform_facts(comment_facts: pd.DataFrame) -> pd.DataFrame:
    """景区 × 平台 × 日 的评论计数。

    口径：平台 = 源表 channel。好评数只看 sentiment_score > 0。

    入参：comment_facts
    出参：scenic_spot_code, platform_code, travel_date, scenic_spot_name,
          platform_name, plat_cnt（评论数）, plat_pos（好评数）
    """
    cols = ["scenic_spot_code", "platform_code", "travel_date",
            "scenic_spot_name", "platform_name", "plat_cnt", "plat_pos"]
    if comment_facts.empty:
        return pd.DataFrame(columns=cols)
    return (comment_facts
            .groupby(["scenic_spot_code", "platform_code", "travel_date"], as_index=False)
            .agg(scenic_spot_name=("scenic_spot_name", "first"),
                 platform_name=("platform_name", "first"),
                 plat_cnt=("sentiment", "size"),
                 plat_pos=("is_positive", "sum")))[cols]


def daily_dimension_facts(dim_facts: pd.DataFrame, keys: Sequence[str]) -> pd.DataFrame:
    """维度提及的日粒度计数（按传入的层级键分组）。

    口径：规则文档 §4/§5「多维度评论各维度独立计数」——
          一条评论命中 3 个维度就是 3 次提及；每次提及的情感取
          **该维度自己的 sentiment**，不跟随整条评论
          （「风景好但停车难」里交通接驳是负面）。

    入参：dim_facts（维度粒度明细）；keys 分组键，取以下三者之一：
          [景区, dim1] / [景区, dim1, dim2] / [景区, dim1, dim2, dim3]
    出参：keys + travel_date + mention_cnt / mention_pos / mention_neu / mention_neg
    """
    keys = list(keys)
    cols = keys + ["travel_date", "mention_cnt", "mention_pos",
                   "mention_neu", "mention_neg"]
    if dim_facts.empty:
        return pd.DataFrame(columns=cols)
    return (dim_facts.groupby(keys + ["travel_date"], as_index=False)
            .agg(mention_cnt=("dim_sentiment", "size"),
                 mention_pos=("is_positive", "sum"),
                 mention_neu=("is_neutral", "sum"),
                 mention_neg=("is_negative", "sum")))[cols]


def resolve_word_emotion_type(rows: pd.DataFrame, known: pd.DataFrame,
                              keys: Sequence[str], date_col: str,
                              type_col: str = "emotion_type") -> pd.Series:
    """给内容表的每一行定情感归属，包括「当天没出现」的补零行。

    口径：取该词**最近一次出现**时的归类；那天之前还没出现过就用它第一次出现时的归类。

    为什么不能留空：emotion_type 是词云的切换维度（正/中/负），
    留空的行既进不了任何一个词云，又占着唯一键的位置。
    为什么不按整段历史的主类：词的情感会随事件翻转（「免费」在免票日是正面、
    在退票纠纷里是负面），沿用最近一次的归类比按全期主类更贴近当下。

    入参：rows 需要定类型的行（含 keys + date_col）；
          known 该词实际出现过的日子及其归类（daily_keyword_facts 的产物）
    出参：与 rows 等长、同 index 的 Series
    """
    if rows.empty:
        return pd.Series(dtype=object, index=rows.index)
    keys = list(keys)
    left = rows[keys + [date_col]].copy()
    left["_dt"] = pd.to_datetime(left[date_col].astype(str), format="%Y%m%d")
    left["_pos"] = np.arange(len(left))

    right = known[keys + [date_col, type_col]].dropna(subset=[type_col]).copy()
    if right.empty:
        return pd.Series([SENTIMENT_TYPE[NEUTRAL]] * len(rows), index=rows.index)
    right["_dt"] = pd.to_datetime(right[date_col].astype(str), format="%Y%m%d")
    right = right.drop(columns=[date_col]).sort_values("_dt")

    back = pd.merge_asof(left.sort_values("_dt"), right, by=keys, on="_dt",
                         direction="backward")
    fwd = pd.merge_asof(left.sort_values("_dt"), right, by=keys, on="_dt",
                        direction="forward")
    out = back[type_col].fillna(pd.Series(fwd[type_col].to_numpy(), index=back.index))
    out = out.fillna(SENTIMENT_TYPE[NEUTRAL])
    out.index = back["_pos"].to_numpy()
    return pd.Series(out.sort_index().to_numpy(), index=rows.index)


# 词归类的优先级：同一天同一个词在多类评论里都出现时，按出现次数取多的那类；
# 次数并列时按此优先级 —— 负面信号宁可露出，不要被淹没。
WORD_TYPE_PRIORITY = {"negative": 0, "positive": 1, "neutral": 2}


def daily_keyword_facts(keyword_facts: pd.DataFrame) -> pd.DataFrame:
    """景区 × 日 × 词 的出现次数，并定该词当天的情感归属。

    口径：
      · 词频 —— 同一条评论里重复出现的同一个词只算一次（在 normalize 阶段已去重），
        否则一条刷屏评论能把词频顶上天；
      · 情感归属 —— 取该词当天出现次数最多的那一类评论的情感，
        并列时按 WORD_TYPE_PRIORITY（负 > 正 > 中）。
        必须唯一，否则会撞内容表 (travel_date, scenic_spot_code, emotion_word) 唯一索引；
      · 窗口累计基于**不分情感的日总词频**，否则某天归类翻转会让 7d 累计凭空少一段。

    入参：keyword_facts（词粒度明细：景区/日期/词/该评论情感类型）
    出参：scenic_spot_code, travel_date, emotion_word, word_cnt, emotion_type
    """
    cols = ["scenic_spot_code", "travel_date", "emotion_word", "word_cnt", "emotion_type"]
    if keyword_facts.empty:
        return pd.DataFrame(columns=cols)

    by_type = (keyword_facts
               .groupby(["scenic_spot_code", "travel_date", "emotion_word", "emotion_type"],
                        as_index=False)
               .agg(cnt=("emotion_word", "size")))
    by_type["_prio"] = by_type["emotion_type"].map(WORD_TYPE_PRIORITY).fillna(9)
    by_type = by_type.sort_values(
        ["scenic_spot_code", "travel_date", "emotion_word", "cnt", "_prio"],
        ascending=[True, True, True, False, True])
    dominant = by_type.drop_duplicates(
        subset=["scenic_spot_code", "travel_date", "emotion_word"], keep="first")[
        ["scenic_spot_code", "travel_date", "emotion_word", "emotion_type"]]

    daily = (keyword_facts
             .groupby(["scenic_spot_code", "travel_date", "emotion_word"], as_index=False)
             .agg(word_cnt=("emotion_word", "size")))
    return daily.merge(dominant, on=["scenic_spot_code", "travel_date", "emotion_word"],
                       how="left")[cols]


# ══════════════════════════════════════════════════════════════════════════
# 第 4 章  字段名注册表
#
# 各表 DDL 的字段命名毫无规律（1d 无后缀、7d 有的叫 _7d 有的叫 week_、
# 30d 有的叫 mon_），全部在这里查表，builder 里不出现任何字面字段名。
# ══════════════════════════════════════════════════════════════════════════

# ---- core 表 ----
CORE_SUFFIX: Dict[int, str] = {1: "", 7: "_7d", 30: "_30d", 60: "_60d",
                               90: "_90d", 365: "_365d"}
CORE_CNT_GROWTH: Dict[int, str] = {
    1: "comment_count_dod", 7: "comment_count_wow", 30: "comment_count_mom",
    60: "comment_count_60dod", 90: "comment_count_90dod", 365: "comment_count_365dod"}
CORE_COUNT_FIELDS = ["comment_count", "positive_count", "neutral_count", "negative_count"]

CORE_COLUMNS: List[str] = (
    ["scenic_spot_name", "scenic_spot_code"] + CORE_COUNT_FIELDS
    + [f"positive_growth_rate{CORE_SUFFIX[w]}" for w in CORE_WINDOWS]
    + [f"neutral_growth_rate{CORE_SUFFIX[w]}" for w in CORE_WINDOWS]
    + [f"negative_growth_rate{CORE_SUFFIX[w]}" for w in CORE_WINDOWS]
    + ["daily_rating"]
    + [f"emotional_score{CORE_SUFFIX[w]}" for w in CORE_WINDOWS]
    + [CORE_CNT_GROWTH[w] for w in CORE_WINDOWS]
    + [f"positive_rate{CORE_SUFFIX[w]}" for w in CORE_WINDOWS]
    + [f"neutral_rate{CORE_SUFFIX[w]}" for w in CORE_WINDOWS]
    + [f"negative_rate{CORE_SUFFIX[w]}" for w in CORE_WINDOWS]
    + ["publish_time", "etl_time", "travel_date"])

# ---- platform 表 ----
PLAT_CNT: Dict[int, str] = {1: "comment_cnt", 7: "comment_cnt_7d", 14: "comment_cnt_14d",
                            30: "comment_cnt_30d", 60: "comment_cnt_60d",
                            90: "comment_cnt_90d", 365: "comment_cnt_365d"}
PLAT_TOT_POS: Dict[int, str] = {1: "positive_count", 7: "positive_7d_count",
                                14: "positive_14d_count", 30: "positive_30d_count",
                                60: "positive_60d_count", 90: "positive_90d_count",
                                365: "positive_365d_count"}
PLAT_POS: Dict[int, str] = {1: "plat_positive_count", 7: "plat_positive_7d_count",
                            14: "plat_positive_14d_count", 30: "plat_positive_30d_count",
                            60: "plat_positive_60d_count", 90: "plat_positive_90d_count",
                            365: "plat_positive_365d_count"}
PLAT_TOT_RATE: Dict[int, str] = {1: "total_good_rate_1d", 7: "week_total_good_rate",
                                 14: "total_good_rate_14d", 30: "mon_total_good_rate",
                                 60: "total_good_rate_60d", 90: "total_good_rate_90d",
                                 365: "total_good_rate_365d"}
PLAT_RATE: Dict[int, str] = {1: "good_rate_1d", 7: "week_good_rate",
                             14: "good_rate_14d", 30: "mon_good_rate",
                             60: "good_rate_60d", 90: "good_rate_90d",
                             365: "good_rate_365d"}
PLAT_TOT_WOW: Dict[int, str] = {1: "total_good_rate_wow_1d", 7: "week_total_good_rate_wow",
                                14: "total_good_rate_wow_14d", 30: "mon_total_good_rate_wow",
                                60: "total_good_rate_wow_60d", 90: "total_good_rate_wow_90d",
                                365: "total_good_rate_wow_365d"}
PLAT_WOW: Dict[int, str] = {1: "good_rate_wow_1d", 7: "week_good_rate_wow",
                            14: "good_rate_wow_14d", 30: "mon_good_rate_wow",
                            60: "good_rate_wow_60d", 90: "good_rate_wow_90d",
                            365: "good_rate_wow_365d"}

PLATFORM_COLUMNS: List[str] = (
    ["scenic_spot_code", "scenic_spot_name", "platform_name", "platform_code",
     "comment_rate", "positive_rate", "growth_rate"]
    + [PLAT_CNT[w] for w in PLATFORM_WINDOWS]
    + [x for w in PLATFORM_WINDOWS for x in (PLAT_TOT_WOW[w], PLAT_WOW[w])]
    + [x for w in PLATFORM_WINDOWS for x in (PLAT_TOT_POS[w], PLAT_POS[w])]
    + [x for w in PLATFORM_WINDOWS for x in (PLAT_TOT_RATE[w], PLAT_RATE[w])]
    + ["etl_time", "publish_time", "travel_date"])

# ---- dimension_score 表 ----
DIM_SUFFIX: Dict[int, str] = {1: "", 7: "_7d", 14: "_14d", 30: "_30d",
                              60: "_60d", 90: "_90d", 365: "_365d"}
DIMENSION_COLUMNS: List[str] = (
    ["scenic_spot_code", "scenic_spot_name",
     "dimension_level1", "dimension_level2", "dimension_level3"]
    + [f"dimension_{lvl}_score{DIM_SUFFIX[w]}"
       for w in DIMENSION_WINDOWS for lvl in (1, 2, 3)]
    + ["publish_time", "etl_time", "travel_date"])

# 维度三层的分组键
DIM_KEYS_L1 = ["scenic_spot_code", "dimension_level1"]
DIM_KEYS_L2 = ["scenic_spot_code", "dimension_level1", "dimension_level2"]
DIM_KEYS_L3 = ["scenic_spot_code", "dimension_level1", "dimension_level2",
               "dimension_level3"]
DIM_LEVEL_KEYS = {1: DIM_KEYS_L1, 2: DIM_KEYS_L2, 3: DIM_KEYS_L3}
DIM_VALUE_FIELDS = ["mention_cnt", "mention_pos", "mention_neu", "mention_neg"]

# ---- content 表 ----
# emotion_value 就是 1 日口径，DDL 里没有 emotion_value_1d
CONTENT_VALUE: Dict[int, str] = {1: "emotion_value", 7: "emotion_value_7d",
                                 30: "emotion_value_30d", 60: "emotion_value_60d",
                                 90: "emotion_value_90d", 365: "emotion_value_365d"}
CONTENT_RATE: Dict[int, str] = {w: f"emotion_rate_{w}d" for w in CONTENT_WINDOWS}

CONTENT_COLUMNS: List[str] = (
    ["scenic_spot_code", "scenic_spot_name", "emotion_type", "emotion_word", "emotion_rank"]
    + [CONTENT_VALUE[w] for w in CONTENT_WINDOWS]
    + [CONTENT_RATE[w] for w in CONTENT_WINDOWS]
    + ["publish_time", "etl_time", "travel_date"])


# ══════════════════════════════════════════════════════════════════════════
# 第 5 章  指标计算：core 表（规则文档 §2 核心数值卡 / §3 趋势图 / §4 总体评分）
# ══════════════════════════════════════════════════════════════════════════

def core_metrics(w: pd.DataFrame,
                 formula: str = DEFAULT_SCORE_FORMULA) -> pd.DataFrame:
    """算出 core 表的全部指标列。

    公式
    ----
      好评率_Nd      = 近 N 日好评数 / 近 N 日总评论数           （中评率、差评率同理）
      好评环比_Nd    = (近 N 日好评数 - 上一个 N 日好评数) / 上一个 N 日好评数
      评论数环比_Nd  = (近 N 日评论数 - 上一个 N 日评论数) / 上一个 N 日评论数
      情感得分_Nd    = 5 × (好评率×1.0 + 中评率×0.9 + 差评率×0.5)   ← v2，值域 [2.5, 5]
      daily_rating   = (好评×5 + 中评×3 + 差评×1) / 总数
      comment_count 等计数     = 回补后的 1 日窗口值

    入参（builder 滚动后提供的宽表，缺列即报错，属于契约）
    ----
      {metric}_{N}d        近 N 日累计，metric ∈ CORE_COUNT_FIELDS，N ∈ CORE_WINDOWS
      {metric}_prev_{N}d   上一个 N 日累计（用于环比）
      formula              得分口径，默认 weighted_v2，来自 settings.score_formula

      **入参已经是回补后的值**（整窗前移，domain §1.8）：`comment_count_1d` 不是
      「当天真实收到多少条」，而是「最近一个有数据的 1 日窗口的条数」。
      业务方要求总数也用补齐后的数据算，所以这里不再区分「真实计数」与「回补计数」。

    出参
    ----
      DataFrame，列 = CORE_COLUMNS 去掉 [scenic_spot_name, scenic_spot_code,
      publish_time, etl_time, travel_date]（这些由 builder 拼），index 与入参一致。

    口径要点
    ----
      · **所有字段都用回补后的数据算**，包括计数本身（业务方要求）。
      · 环比比的是「实际用的那个窗口」与它之前一个周期 —— 窗口挪了，
        上一周期跟着一起挪，否则拿 09-10 的本期去比 09-04 的上期，口径就错位了。
    """
    out = pd.DataFrame(index=w.index)

    for c in CORE_COUNT_FIELDS:
        out[c] = w[f"{c}_1d"].round().astype("int64")

    for n in CORE_WINDOWS:
        sfx = CORE_SUFFIX[n]
        pos, neu = w[f"positive_count_{n}d"], w[f"neutral_count_{n}d"]
        neg, tot = w[f"negative_count_{n}d"], w[f"comment_count_{n}d"]

        out[f"positive_rate{sfx}"] = s_ratio(pos, tot)
        out[f"neutral_rate{sfx}"] = s_ratio(neu, tot)
        out[f"negative_rate{sfx}"] = s_ratio(neg, tot)
        out[f"emotional_score{sfx}"] = s_score(pos, neu, neg, tot, formula)

        out[f"positive_growth_rate{sfx}"] = s_growth(w[f"positive_count_{n}d"],
                                                     w[f"positive_count_prev_{n}d"])
        out[f"neutral_growth_rate{sfx}"] = s_growth(w[f"neutral_count_{n}d"],
                                                    w[f"neutral_count_prev_{n}d"])
        out[f"negative_growth_rate{sfx}"] = s_growth(w[f"negative_count_{n}d"],
                                                     w[f"negative_count_prev_{n}d"])
        out[CORE_CNT_GROWTH[n]] = s_growth(w[f"comment_count_{n}d"],
                                           w[f"comment_count_prev_{n}d"])

    out["daily_rating"] = star_rating(w["positive_count_1d"], w["neutral_count_1d"],
                                      w["negative_count_1d"], w["comment_count_1d"])
    return out


# ══════════════════════════════════════════════════════════════════════════
# 第 6 章  指标计算：platform 表（规则文档 §6 平台评论分布）
# ══════════════════════════════════════════════════════════════════════════

def platform_metrics(w: pd.DataFrame) -> pd.DataFrame:
    """算出 platform 表的全部指标列。

    公式
    ----
      comment_rate（平台评论占比）= 当日该平台评论数 / 当日该景区总评论数
      positive_rate（平台好评占比）= 当日该平台好评数 / 当日该平台评论数
      growth_rate（平台环比）      = (当日平台评论数 - 昨日) / 昨日
      好评占比_Nd（平台/全平台）    = 近 N 日好评数 / 近 N 日评论数
      好评环比_Nd（平台/全平台）    = (近 N 日好评数 - 上一个 N 日好评数) / 上一个 N 日好评数

    入参
    ----
      plat_cnt_{N}d / plat_pos_{N}d            该平台近 N 日评论数 / 好评数
      plat_cnt_prev_{N}d / plat_pos_prev_{N}d  上一周期
      total_cnt_{N}d / total_pos_{N}d          全平台（该景区）近 N 日
      total_pos_prev_{N}d                      全平台上一周期好评数
      day_total_cnt                            各渠道**回补后**的 1 日值之和（占比的分母）
      N ∈ PLATFORM_WINDOWS

      入参已是回补后的值（整窗前移，domain §1.8）。

    出参
    ----
      DataFrame，列 = PLATFORM_COLUMNS 去掉景区/平台名与时间字段。

    口径要点
    ----
      · DDL 注释写的 `好评占比=总评数/好评数` 是**写反了**，
        本实现按数学上正确的「好评数/总评数」，否则值会大于 1 且无法解释。
      · 一行里同时存平台口径与全平台口径，排行榜要两者并排比较。
      · 占比的分母用「各渠道回补后的值之和」而不是景区当日真实总数 ——
        各渠道各自平移，分母必须跟分子同源，占比合计才还是 1。
    """
    out = pd.DataFrame(index=w.index)

    out["comment_rate"] = s_ratio(w["plat_cnt_1d"], w["day_total_cnt"])
    out["positive_rate"] = s_ratio(w["plat_pos_1d"], w["plat_cnt_1d"])
    out["growth_rate"] = s_growth(w["plat_cnt_1d"], w["plat_cnt_prev_1d"])

    for n in PLATFORM_WINDOWS:
        pc, pp = w[f"plat_cnt_{n}d"], w[f"plat_pos_{n}d"]
        tc, tp = w[f"total_cnt_{n}d"], w[f"total_pos_{n}d"]

        out[PLAT_CNT[n]] = pc.round().astype("int64")
        out[PLAT_TOT_POS[n]] = tp.round().astype("int64")
        out[PLAT_POS[n]] = pp.round().astype("int64")
        out[PLAT_TOT_RATE[n]] = s_ratio(tp, tc)
        out[PLAT_RATE[n]] = s_ratio(pp, pc)
        out[PLAT_TOT_WOW[n]] = s_growth(tp, w[f"total_pos_prev_{n}d"])
        out[PLAT_WOW[n]] = s_growth(pp, w[f"plat_pos_prev_{n}d"])
    return out


# ══════════════════════════════════════════════════════════════════════════
# 第 7 章  指标计算：dimension_score 表（规则文档 §4 维度好评 / §5 维度趋势）
# ══════════════════════════════════════════════════════════════════════════

def dimension_metrics(w: pd.DataFrame,
                      formula: str = DEFAULT_SCORE_FORMULA) -> pd.DataFrame:
    """算出维度表的 21 个得分列（3 层 × 7 个窗口档）。

    公式
    ----
      dimension_3_score_Nd = 5 × (该 dim1/dim2/dim3 路径近 N 日的
                                  好评率×1.0 + 中评率×0.9 + 差评率×0.5)
      dimension_2_score_Nd = 同式，样本换成该 dim1/dim2 下所有提及
      dimension_1_score_Nd = 同式，样本换成该 dim1 下所有提及

      **与游客综合情感得分是同一个公式**（sentiment_score_from_counts），
      区别只在样本范围：综合得分用全部评论，维度得分用该维度下的提及。
      值域同样是 [2.5, 5]，不会超过 5。

    入参
    ----
      L{lvl}_mention_cnt_{N}d / L{lvl}_mention_pos_{N}d /
      L{lvl}_mention_neu_{N}d / L{lvl}_mention_neg_{N}d
      lvl ∈ {1,2,3}，N ∈ DIMENSION_WINDOWS
      formula 得分口径，默认 weighted_v2

      入参已是回补后的值（整窗前移，domain §1.8），每条维度路径各自平移。

    出参
    ----
      DataFrame，列 = dimension_{1,2,3}_score{窗口后缀}。

    口径要点
    ----
      · 三层各自独立按公式算，口径自洽：同一个 dim1 下所有行的 dimension_1_score
        必然相同，BI 下钻能对上账（validate 里有这条断言）。
      · v2 口径下不再有置信度权重，小样本维度不会被压向中性 ——
        提及数少时得分波动更大，这部分由缺数回补（整窗前移）来平滑。
      · DDL 里 90d/365d 的注释写成「维度TOP1/TOP2/TOP3得分」，与短窗口的
        层级语义矛盾，本实现统一按层级口径。要改成 Top-N 需要单开开关。
    """
    out = pd.DataFrame(index=w.index)
    for n in DIMENSION_WINDOWS:
        for lvl in (1, 2, 3):
            pos = w[f"L{lvl}_mention_pos_{n}d"]
            neu = w[f"L{lvl}_mention_neu_{n}d"]
            neg = w[f"L{lvl}_mention_neg_{n}d"]
            tot = w[f"L{lvl}_mention_cnt_{n}d"]
            out[f"dimension_{lvl}_score{DIM_SUFFIX[n]}"] = s_score(
                pos, neu, neg, tot, formula)
    return out


# ══════════════════════════════════════════════════════════════════════════
# 第 8 章  指标计算：content 表（规则文档 §7 关键词云 / §8 负面突增词）
# ══════════════════════════════════════════════════════════════════════════

def content_metrics(w: pd.DataFrame) -> pd.DataFrame:
    """算出内容表的词频与热度占比列。

    公式
    ----
      emotion_value      = 该词当日出现次数
      emotion_value_Nd   = 该词近 N 日累计出现次数
      emotion_rate_Nd    = 近 N 日累计出现次数 / 近 N 日总评论数

    入参
    ----
      word_cnt_{N}d        该词近 N 日累计出现次数
      comment_count_{N}d   该景区近 N 日总评论数（分母，取自 core 表同一套滚动结果）
      N ∈ CONTENT_WINDOWS

    出参
    ----
      DataFrame，列 = emotion_value* + emotion_rate_*。

    口径要点
    ----
      · 分母与 core 表**同源**，否则热度占比跟评论总数对不上账。
      · **突增量不落表**：它是「本期 - 上期」的两期差值，随所选周期变化
        （今日榜用 1d 差、本周用 7d 差、本月用 30d 差），一行存不下三套。
        由 word_surge() 或 sql/queries.sql §8 按周期现算。
    """
    out = pd.DataFrame(index=w.index)
    for n in CONTENT_WINDOWS:
        out[CONTENT_VALUE[n]] = w[f"word_cnt_{n}d"].round().astype("int64")
        out[CONTENT_RATE[n]] = s_ratio(w[f"word_cnt_{n}d"], w[f"comment_count_{n}d"],
                                       decimals=CONTENT_RATE_DECIMALS)
    return out


def content_rank(values: pd.Series, groups: List[pd.Series]) -> pd.Series:
    """emotion_rank：同日同情感类型内按当日出现次数降序的名次。

    入参：values 当日出现次数；groups 分组列（景区、日期、情感类型）
    出参：int64 名次，从 1 开始，同值按出现顺序打破并列（method="first"）
    """
    return (values.groupby(groups).rank(method="first", ascending=False).astype("int64"))


def word_surge(cur_value, prev_value):
    """负面突增词的突增量与突增率（规则文档 §8）。

    公式：突增量 = 本周期出现次数 - 上一周期出现次数
          突增率 = 突增量 / 上一周期出现次数（上期为 0 时记为「新增」= inf）
    入参：cur_value 本期 emotion_value_Nd；prev_value 上期同字段（N 天前那一行）
    出参：(突增量, 突增率)
    """
    cur = np.asarray(cur_value, dtype="float64")
    prev = np.asarray(prev_value, dtype="float64")
    delta = cur - prev
    rate = np.where(prev > 0, np.divide(delta, prev, out=np.zeros_like(delta),
                                        where=prev > 0), np.inf)
    return delta, rate


# ══════════════════════════════════════════════════════════════════════════
# 第 9 章  下钻明细表的评论原文掩码（需求 5.2 的展示口径）
# ══════════════════════════════════════════════════════════════════════════
#
# 规则（业务方定义）：`content_snippet` 里**除命中的关键词之外，其余内容一律隐藏**。
#
#     原文      我是体力一般，来回两个半小时左右
#     关键词    体力一般 / 来回两个半小时
#     掩码后    **体力一般**来回两个半小时**
#
# 注意被隐藏的每一段都换成**固定的一个掩码串**（默认 `**`），而不是按字数出星号：
#   「我是」→ `**`    「，」→ `**`    「左右」→ `**`
# 所以掩码后的串看不出原文有多长 —— 这是刻意的，按字数出星号等于泄露了文本长度，
# 拼上关键词位置能还原出不少信息。想看长度就把 mode 调成 "char"。
#
# ⚠ 关键词对不上原文的情况真实存在。实测八大处 2 万条评论：
#   98.1% 的关键词能在原文里原样找到，1.9% 找不到 —— 大模型把词抽象过了
#   （原文「每次感受都不错」，关键词「感受不错」；原文「景色很漂亮，人也很多」，
#   关键词「人很多」）。这类评论一个词都对不上，掩码后就是光秃秃一个 `**`。
#   默认就这么存（隐藏优先）；想让这类评论保留原文，把 on_no_match 设成 "keep_raw"。

DRILL_MASK_TOKEN = "**"              # 每一段被隐藏的内容换成什么

DRILL_MASK_MODE_RUN = "run"          # 连续的一段隐藏内容 → 一个掩码串（默认，示例即此）
DRILL_MASK_MODE_CHAR = "char"        # 每个隐藏字符 → 一个掩码字符（会泄露原文长度）

DRILL_MASK_SCOPE_COMMENT = "comment"  # 保留**这条评论的全部关键词**（默认）
DRILL_MASK_SCOPE_WORD = "word"        # 只保留当前行那一个 emotion_word

DRILL_NO_MATCH_MASK_ALL = "mask_all"  # 一个词都没对上 → 整条隐藏（默认）
DRILL_NO_MATCH_KEEP_RAW = "keep_raw"  # 一个词都没对上 → 原样保留，别存一条没信息量的 `**`


def mask_content(text: str, words: Sequence[str], *,
                 token: str = DRILL_MASK_TOKEN,
                 mode: str = DRILL_MASK_MODE_RUN,
                 ignore_case: bool = True,
                 on_no_match: str = DRILL_NO_MATCH_MASK_ALL) -> tuple:
    """把评论原文里**关键词以外**的内容隐藏掉。

    公式
    ----
      原文切成「命中区间」与「非命中区间」交替的序列：
        · 命中区间     原样输出
        · 非命中区间   mode="run"  → 整段换成一个 token
                       mode="char" → 每个字符换成 token 的首字符
      同一个词出现多次，**每一次**都保留；区间重叠（词互相包含/交叉）先合并再切。

    入参
    ----
      text         评论原文（已截断到 drill_content_limit 的那一段）
      words        要保留的关键词列表
      token        掩码串，默认 "**"
      mode         "run"（默认）/ "char"
      ignore_case  匹配时忽略大小写（英文/拼音关键词常见），输出仍用原文的大小写
      on_no_match  一个词都没对上时：mask_all（默认，整条隐藏）/ keep_raw（原样保留）

    出参
    ----
      (掩码后的字符串, 命中的区间数)
      命中区间数 = 0 表示这条评论的关键词一个都没在原文里找到，
      调用方拿它来统计「有多少条被整条隐藏」，写进跑批报告。
    """
    text = "" if text is None else str(text)
    if not text:
        return "", 0

    hay = text.lower() if ignore_case else text
    spans = []
    for w in words or []:
        w = "" if w is None else str(w).strip()
        if not w:
            continue
        needle = w.lower() if ignore_case else w
        start = hay.find(needle)
        while start >= 0:                      # 同一个词出现多次，每次都保留
            spans.append((start, start + len(needle)))
            start = hay.find(needle, start + 1)

    if not spans:
        if on_no_match == DRILL_NO_MATCH_KEEP_RAW:
            return text, 0
        return token if mode == DRILL_MASK_MODE_RUN else (token[:1] or "*") * len(text), 0

    # 区间合并：关键词可能互相包含（「体力」「体力一般」）或交叉，不合并会切出空片段
    spans.sort()
    merged = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    fill = (token[:1] or "*")
    out, pos = [], 0
    for s, e in merged:
        if s > pos:                            # 命中区间之前的那段要隐藏
            out.append(token if mode == DRILL_MASK_MODE_RUN else fill * (s - pos))
        out.append(text[s:e])                  # 关键词原样输出
        pos = e
    if pos < len(text):                        # 最后一个关键词之后还有尾巴
        out.append(token if mode == DRILL_MASK_MODE_RUN else fill * (len(text) - pos))
    return "".join(out), len(merged)
