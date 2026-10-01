# -*- coding: utf-8 -*-
"""源数据标准化：把 src 两张表打平成三份「事实明细」。

产出（全部是 DataFrame，直接进内存，不落任何中间表）：

  comment_facts   —— 评论粒度。一条评论一行，主评论/子评论**均按独立个体计数**
                     （规则文档 §2「主子评论均作为独立个体计数」）
  dimension_facts —— 维度粒度。dimension_tags 里每个元素拆一行
                     （规则文档 §4「多维度评论各维度独立计数」）
  keyword_facts   —— 关键词粒度。keyword_tags 里每个词拆一行

三份明细的情感来源都是 `sentiment_score`：
  > 1 = 正向（好评）、0 = 中性（中评）、-1 = 负向（差评）
维度明细额外带自己的 `dim_sentiment`（dimension_tags 元素里的 sentiment），
因为「整条评论是好评」不代表「其中提到的排队时长也是好评」。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from .metric_calc_domain import (
    CHANNEL_NAME, L1_DIMENSIONS, NEGATIVE, NEUTRAL, POSITIVE,
    SENTIMENT_TYPE, judge_sentiment, normalize_region,
)

log = logging.getLogger(__name__)


# ============================================================
# JSON 字段解析
# ============================================================
def parse_json_array(raw: Any) -> List[Any]:
    """把 text 字段解析成列表。

    真实采集数据里这几个字段的脏法很多：None、空串、'[]'、单引号、
    双重转义、甚至直接是一个逗号分隔的裸串。这里全部兜住并返回 []，
    不能让一条脏数据把整天的跑批打断。
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, float) and np.isnan(raw):
        return []
    s = str(raw).strip()
    if not s or s in ("[]", "{}", "null", "None", "NULL"):
        return []
    try:
        v = json.loads(s)
    except Exception:
        try:
            v = json.loads(s.replace("'", '"'))
        except Exception:
            # 最后兜底：当成逗号分隔的裸词串
            parts = [p.strip().strip('"[]\'') for p in s.split(",")]
            return [p for p in parts if p]
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v]
    return [v] if v else []


# 情感判定口径不在这里 —— 它是指标口径的一部分，落在 metric_calc_domain.judge_sentiment。
# 这里保留一个同名转发，只为让本模块的读者知道去哪儿看规则。
normalize_sentiment = judge_sentiment


# ============================================================
# 评论明细
# ============================================================
def build_comment_facts(comments: pd.DataFrame) -> pd.DataFrame:
    """src_opinion_social_work_comment_di → 评论粒度事实。

    返回列：
      scenic_spot_code / scenic_spot_name / travel_date / channel /
      platform_code / platform_name / comment_id / work_id / comment_uk /
      sentiment / is_positive / is_neutral / is_negative / publish_dt /
      region（location 归一后的地域，见 domain §1.11）
    """
    if comments.empty:
        return _empty_comment_facts()

    df = comments.copy()
    df["publish_dt"] = pd.to_datetime(df["publish_time"], errors="coerce")
    # publish_time 为空的评论没有归属日期，无法进任何日粒度指标。
    # 需求六.2 明确「按 publish_time 拉取」，这里显式丢弃并计数告警。
    bad = int(df["publish_dt"].isna().sum())
    if bad:
        log.warning("publish_time 为空/非法，丢弃 %d 条评论（占 %.2f%%）",
                    bad, 100.0 * bad / len(df))
        df = df[df["publish_dt"].notna()]
    if df.empty:
        return _empty_comment_facts()

    df["travel_date"] = df["publish_dt"].dt.strftime("%Y%m%d")
    df["scenic_spot_code"] = df["scenic_id"].astype(str).str.strip()
    df["scenic_spot_name"] = df["scenic_name"].astype(str).str.strip()
    df["channel"] = df["channel"].astype(str).str.strip().str.lower()
    df["platform_code"] = df["channel"]
    df["platform_name"] = df["channel"].map(CHANNEL_NAME).fillna(df["channel"])

    score = df["sentiment_score"] if "sentiment_score" in df.columns else pd.Series(
        [None] * len(df), index=df.index)
    label = df["sentiment_label"] if "sentiment_label" in df.columns else pd.Series(
        [None] * len(df), index=df.index)
    df["sentiment"] = [judge_sentiment(s, l) for s, l in zip(score, label)]

    df["is_positive"] = (df["sentiment"] == POSITIVE).astype("int64")
    df["is_neutral"] = (df["sentiment"] == NEUTRAL).astype("int64")
    df["is_negative"] = (df["sentiment"] == NEGATIVE).astype("int64")
    # 地域：热力地图与下钻表都用它。源表没有 location 列（老 CSV）时全部为空
    df["region"] = (df["location"].map(normalize_region) if "location" in df.columns
                    else "")

    keep = ["scenic_spot_code", "scenic_spot_name", "travel_date", "channel",
            "platform_code", "platform_name", "sentiment",
            "is_positive", "is_neutral", "is_negative", "publish_dt", "region"]
    for c in ("comment_id", "work_id", "comment_uk", "comment_level",
              "root_comment_id", "likes", "content", "commenter_name",
              "dimension_tags", "keyword_tags", "entity_tags"):
        if c in df.columns:
            keep.append(c)
    return df[keep].reset_index(drop=True)


