# -*- coding: utf-8 -*-
"""编排：源表 → 内存计算 → ADS 表。

全程不落任何中间表（无 ODS/DWD/DWS），一次跑批就是一个进程内的
extract → normalize → build ×4 → validate → load。

    ┌── src_opinion_social_work_comment_di ──┐
    │   src_opinion_social_work_di（仅 5.2） │
    └────────────────┬───────────────────────┘
                     ↓ normalize
        comment_facts / dimension_facts / keyword_facts   （DataFrame，内存）
                     ↓ builders
        core / platform / dimension_score / content       （= 目标表结构）
                     ↓ validate → load
                  MySQL ADS 表（+ CSV 备份）
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from .builders.content import build_content
from .builders.core import build_core
from .builders.dimension import build_dimension
from .builders.macro import build_macro, macro_data_ranges
from .builders.platform import build_platform
from .context import RunContext
from .db import MySQL
from .drill_analysis import build_drill_analysis
from .loader import (TABLE_CONTENT, TABLE_CORE, TABLE_DIMENSION, TABLE_DRILL_ANALYSIS,
                     TABLE_MACRO, TABLE_PLATFORM, load_all)
from .metric_calc_domain import (CORE_WINDOWS, daily_channel_presence,
                                 daily_core_facts_by_platform)
from .metric_calc_domain import PLATFORM_WINDOWS as D_PLATFORM_WINDOWS
from .normalize import (build_comment_facts, build_dimension_facts, build_keyword_facts)
from .pusher import PushReport, cleanup_pushed_csv, push_all
from .settings import EtlSettings
from .source import fetch_comments, fetch_works
from .validate import validate_all
from .windows import fill_source_dates

log = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    tables: Dict[str, pd.DataFrame] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    load_results: List = field(default_factory=list)
    push: Optional[PushReport] = None      # 推送结果，未开启推送时为 None
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors


def _fill_report(presence: pd.DataFrame, ctx: RunContext, lookback: dict) -> str:
    """输出日期上「当日」档（1 日档上限）补了多少：复制了几组、几组往前也找不到。"""
    if 1 not in lookback:
        return "1 日档未配置回补上限，当日不补"
    m = fill_source_dates(presence, ["scenic_spot_code", "platform_code"], "travel_date",
                          ctx.output_dates, lookback[1])
    if m.empty:
        return "输出日期上每个渠道当天都有评论，没有触发补齐"
    hit = int(m["found"].sum())
    return (f"输出日期上有 {len(m):,} 个「景区·渠道·日」当天没评论：{hit:,} 个已复制往前最近一天的明细"
            f"（1 日档最多找 {lookback[1]} 天），{len(m) - hit:,} 个往前也没找到、保持为 0")


def compute_load_range(output_dates: List[str], settings: EtlSettings) -> tuple:
    """反推取数区间。

    最长窗口 365 天 + 一个上一周期才够算 365d 环比，但需求六.2 明确「近半年」，
    所以实际取数长度由 settings.lookback_days 封顶：
    历史不够时长窗口会退化成「已有历史的累计」，这是可接受的降级，
    但**必须**在跑批报告里说清楚，不能让人以为 365d 真的是一年。
    """
    start_out = min(output_dates)
    need = settings.lookback_days
    load_start = (pd.to_datetime(start_out, format="%Y%m%d")
                  - pd.Timedelta(days=need - 1)).strftime("%Y%m%d")
    return load_start, max(output_dates)


def run(settings: EtlSettings,
        dates: List[str],
        scenic_codes: Optional[List[str]] = None,
        db: Optional[MySQL] = None,
        comments_df: Optional[pd.DataFrame] = None,
        works_df: Optional[pd.DataFrame] = None,
        push_progress=None) -> PipelineResult:
    """跑一次批。

    dates          要产出的目标日期（yyyyMMdd）
    comments_df    直接喂源数据（测试/离线用）；为 None 则从 MySQL 取
    push_progress  推送进度条工厂（见 progress.push_progress_factory），None 不显示
    """
    t0 = time.time()
    res = PipelineResult()

    load_start, load_end = compute_load_range(dates, settings)
    ctx = RunContext(settings=settings, output_dates=sorted(dates),
                     load_start=load_start, load_end=load_end,
                     scenic_codes=scenic_codes)
    log.info("目标日期 %s ~ %s | 取数区间 %s ~ %s（%d 天）",
             ctx.output_dates[0], ctx.output_dates[-1], load_start, load_end,
             settings.lookback_days)

    # macro 大盘表还要「上期」与「去年同期」的数据（需求 2.0 的环比/同比），
    # 这两段可能落在 [load_start, load_end] 之外，单独多取；其余四张表只用取数区间。
    # 明细补齐要往前多取一段（domain §1.8：最多往前找几天），只当补齐的来源。
    fill_start = ctx.fill_start
    ranges = macro_data_ranges(ctx) if settings.enable_macro_metric \
        else [(fill_start, load_end)]
    extra = [r for r in ranges if r != (fill_start, load_end)]
    if extra:
        log.info("macro 表额外取数：%s", "、".join(f"{a}~{b}" for a, b in ranges))

    # ---- 1. 抽取 ----
    if comments_df is None:
        if db is None:
            raise ValueError("未提供 comments_df 时必须传入已连接的 MySQL 实例")
        parts = [fetch_comments(db, a, b, scenic_codes) for a, b in ranges]
        parts = [p for p in parts if p is not None and not p.empty]
        comments_df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if comments_df is None or comments_df.empty:
        res.errors.append(f"取数区间 {load_start}~{load_end} 没有任何评论数据")
        res.elapsed = time.time() - t0
        return res

    # ---- 2. 标准化（全部在内存，不落中间表）----
    cf_all = build_comment_facts(comments_df)
    # 取数区间外的评论一律不参与计算。MySQL 侧已经按 publish_time 过滤过，
    # 但离线 CSV / 手工导出不一定 —— 源表里混进一条 2013 年的评论，
    # 就能把滚动面板的日历从 400 天撑到 4700 天。
    n_all = len(cf_all)
    in_range = pd.Series(False, index=cf_all.index)
    for a, b in ranges:
        in_range |= cf_all["travel_date"].between(a, b)
    cf_all = cf_all[in_range]
    if len(cf_all) < n_all:
        res.notes.append(f"取数区间外的评论已剔除 {n_all - len(cf_all):,} 条"
                         f"（区间 {'、'.join(f'{a}~{b}' for a, b in ranges)}）")
    # 四张既有表看 [fill_start, load_end]：取数区间 + 往前补齐要用的那一段
    cf = cf_all[cf_all["travel_date"].between(fill_start, load_end)]
    if not cf["travel_date"].between(load_start, load_end).any():
        res.errors.append(f"取数区间 {load_start}~{load_end} 内没有评论数据")
        res.elapsed = time.time() - t0
        return res
    dim_all = build_dimension_facts(cf_all, settings.unknown_dimension_policy)
    kw_all = build_keyword_facts(cf_all)
    dim_facts = dim_all[dim_all["travel_date"].between(fill_start, load_end)]
    kw_facts = kw_all[kw_all["travel_date"].between(fill_start, load_end)]
    res.notes.append(f"评论明细 {len(cf):,} 行 | 维度明细 {len(dim_facts):,} 行 | "
                     f"关键词明细 {len(kw_facts):,} 行")

    # ---- 3. 计算四张表 ----
    core = build_core(cf, ctx)
    platform = build_platform(cf, ctx)

    # 「渠道当天有没有评论」：维度 / 内容表补齐明细时都看这一份（domain §1.9）
    presence = daily_channel_presence(cf)
    dimension = build_dimension(dim_facts, ctx, presence)
    content = build_content(kw_facts, daily_core_facts_by_platform(cf), ctx, presence)

    res.tables = {TABLE_CORE: core, TABLE_PLATFORM: platform,
                  TABLE_DIMENSION: dimension, TABLE_CONTENT: content}

    # ---- 4. 5.2 下钻分析表（词 → 评论 → 作品）----
    if settings.enable_drill_analysis:
        if works_df is None and db is not None:
            work_ids = cf.get("work_id")
            works_df = fetch_works(db, fill_start, load_end, scenic_codes,
                                   work_ids.dropna().unique().tolist()
                                   if work_ids is not None else None)
        drill = build_drill_analysis(
            cf, works_df if works_df is not None else pd.DataFrame(), ctx)
        res.tables[TABLE_DRILL_ANALYSIS] = drill
        res.notes.append(f"下钻明细（词→评论→作品）{len(drill):,} 行")

    # ---- 4b. 需求 2.0 大盘表（景区 × 日期 × 周期粒度 × 渠道）----
    macro = None
    if settings.enable_macro_metric:
        macro = build_macro(cf_all, dim_all, kw_all, ctx)
        res.tables[TABLE_MACRO] = macro
        res.notes.append(f"大盘 KPI（{len(ctx.macro_granularities)} 个周期 × 渠道）"
                         f"{len(macro):,} 行")

    # ---- 5. 校验 ----
    from .builders.platform import platform_scope
    codes = platform_scope(ctx, cf["platform_code"].drop_duplicates().tolist())
    checks = {"core": core, "platform": platform,
              "dimension": dimension, "content": content,
              "platform_codes": codes,
              "backfill_mode": settings.backfill_mode,
              "score_formula": settings.score_formula,
              "score_weights": settings.score_weights}
    if macro is not None:
        checks["macro"] = macro
        checks["macro_granularities"] = [g["name"] for g in ctx.macro_granularities]
    res.errors = validate_all(checks)

    # 回补可见化：输出日期里有多少「景区·渠道·日」是复制来的明细
    lb = ctx.backfill_lookback(D_PLATFORM_WINDOWS)
    if lb:
        res.notes.append(
            "缺数回补=先补明细再算指标（渠道当天没评论 → 复制往前最近一天的明细），"
            "各窗口最多往前找 " + " / ".join(f"{w}日→{d}天" for w, d in sorted(lb.items())))
        res.notes.append(_fill_report(presence, ctx, lb))
    else:
        res.notes.append("缺数回补已关闭（backfill_mode=off），当天没评论即为 0")
    # 行存在规则的产出可见化：补了多少条「当天没数据但窗口有值」的行
    if not platform.empty:
        zero = int((platform["comment_cnt"] == 0).sum())
        res.notes.append(
            f"渠道行完整性：{len(codes)} 个渠道 × {platform['travel_date'].nunique()} 天 "
            f"× {platform['scenic_spot_code'].nunique()} 个景区 = {len(platform):,} 行，"
            f"其中当日为 0 的补零行 {zero:,} 条（窗口指标照常）")
    if not core.empty:
        z = int((core["comment_count"] == 0).sum())
        if z:
            res.notes.append(f"core：{z} 个「零评论日」也出了行，窗口指标保留")

    # 长窗口降级提示：历史不足时 90d/365d 会退化成「全部历史累计」
    span_days = (pd.to_datetime(load_end, format="%Y%m%d")
                 - pd.to_datetime(load_start, format="%Y%m%d")).days + 1
    for w in CORE_WINDOWS:
        if w > span_days:
            res.notes.append(
                f"提示：取数区间只有 {span_days} 天，{w}d 窗口退化为「全部历史累计」，"
                f"与更短窗口可能相等（不是 bug，见 README「各周期得分相同 ≠ bug」）")
            break

    # ctx.warn() 攒下来的告警统一在这里出口，别散在各处 print
    res.notes.extend(ctx.warnings)

    # ---- 6. 落库 ----
    if settings.write_csv or settings.write_db:
        # 写库前按「本次景区 × 本次日期」先删再插（见 loader 模块说明）。
        # 景区取命令行指定的；没指定就取这次数据里出现的全部景区。
        scenics = scenic_codes or sorted(cf["scenic_spot_code"].astype(str).unique())
        res.load_results = load_all(db, res.tables, settings=settings,
                                    scenics=scenics, dates=ctx.output_dates)

    # ---- 7. 推送下游（默认关闭；失败不改变跑批成败，除非 push_strict）----
    # 放在落库**之后**：先保证数据落定，再推。推失败了数据还在库里，
    # 补推一次就行；反过来推成功但落库失败，下游拿到的就是查无对证的数据。
    if getattr(settings, "push_enabled", False):
        res.push = push_all(res.tables, settings=settings, progress_factory=push_progress)
        res.notes.extend(res.push.notes())
        # 推送成功的表不在本地留 CSV（数据会越积越大）；推失败的留着补推
        if (settings.write_csv and getattr(settings, "push_cleanup_local", True)
                and not getattr(settings, "push_dry_run", False)):
            removed = cleanup_pushed_csv(settings.output_dir, res.push.results)
            if removed:
                res.notes.append(f"推送成功，已删除本地 CSV {len(removed)} 个（{settings.output_dir}）")
        if not res.push.ok:
            if getattr(settings, "push_strict", False):
                res.errors.extend(res.push.errors)
            else:
                res.notes.extend(["（推送失败不影响跑批成败，数据已落库，可用 "
                                  "`push` 子命令补推）"] + res.push.errors)

    res.elapsed = time.time() - t0
    return res
