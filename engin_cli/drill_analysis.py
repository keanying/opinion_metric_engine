# -*- coding: utf-8 -*-
"""需求 5.2：下钻分析表 —— 词 → 评论 → 作品。

看板上点内容表里的某个 `emotion_word`，要能列出命中该词的评论，
再点评论跳到它所属的作品/帖子/笔记原文。内容表只有聚合值，落不下明细，
所以单独出一张 `ads_trf_social_opinion_drill_analysis_di`（建表语句见
`sql/ads_drill_analysis_ddl.sql`）。

一行 = 某一天 × 一条评论 × 一个词。同一条评论命中 3 个词就有 3 行。
`work_url` 直接来自 `src_opinion_social_work_di`，前端拿到就能跳转。

按天存，周期按 travel_date 区间查（客户 2026-10）
------------------------------------------------
一行 = 某一天（travel_date）的一条评论 × 一个词。某渠道这一天没评论 → 复制往前最近一个
有评论那天的全部评论（最多往前找 backfill_lookback 天，默认 5 天），复制行 publish_time = 这一天
00:00:00（真实评论保留原发布时间）。time_granularity 固定写当日周期名（td）。

补数上限所有周期统一（默认）时，每天只有一份补齐结果，大盘表任意周期 == 区间内每天之和，所以
大盘表 (travel_date = T, time_granularity = g) 这一格，对应下钻表
travel_date BETWEEN g 的本期首日 AND 本期末日（macro_period_ranges）：

    COUNT(DISTINCT channel, work_id, comment_id, publish_time) == comment_total
    按 region 计数 == period_comment_heatmap 的 heat
    wordcloud_map 里的词，按 emotion_word 都查得到

去重要带上 publish_time：同一条评论被复制到区间里好几天时，大盘表每天各算一次，
下钻表也是每天一行（publish_time 不同）。
没有关键词的评论也出一行（emotion_word 为空、word_source = none），否则数不到这些评论。

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
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from . import metric_calc_domain as D
from .context import RunContext
from .metric_calc_domain import (DRILL_MASK_SCOPE_WORD, REGION_UNKNOWN, SENTIMENT_TYPE,
                                 daily_channel_presence, mask_content)
from .normalize import parse_json_array

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
    "publish_time", "travel_date",
    # 固定写当日周期名（td）：按天存，周期用 travel_date 区间查
    "time_granularity",
    "etl_time", "detail_uk",
]
_PKEY = ["scenic_spot_code", "platform_code"]


def _uk(*parts) -> str:
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def build_drill_analysis(comment_facts: pd.DataFrame, works: pd.DataFrame,
                         ctx: RunContext,
                         keywords: Optional[Dict[str, List[str]]] = None) -> pd.DataFrame:
    """评论明细 + 作品表 → 下钻分析表（词 → 评论 → 作品）。

    keywords：{景区: [采集关键字]}，None = 不过滤（原逻辑）。
      给了就只收**评论原文包含本景区任一采集关键字**的评论（不分大小写），其余评论不进下钻表；
      本景区一个关键字都没有 → 这个景区下钻表 0 行。
      过滤放在补齐之后：渠道当天有没有评论、要不要往前补，仍按全部评论判断，
      补过来的评论同样要包含关键字才收。
    """
    if comment_facts.empty:
        return pd.DataFrame(columns=COLUMNS)

    plan = _window_plan(comment_facts, ctx)
    if plan.empty:
        return pd.DataFrame(columns=COLUMNS)
    # 只给用得到的 (景区, 渠道, 来源日) 拼行，每条评论拼一次，再展开到用到它的日子
    need = plan[_PKEY + ["src_date"]].drop_duplicates()
    df = comment_facts.merge(need, left_on=_PKEY + ["travel_date"],
                             right_on=_PKEY + ["src_date"], how="inner")
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)

    limit = int(ctx.settings.drill_content_limit)
    mask_cfg = _mask_config(ctx)
    by_word = (DRILL_MASK_SCOPE_WORD
               if mask_cfg is not None
               and getattr(ctx.settings, "drill_mask_scope", "") == DRILL_MASK_SCOPE_WORD
               else None)
    n_masked = n_no_match = 0
    kw_lower = None if keywords is None else {
        str(sc): [k.lower() for k in ks if k] for sc, ks in keywords.items()}
    n_kw_in = n_kw_out = 0
    rows = []
    for r in df.itertuples(index=False):
        if kw_lower is not None:
            full = str(getattr(r, "content", "") or "").lower()
            if not any(k in full for k in kw_lower.get(str(r.scenic_spot_code), ())):
                n_kw_out += 1
                continue
            n_kw_in += 1
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
                "src_date": r.src_date,
            })

    if kw_lower is not None:
        empty = [str(sc) for sc in df["scenic_spot_code"].astype(str).unique()
                 if not kw_lower.get(sc)]
        ctx.warn(f"下钻表采集关键字过滤：收 {n_kw_in:,} 条评论，"
                 f"原文不含采集关键字的 {n_kw_out:,} 条不写入"
                 + (f"；⚠ 景区 {'、'.join(empty)} 没有采集关键字，下钻表 0 行" if empty else ""))
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
    # 作品信息拼好之后再展开到用到它的日子（复制来的评论会出现在好几天）
    out = _expand(out, plan)
    # 同一 (词, 评论) 只保留一行 —— 一条评论的 keyword_tags 里若有重复词，
    # 上面已按 seen 去重，这里再兜一层，防止源表 comment_uk 重复导致唯一键冲突。
    out = out.drop_duplicates(subset=["detail_uk"], keep="first")

    # 掩码是「看不见的手」：不报出来，没人知道有多少条被整条隐藏了
    if mask_cfg is not None and n_masked:
        ctx.warn(f"下钻明细 content_snippet 已掩码 {n_masked:,} 条"
                 + (f"，其中 {n_no_match:,} 条的关键词在原文里一个都没找到"
                    f"（{n_no_match / n_masked:.1%}，大模型把词抽象过了），"
                    f"按 drill_mask_on_no_match="
                    f"{getattr(ctx.settings, 'drill_mask_on_no_match', 'prefix')} 处理"
                    if n_no_match else "，关键词全部命中"))
    return out[COLUMNS].sort_values(
        ["scenic_id", "travel_date", "time_granularity", "emotion_word"]).reset_index(drop=True)


def _day_granularity(ctx: RunContext) -> str:
    """下钻表 time_granularity 写什么：大盘表里「当日」那个周期的名字（rolling 1 天、不偏移，默认 td）。"""
    for g in ctx.macro_granularities:
        if g["type"] == D.MACRO_PERIOD_ROLLING and int(g.get("days", 0)) == 1 \
                and not int(g.get("offset") or 0):
            return g["name"]
    return "td"


def _window_plan(comment_facts: pd.DataFrame, ctx: RunContext) -> pd.DataFrame:
    """每个 (景区, 渠道, 输出日期)：这一天用哪一天的评论。

    当天有评论就用当天；没有就用往前最近一个有评论的日子，最多往前找当日档的上限天
    （backfill_lookback 统一时就是那个数，和大盘表每个周期一样）；找不到这一天就空着。
    出参：scenic_spot_code, platform_code, travel_date（输出日期 int）, time_granularity,
          day（= 输出日期 Timestamp）, src_date（来源日 yyyyMMdd）
    """
    cols = _PKEY + ["travel_date", "time_granularity", "day", "src_date"]
    pres = daily_channel_presence(comment_facts)
    if pres.empty or not ctx.output_dates:
        return pd.DataFrame(columns=cols)
    pres = pres.assign(_d=pd.to_datetime(pres["travel_date"].astype(str), format=D.DATE_FMT))
    present = {k: np.sort(g["_d"].to_numpy(dtype="datetime64[D]"))
               for k, g in pres.groupby(_PKEY)}
    lookback = int(ctx.backfill_lookback([1]).get(1, 0))
    days = np.array(sorted(pd.to_datetime(list(ctx.output_dates), format=D.DATE_FMT)),
                    dtype="datetime64[D]")
    one = np.timedelta64(1, "D")
    parts = []
    for (sc, ch), p in present.items():
        i = np.searchsorted(p, days, side="right") - 1        # 最近一个 <= 当天的有评论日
        src = p[np.clip(i, 0, None)]
        ok = (i >= 0) & ((days - src) <= lookback * one)
        if ok.any():
            parts.append(pd.DataFrame({"scenic_spot_code": sc, "platform_code": ch,
                                       "day": days[ok], "src_date": src[ok]}))
    if not parts:
        return pd.DataFrame(columns=cols)
    plan = pd.concat(parts, ignore_index=True)
    plan["day"] = pd.to_datetime(plan["day"])
    plan["travel_date"] = plan["day"].dt.strftime(D.DATE_FMT).astype(int)
    plan["time_granularity"] = _day_granularity(ctx)
    plan["src_date"] = pd.to_datetime(plan["src_date"]).dt.strftime(D.DATE_FMT)
    return plan[cols]


def _expand(base: pd.DataFrame, plan: pd.DataFrame) -> pd.DataFrame:
    """每条评论的行（base，带来源日 src_date）× 用到它的日子 → 下钻行。

    复制来的（那一天 != 来源日）publish_time = 那一天 00:00:00：不能是「未来」时刻，重跑要得到同一个值。
    detail_uk 带上 travel_date：同一条评论被复制到不同天，各是一行。
    """
    p = plan.rename(columns={"scenic_spot_code": "scenic_id", "platform_code": "channel"})
    out = base.merge(p, on=["scenic_id", "channel", "src_date"], how="inner")
    copy = out["day"].dt.strftime(D.DATE_FMT) != out["src_date"]
    out["publish_time"] = pd.to_datetime(out["publish_time"]).where(~copy, out["day"])
    out["detail_uk"] = [
        _uk(*k) for k in zip(out["scenic_id"], out["emotion_word"], out["channel"],
                             out["work_id"], out["comment_id"], out["travel_date"])]
    return out.drop(columns=["day", "src_date"])


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
        "on_no_match": getattr(st, "drill_mask_on_no_match", D.DRILL_NO_MATCH_PREFIX),
        "no_match_ratio": float(getattr(st, "drill_mask_no_match_ratio", D.DRILL_NO_MATCH_RATIO)),
        "no_match_extra": int(getattr(st, "drill_mask_no_match_extra", D.DRILL_NO_MATCH_EXTRA)),
        "no_match_short_ratio": float(getattr(st, "drill_mask_no_match_short_ratio",
                                              D.DRILL_NO_MATCH_SHORT_RATIO)),
    }