def _empty_comment_facts() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "scenic_spot_code", "scenic_spot_name", "travel_date", "channel",
        "platform_code", "platform_name", "sentiment",
        "is_positive", "is_neutral", "is_negative", "publish_dt", "region"])


# ============================================================
# 维度明细
# ============================================================
def build_dimension_facts(comment_facts: pd.DataFrame,
                          unknown_policy: str = "keep") -> pd.DataFrame:
    """dimension_tags 炸开成维度粒度事实。

    一条评论若命中 3 个维度，就产生 3 行 —— 规则文档 §4/§5 的
    「多维度评论各维度独立计数 / 拆解为独立个体」就是这个意思。

    每行的情感取**该维度自己的 sentiment**，拿不到才退回整条评论的情感。
    """
    cols = ["scenic_spot_code", "scenic_spot_name", "travel_date", "platform_code",
            "dimension_level1", "dimension_level2", "dimension_level3",
            "dim_sentiment", "is_positive", "is_neutral", "is_negative"]
    if comment_facts.empty or "dimension_tags" not in comment_facts.columns:
        return pd.DataFrame(columns=cols)

    rows: List[Dict[str, Any]] = []
    whitelist = set(L1_DIMENSIONS)
    dropped = 0
    for r in comment_facts.itertuples(index=False):
        tags = parse_json_array(getattr(r, "dimension_tags", None))
        if not tags:
            continue
        for t in tags:
            if not isinstance(t, dict):
                continue
            l1 = str(t.get("dim1") or "").strip()
            if not l1:
                continue
            if l1 not in whitelist:
                if unknown_policy == "drop":
                    dropped += 1
                    continue
            s = judge_sentiment(t.get("sentiment"), t.get("sentiment_label"))
            rows.append({
                "scenic_spot_code": r.scenic_spot_code,
                "scenic_spot_name": r.scenic_spot_name,
                "travel_date": r.travel_date,
                "platform_code": getattr(r, "platform_code", ""),
                "dimension_level1": l1,
                "dimension_level2": str(t.get("dim2") or "").strip(),
                "dimension_level3": str(t.get("dim3") or "").strip(),
                "dim_sentiment": s,
                "is_positive": 1 if s == POSITIVE else 0,
                "is_neutral": 1 if s == NEUTRAL else 0,
                "is_negative": 1 if s == NEGATIVE else 0,
            })
    if dropped:
        log.warning("丢弃非白名单一级维度 %d 条（unknown_dimension_policy=drop）", dropped)
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows, columns=cols)


# ============================================================
# 关键词明细
# ============================================================
def build_keyword_facts(comment_facts: pd.DataFrame) -> pd.DataFrame:
    """keyword_tags 炸开成关键词粒度事实。

    词的情感类型取**整条评论的 sentiment**（sentiment_score），不是词本身的 ——
    源表的 keyword_tags 只是一个词数组，没有逐词情感。
    「太差」出现在一条负向评论里，它就是一个负面词；
    同一个词若同时出现在正/负评论中，会分别计入 positive / negative 两组，
    最终由 content builder 按 emotion_word 去重时保留计数更大的那一组。
    """
    cols = ["scenic_spot_code", "scenic_spot_name", "travel_date", "platform_code",
            "sentiment", "emotion_type", "emotion_word"]
    if comment_facts.empty or "keyword_tags" not in comment_facts.columns:
        return pd.DataFrame(columns=cols)

    rows: List[Dict[str, Any]] = []
    for r in comment_facts.itertuples(index=False):
        words = parse_json_array(getattr(r, "keyword_tags", None))
        if not words:
            continue
        etype = SENTIMENT_TYPE.get(int(r.sentiment), "neutral")
        seen = set()
        for w in words:
            if isinstance(w, dict):          # 兼容 [{"word": "x"}] 这种写法
                w = w.get("word") or w.get("value") or w.get("keyword")
            word = str(w or "").strip()
            if not word or word in seen:
                continue                     # 同一条评论里同词只算一次
            seen.add(word)
            rows.append({
                "scenic_spot_code": r.scenic_spot_code,
                "scenic_spot_name": r.scenic_spot_name,
                "travel_date": r.travel_date,
                "platform_code": getattr(r, "platform_code", ""),
                "sentiment": int(r.sentiment),
                "emotion_type": etype,
                "emotion_word": word,
            })
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows, columns=cols)
