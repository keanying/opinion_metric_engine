# -*- coding: utf-8 -*-
"""需求 5.2：下钻分析表 —— 词 → 评论 → 作品。

看板上点内容表里的某个 `emotion_word`，要能列出命中该词的评论，
再点评论跳到它所属的作品/帖子/笔记原文。内容表只有聚合值，落不下明细，
所以单独出一张 `ads_trf_social_opinion_drill_analysis_di`（建表语句见
`sql/ads_drill_analysis_ddl.sql`）。

一行 = 一个词 × 一条评论。同一条评论命中 3 个词就有 3 行。
`work_url` 直接来自 `src_opinion_social_work_di`，前端拿到就能跳转。

缺数回补（domain §1.8，业务确认「包含」）
--------------------------------------
与指标表同一套「先补明细」：某渠道在输出日期当天一条评论都没有 → 复制往前最近一个
有评论那天（当日档，最多找 5 天）的全部评论明细，作为这一天的下钻明细。
复制出来的行：travel_date = 这一天；publish_time = 这一天 00:00:00（客户要求补进来的明细
publish_time 用跑数那天）。不保留原评论的时分秒：上午跑批时 23:25 这种时刻还在「未来」，
下游按「当天到现在」查会查不到；固定 00:00:00 也保证重跑得到同一个值（publish_time 在推送 pkId 里）。
detail_uk 带上这一天，不和原评论撞键。看板点内容表里补出来的词，也能列出对应的评论。

与大盘表对账（客户要求）：某天某渠道，下钻表按 comment_id 去重的评论数
== 大盘表当日（td）该渠道的 comment_total；各渠道加起来 == all 行。
所以**没有关键词的评论也出一行**（emotion_word 为空、word_source = none），
否则这些评论在下钻表里数不到。看板点词查询按词过滤，碰不到这些行。

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
import json
from typing import List

import pandas as pd

from .context import RunContext
from .metric_calc_domain import (DRILL_MASK_SCOPE_WORD, REGION_UNKNOWN, SENTIMENT_TYPE,
                                 daily_channel_presence, mask_content)
from .normalize import parse_json_array
from .windows import fill_source_dates

# ⚠ 这张表的景区 / 渠道字段跟另外四张 ADS 表**不一样**：
#   下钻表    scenic_id / scenic_name、channel / channel_name   （与源表同名，客户 2026-10 改表）
#   其余四张  scenic_spot_code / scenic_spot_name、platform_code / platform_name
# 内部管线（comment_facts 等）统一用 scenic_spot_code，只在这张表的**输出边界**改名，
# 免得为了一张表把整条链路的列名都动一遍。
# tests/test_schema.py 会拿这份清单跟 sql/ads_drill_analysis_ddl.sql 逐列对拍。
COLUMNS: List[str] = [
    "scenic_id", "scenic_name", "emotion_word", "emotion_type", "word_source",
    "channel", "channel_name",
    "work_id", "work_url", "work_title", "author_name",
    "comment_id", "root_comment_id", "comment_level", "commenter_name",
    "content_snippet", "likes", "sentiment_score",
    "dimension_level1", "dimension_level2", "dimension_level3",
    # 需求 2.0 新增：评论地域（location 归一，domain §1.11）与实体标签（原样 JSON 数组）
    "region", "entity_tags",
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
    df = comment_facts[comment_facts["travel_date"].isin(out_dates)].assign(fill_copy=False)
    copies = _filled_copies(comment_facts, ctx)
    if not copies.empty:
        df = pd.concat([df, copies], ignore_index=True)
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
            if words:                    # 没有关键词的评论不算进「词没找到」的统计
                n_masked += 1
                if hit == 0:
                    n_no_match += 1
        comment_id = str(getattr(r, "comment_id", "") or "")
        work_id = str(getattr(r, "work_id", "") or "")
        # 实体标签原样落 JSON 数组（解析一遍再序列化，单引号等脏写法顺手修掉）；没有就是 []
        entities = json.dumps(parse_json_array(getattr(r, "entity_tags", None)),
                              ensure_ascii=False, separators=(",", ":"))
        # 与大盘热力图同口径：没地域记「未知」
        region = str(getattr(r, "region", "") or "") or REGION_UNKNOWN

        clean = []
        for w in words:
            if isinstance(w, dict):
                w = w.get("word") or w.get("value") or w.get("keyword")
            word = str(w or "").strip()
            if word and word not in clean:
                clean.append(word)
        # 没有关键词的评论也出一行（空词），下钻表的评论数才能和大盘表 comment_total 对上
        for word in clean or [""]:
            row_snippet = snippet
            if by_word is not None:
                # scope="word"：每行只保留自己那一个词，所以要逐行掩码
                row_snippet, hit = mask_content(content[:limit], [word] if word else [],
                                                **mask_cfg)
                if word:
                    n_masked += 1
                    if hit == 0:
                        n_no_match += 1
            rows.append({
                "scenic_id": r.scenic_spot_code,
                "scenic_name": r.scenic_spot_name,
                "emotion_word": word,
                "emotion_type": etype,
                "word_source": "keyword" if word else "none",
                "channel": r.platform_code,
                "channel_name": r.platform_name,
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
                "region": region,
                "entity_tags": entities,
                "publish_time": r.publish_dt,
                "travel_date": int(r.travel_date),
                # 复制来的明细带上这一天，否则与原评论那一行撞 detail_uk
                "detail_uk": (_uk(r.scenic_spot_code, word, r.platform_code, work_id,
                                  comment_id, "fill", r.travel_date) if r.fill_copy
                              else _uk(r.scenic_spot_code, word, r.platform_code,
                                       work_id, comment_id)),
            })

    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    out = pd.DataFrame(rows)

    # 作品信息：拿 work_url 才能跳转，拿不到就留空，不要因为缺作品行丢掉评论
    if works is not None and not works.empty:
        # 源表 src_opinion_social_work_di 本来就叫 scenic_id，改名之后这里少一次 rename
        # 作品表的渠道列本来就叫 channel，跟这张表同名，直接关联
        w = works.rename(columns={"title": "work_title"})[
            ["scenic_id", "channel", "work_id", "work_url",
             "work_title", "author_name"]].copy()
        for c in ("scenic_id", "channel", "work_id"):
            w[c] = w[c].astype(str)
        w = w.drop_duplicates(subset=["scenic_id", "channel", "work_id"])
        out = out.merge(w, on=["scenic_id", "channel", "work_id"], how="left")
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


def _filled_copies(comment_facts: pd.DataFrame, ctx: RunContext) -> pd.DataFrame:
    """输出日期上「渠道当天没评论」的那几天，复制往前最近一天的评论明细（domain §1.8）。

    上限用当日档（1 日档）。复制行 travel_date = 这一天、publish_dt = 这一天 00:00:00，
    带 fill_copy=True。
    """
    lb = ctx.backfill_lookback([1]).get(1, 0)
    if lb <= 0 or comment_facts.empty:
        return pd.DataFrame()
    by = ["scenic_spot_code", "platform_code"]
    m = fill_source_dates(daily_channel_presence(comment_facts), by, "travel_date",
                          ctx.output_dates, lb)
    m = m[m["found"]]
    if m.empty:
        return pd.DataFrame()
    m = m.rename(columns={"travel_date": "_target"})[by + ["_target", "src_date"]]
    c = comment_facts.merge(m, left_on=by + ["travel_date"],
                            right_on=by + ["src_date"], how="inner")
    # publish_time = 补到的那一天 00:00:00（见模块说明：不能是「未来」时刻，重跑要得到同一个值）
    c["publish_dt"] = pd.to_datetime(c["_target"], format="%Y%m%d")
    c["travel_date"] = c["_target"]
    return c.drop(columns=["_target", "src_date"]).assign(fill_copy=True)


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
