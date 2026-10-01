# -*- coding: utf-8 -*-
"""需求 5.2：下钻分析表 —— 词 → 评论 → 作品。

看板上点内容表里的某个 `emotion_word`，要能列出命中该词的评论，
再点评论跳到它所属的作品/帖子/笔记原文。内容表只有聚合值，落不下明细，
所以单独出一张 `ads_trf_social_opinion_drill_analysis_di`（建表语句见
`sql/ads_drill_analysis_ddl.sql`）。

一行 = 一个词 × 一条评论。同一条评论命中 3 个词就有 3 行。
`work_url` 直接来自 `src_opinion_social_work_di`，前端拿到就能跳转。

需求六.1 提到「5.2 历史已经计算了可以直接搜索历史的应该就行」——
所以这张表按 `travel_date` 幂等重写当期分区即可，不需要回补逻辑。

`content_snippet` 掩码
---------------------
默认**只保留命中的关键词，其余内容一律隐藏**（口径与实现见 domain 第 9 章）：

    原文    我是体力一般，来回两个半小时左右
    掩码后  **体力一般**来回两个半小时**

默认按「这条评论的全部关键词」掩码（scope="comment"），所以同一条评论
在它的每一行里长得一样 —— 点哪个词进来看到的都是同一段文本，不会忽长忽短。
想让每行只高亮自己那个词，把 drill_mask_scope 设成 "word"。
关掉掩码存原文：drill_mask_content = False。
"""

from __future__ import annotations

import hashlib
from typing import List

import pandas as pd

from .context import RunContext
from .metric_calc_domain import (DRILL_MASK_SCOPE_WORD, SENTIMENT_TYPE,
                                 mask_content)
from .normalize import parse_json_array

# ⚠ 这张表的景区字段跟另外四张 ADS 表**不一样**：
#   下钻表    scenic_id / scenic_name       （与源表 src_opinion_social_work_di 同名）
#   其余四张  scenic_spot_code / scenic_spot_name
# 内部管线（comment_facts 等）统一用 scenic_spot_code，只在这张表的**输出边界**改名，
# 免得为了一张表把整条链路的列名都动一遍。
# tests/test_schema.py 会拿这份清单跟 sql/ads_drill_analysis_ddl.sql 逐列对拍。
COLUMNS: List[str] = [
    "scenic_id", "scenic_name", "emotion_word", "emotion_type", "word_source",
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


def build_drill_analysis(comment_facts: pd.DataFrame, works: pd.DataFrame,
                         ctx: RunContext) -> pd.DataFrame:
    """评论明细 + 作品表 → 下钻分析表（词 → 评论 → 作品）。"""
    if comment_facts.empty:
        return pd.DataFrame(columns=COLUMNS)

    out_dates = set(ctx.output_dates)
    df = comment_facts[comment_facts["travel_date"].isin(out_dates)]
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)

    limit = int(ctx.settings.drill_content_limit)
    mask_cfg = _mask_config(ctx)
    by_word = (DRILL_MASK_SCOPE_WORD
               if mask_cfg is not None
               and getattr(ctx.settings, "drill_mask_scope", "") == DRILL_MASK_SCOPE_WORD
               else None)
    n_masked = n_no_match = 0
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
        # 先截断再掩码：掩码后的串长度跟原文无关，反过来做会让 limit 失去意义。
        # 被截断切掉一半的关键词自然就匹配不上，跟着一起进掩码段，不会露出半个词。
        if mask_cfg is not None and by_word is None:
            snippet, hit = mask_content(snippet, words, **mask_cfg)
            n_masked += 1
            if hit == 0:
                n_no_match += 1
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
            row_snippet = snippet
            if by_word is not None:
                # scope="word"：每行只保留自己那一个词，所以要逐行掩码
                row_snippet, hit = mask_content(content[:limit], [word], **mask_cfg)
                n_masked += 1
                if hit == 0:
                    n_no_match += 1
            rows.append({
                "scenic_id": r.scenic_spot_code,
                "scenic_name": r.scenic_spot_name,
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
                "content_snippet": row_snippet,
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
        # 源表 src_opinion_social_work_di 本来就叫 scenic_id，改名之后这里少一次 rename
        w = works.rename(columns={"channel": "platform_code",
                                  "title": "work_title"})[
            ["scenic_id", "platform_code", "work_id", "work_url",
             "work_title", "author_name"]].copy()
        for c in ("scenic_id", "platform_code", "work_id"):
            w[c] = w[c].astype(str)
        w = w.drop_duplicates(subset=["scenic_id", "platform_code", "work_id"])
        out = out.merge(w, on=["scenic_id", "platform_code", "work_id"], how="left")
    for c in ("work_url", "work_title", "author_name"):
        if c not in out.columns:
            out[c] = ""
        out[c] = out[c].fillna("")
    out["work_title"] = out["work_title"].astype(str).str.slice(0, 255)

    out["etl_time"] = ctx.etl_time
    # 同一 (词, 评论) 只保留一行 —— 一条评论的 keyword_tags 里若有重复词，
    # 上面已按 seen 去重，这里再兜一层，防止源表 comment_uk 重复导致唯一键冲突。
    out = out.drop_duplicates(subset=["detail_uk"], keep="first")

    # 掩码是「看不见的手」：不报出来，没人知道有多少条被整条隐藏了
    if mask_cfg is not None and n_masked:
        ctx.warn(f"下钻明细 content_snippet 已掩码 {n_masked:,} 条"
                 + (f"，其中 {n_no_match:,} 条的关键词在原文里一个都没找到"
                    f"（{n_no_match / n_masked:.1%}，大模型把词抽象过了），"
                    f"按 drill_mask_on_no_match="
                    f"{getattr(ctx.settings, 'drill_mask_on_no_match', 'mask_all')} 处理"
                    if n_no_match else "，关键词全部命中"))
    return out[COLUMNS].sort_values(
        ["scenic_id", "travel_date", "emotion_word"]).reset_index(drop=True)


def _mask_config(ctx: RunContext):
    """把 settings 里的掩码配置翻译成 mask_content 的入参。关掉时返回 None。"""
    st = ctx.settings
    if not getattr(st, "drill_mask_content", False):
        return None
    from . import metric_calc_domain as D
    return {
        "token": getattr(st, "drill_mask_token", D.DRILL_MASK_TOKEN),
        "mode": getattr(st, "drill_mask_mode", D.DRILL_MASK_MODE_RUN),
        "ignore_case": bool(getattr(st, "drill_mask_ignore_case", True)),
        "on_no_match": getattr(st, "drill_mask_on_no_match", D.DRILL_NO_MATCH_MASK_ALL),
    }
