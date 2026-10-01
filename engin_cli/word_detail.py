# -*- coding: utf-8 -*-
"""需求 5.2：词 → 评论 → 作品 的下钻明细表。

看板上点内容表里的某个 `emotion_word`，要能列出命中该词的评论，
再点评论跳到它所属的作品/帖子/笔记原文。内容表只有聚合值，落不下明细，
所以单独出一张 `ads_trf_social_opinion_drill_analysis_di`（建表语句见
`sql/ads_word_detail_ddl.sql`）。

一行 = 一个词 × 一条评论。同一条评论命中 3 个词就有 3 行。
`work_url` 直接来自 `src_opinion_social_work_di`，前端拿到就能跳转。

需求六.1 提到「5.2 历史已经计算了可以直接搜索历史的应该就行」——
所以这张表按 `travel_date` 幂等重写当期分区即可，不需要回补逻辑。
"""

from __future__ import annotations

import hashlib
from typing import List

import pandas as pd

from .metric_calc_domain import SENTIMENT_TYPE
from .context import RunContext
from .normalize import parse_json_array

COLUMNS: List[str] = [
    "scenic_spot_code", "scenic_spot_name", "emotion_word", "emotion_type", "word_source",
    "platform_code", "platform_name",
    "work_id", "work_url", "work_title", "author_name",
    "comment_id", "root_comment_id", "comment_level", "commenter_name",
    "content_snippet", "likes", "sentiment_score",
    "dimension_level1", "dimension_level2", "dimension_level3",
    "publish_time", "travel_date", "etl_time", "detail_uk",
]


def _uk(*parts) -> str:
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def build_word_detail(comment_facts: pd.DataFrame, works: pd.DataFrame,
                      ctx: RunContext) -> pd.DataFrame:
    """评论明细 + 作品表 → 词-评论-作品下钻明细。"""
    if comment_facts.empty:
        return pd.DataFrame(columns=COLUMNS)

    out_dates = set(ctx.output_dates)
    df = comment_facts[comment_facts["travel_date"].isin(out_dates)]
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)

    limit = int(ctx.settings.word_detail_content_limit)
    rows = []
    for r in df.itertuples(index=False):
        words = parse_json_array(getattr(r, "keyword_tags", None))
        if not words:
            continue
        etype = SENTIMENT_TYPE.get(int(r.sentiment), "neutral")
        # 维度只取第一条，作为「这条评论主要在说哪个维度」的定位信息；
        # 完整维度分布在维度表里，明细表不重复承载。
        dims = [t for t in parse_json_array(getattr(r, "dimension_tags", None))
                if isinstance(t, dict)]
        d1 = str(dims[0].get("dim1") or "") if dims else ""
        d2 = str(dims[0].get("dim2") or "") if dims else ""
        d3 = str(dims[0].get("dim3") or "") if dims else ""

        content = str(getattr(r, "content", "") or "").replace("\r", " ").replace("\n", " ")
        snippet = content[:limit]
        comment_id = str(getattr(r, "comment_id", "") or "")
        work_id = str(getattr(r, "work_id", "") or "")

        seen = set()
        for w in words:
            if isinstance(w, dict):
                w = w.get("word") or w.get("value") or w.get("keyword")
            word = str(w or "").strip()
            if not word or word in seen:
                continue
            seen.add(word)
            rows.append({
                "scenic_spot_code": r.scenic_spot_code,
                "scenic_spot_name": r.scenic_spot_name,
                "emotion_word": word,
                "emotion_type": etype,
                "word_source": "keyword",
                "platform_code": r.platform_code,
                "platform_name": r.platform_name,
                "work_id": work_id,
                "comment_id": comment_id,
                "root_comment_id": str(getattr(r, "root_comment_id", "") or ""),
                "comment_level": str(getattr(r, "comment_level", "") or ""),
                "commenter_name": str(getattr(r, "commenter_name", "") or ""),
                "content_snippet": snippet,
                "likes": int(getattr(r, "likes", 0) or 0),
                "sentiment_score": int(r.sentiment),
                "dimension_level1": d1,
                "dimension_level2": d2,
                "dimension_level3": d3,
                "publish_time": r.publish_dt,
                "travel_date": int(r.travel_date),
                "detail_uk": _uk(r.scenic_spot_code, word, r.platform_code,
                                 work_id, comment_id),
            })

    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    out = pd.DataFrame(rows)

    # 作品信息：拿 work_url 才能跳转，拿不到就留空，不要因为缺作品行丢掉评论
    if works is not None and not works.empty:
        w = works.rename(columns={"scenic_id": "scenic_spot_code",
                                  "channel": "platform_code",
                                  "title": "work_title"})[
            ["scenic_spot_code", "platform_code", "work_id", "work_url",
             "work_title", "author_name"]].copy()
        for c in ("scenic_spot_code", "platform_code", "work_id"):
            w[c] = w[c].astype(str)
        w = w.drop_duplicates(subset=["scenic_spot_code", "platform_code", "work_id"])
        out = out.merge(w, on=["scenic_spot_code", "platform_code", "work_id"], how="left")
    for c in ("work_url", "work_title", "author_name"):
        if c not in out.columns:
            out[c] = ""
        out[c] = out[c].fillna("")
    out["work_title"] = out["work_title"].astype(str).str.slice(0, 255)

    out["etl_time"] = ctx.etl_time
    # 同一 (词, 评论) 只保留一行 —— 一条评论的 keyword_tags 里若有重复词，
    # 上面已按 seen 去重，这里再兜一层，防止源表 comment_uk 重复导致唯一键冲突。
    out = out.drop_duplicates(subset=["detail_uk"], keep="first")
    return out[COLUMNS].sort_values(
        ["scenic_spot_code", "travel_date", "emotion_word"]).reset_index(drop=True)
