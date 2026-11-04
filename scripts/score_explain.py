#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""综合得分 / 维度综合得分 核对脚本：从源表评论重算一遍，把过程摊开给人看。

对应大盘表 ads_trf_social_opinion_macro_gran_metric_di 的两个字段：
  · overall_sentiment_score  游客综合情感得分（综合得分）
  · dimension_breakdown      各一级维度的得分（维度综合得分，JSON 里的 dimension1Score）

输出四部分（屏幕打印 + 写文件）：
  1. 明细     本期用到的每一条评论、每一条维度提及（含缺数回补复制来的）
  2. 统计值   按天 × 渠道的好/中/差条数；合计；各一级维度的好/中/差提及数
  3. 计算公式 现行口径与权重
  4. 计算值   代入数字的算式、结果，并和库里大盘表存的值对比

取数、情感判定、缺数回补都直接调引擎自己的函数（与跑批同一套口径），
脚本只负责把中间过程摆出来。只读，不写库、不推送。

用法（在项目根目录）：
    python scripts/score_explain.py --scenic PFTSCA01002434 --date 20261102
    python scripts/score_explain.py --scenic PFTSCA01002434 --date 20261102 --gran latest_7d --channel douyin

    --gran     周期，逗号分隔可写多个，默认 td,latest_7d,latest_30d,mtd（今日 / 近7日 / 近30日 / 本月）；
               可选 td / latest_1d / latest_7d / wtd / latest_30d / mtd / latest_60d / latest_90d / qtd
               （以 settings 里配置的为准）
    --channel  渠道，默认 all（整体 = 各渠道相加）；也可写 douyin / weibo …
    --out      文件写到哪个目录，默认 output/score_explain/<景区>_<日期>_<渠道>/，
               每个周期一个子目录，另有一份各周期的汇总
    --show     屏幕上每个周期的明细打印几行，默认 10（文件里是全部）
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402

from engin_cli import metric_calc_domain as D  # noqa: E402
from engin_cli.builders.platform import platform_scope  # noqa: E402
from engin_cli.context import RunContext  # noqa: E402
from engin_cli.db import MySQL  # noqa: E402
from engin_cli.loader import TABLE_MACRO  # noqa: E402
from engin_cli.normalize import (build_comment_facts, build_dimension_facts,  # noqa: E402
                                 parse_json_array)
from engin_cli.settings import load_settings  # noqa: E402
from engin_cli.source import fetch_comments  # noqa: E402

SENT_NAME = {D.POSITIVE: "好评", D.NEUTRAL: "中评", D.NEGATIVE: "差评"}
CONTENT_SHOW = 60          # 明细里评论内容最多显示多少字
EPS = 5e-6                 # 和库里的值比较的容差（库里存 6 位小数）


def _ds(ts) -> str:
    return pd.Timestamp(ts).strftime(D.DATE_FMT)


def _num(x) -> str:
    """数字的展示：整数不带小数点，其余去掉多余的 0。"""
    x = float(x)
    return str(int(x)) if x.is_integer() else f"{x:.6f}".rstrip("0").rstrip(".")


# ══════════════════════════════════════════════════════════════════════
# 公式 / 算式
# ══════════════════════════════════════════════════════════════════════
def formula_text(formula: str, w: tuple) -> list:
    wp, wn, wg = (_num(v) for v in w)
    if formula == D.SCORE_FORMULA_V1:
        return [
            f"口径：{D.SCORE_FORMULA_V1}（历史口径，值域 0~{_num(D.V1_SCORE_MAX)}）",
            f"S = {_num(D.V1_SCORE_NEUTRAL)} + (好评数 - 差评数) ÷ 总数 × {_num(D.V1_SCORE_SPAN)}"
            f" × min(1, 总数 ÷ {D.CONFIDENCE_T})",
            f"总数 = 0 时 S = {_num(D.V1_SCORE_NEUTRAL)}",
        ]
    return [
        f"口径：{D.SCORE_FORMULA_V2}（现行口径，加权占比法，值域 "
        f"{_num(D.SCORE_SCALE * min(w))}~{_num(D.SCORE_SCALE * max(w))}）",
        f"S = {_num(D.SCORE_SCALE)} × (好评数×{wp} + 中评数×{wn} + 差评数×{wg}) ÷ 总数",
        f"  = {_num(D.SCORE_SCALE)} × (好评率×{wp} + 中评率×{wn} + 差评率×{wg})",
        f"总数 = 好评数 + 中评数 + 差评数；总数 = 0 时 S = {_num(D.SCORE_SCALE)} × {wn}"
        f" = {_num(D.SCORE_SCALE * w[1])}（按全部中评计）",
        "综合得分：样本 = 本期全部评论（每条评论一条，按评论自身的情感）",
        "维度得分：样本 = 本期该一级维度的全部提及（一条评论命中几个维度算几条，按该维度自己的情感）",
        f"结果保留 {D.MACRO_SCORE_DECIMALS} 位小数（与大盘表一致）",
    ]


def score_steps(pos, neu, neg, formula: str, w: tuple) -> tuple:
    """(代入数字的算式, 得分)。得分直接调引擎的 sentiment_score_from_counts。"""
    tot = pos + neu + neg
    s = float(D.sentiment_score_from_counts(pos, neu, neg, tot, formula=formula,
                                            decimals=D.MACRO_SCORE_DECIMALS, weights=w))
    if tot == 0:
        if formula == D.SCORE_FORMULA_V1:
            return "总数 = 0，取中性分", s
        return f"总数 = 0，按全部中评计：{_num(D.SCORE_SCALE)} × {_num(w[1])}", s
    if formula == D.SCORE_FORMULA_V1:
        conf = min(1.0, tot / D.CONFIDENCE_T)
        return (f"{_num(D.V1_SCORE_NEUTRAL)} + ({_num(pos)} - {_num(neg)}) ÷ {_num(tot)} × "
                f"{_num(D.V1_SCORE_SPAN)} × {_num(round(conf, 6))}"), s
    weighted = pos * w[0] + neu * w[1] + neg * w[2]
    return (f"{_num(D.SCORE_SCALE)} × ({_num(pos)}×{_num(w[0])} + {_num(neu)}×{_num(w[1])} + "
            f"{_num(neg)}×{_num(w[2])}) ÷ {_num(tot)} = {_num(D.SCORE_SCALE)} × "
            f"{_num(round(weighted, 6))} ÷ {_num(tot)}"), s


# ══════════════════════════════════════════════════════════════════════
# 缺数回补：统计日 → 来源日（与 builders/macro.py 的 _FillPlan 同一条规则）
# ══════════════════════════════════════════════════════════════════════
def fill_sources(present_days: list, days: list, lookback: int) -> dict:
    """渠道当天有评论就用当天；没有 → 往前最近一个有评论的日子，最多往前找 lookback 天。

    出参 {统计日: 来源日 或 None（找不到 = 这天按 0 算）}
    """
    present = sorted(pd.Timestamp(d) for d in present_days)
    out = {}
    for d in days:
        src = None
        for p in reversed(present):
            if p <= d:
                src = p
                break
        out[d] = src if src is not None and (d - src).days <= int(lookback) else None
    return out


DEFAULT_GRANS = "td,latest_7d,latest_30d,mtd"


def _pad(text: str, width: int, right: bool = False) -> str:
    """按屏幕显示宽度补空格（中文占 2 格），汇总表才对得齐。"""
    import unicodedata
    n = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(text))
    fill = " " * max(1, width - n)
    return fill + str(text) if right else str(text) + fill


def _cmp(mine, theirs):
    if theirs is None or pd.isna(theirs):
        return "—"
    return "一致" if abs(mine - theirs) < EPS else f"不一致（差 {mine - theirs:+.6f}）"


def explain_period(spec, *, ctx, cf, raw, stored_row, scenic, scenic_name, date, channel,
                   out_dir, show) -> dict:
    """一个周期：明细 → 统计值 → 公式 → 计算值，写文件、打印，返回汇总用的一行。"""
    st = ctx.settings
    formula, weights = st.score_formula, ctx.score_weights

    # ---- 本期区间、补数上限 ----
    lo, hi = D.macro_period_ranges(spec, date)["cur"]
    win = D.macro_backfill_window(spec)
    lookback = int(ctx.backfill_lookback([win]).get(win, 0))
    days = list(pd.date_range(lo, hi, freq="D"))
    seen = cf["platform_code"].drop_duplicates().tolist() if not cf.empty else []
    channels = platform_scope(ctx, seen) if channel == D.MACRO_CHANNEL_ALL else [channel]

    # ---- 1. 明细：每个渠道每一天 → 来源日的评论 ----
    day_rows, detail = [], []
    for ch in channels:
        sub = cf[cf["platform_code"] == ch] if not cf.empty else cf
        src_map = fill_sources(sub["travel_date"].unique().tolist() if not sub.empty else [],
                               days, lookback)
        for d in days:
            src = src_map[d]
            part = sub[sub["travel_date"] == _ds(src)] if src is not None else sub.iloc[0:0]
            cnt = {s: int((part["sentiment"] == s).sum()) for s in SENT_NAME}
            day_rows.append({"统计日": _ds(d), "渠道": ch,
                             "来源日": _ds(src) if src is not None else "",
                             "是否补数": "是" if src is not None and src != d else
                             ("无数据" if src is None else ""),
                             "好评": cnt[D.POSITIVE], "中评": cnt[D.NEUTRAL],
                             "差评": cnt[D.NEGATIVE], "合计": len(part)})
            if len(part):
                detail.append(part.assign(stat_day=_ds(d), src_day=_ds(src)))
    det = pd.concat(detail, ignore_index=True) if detail else cf.iloc[0:0].assign(
        stat_day=[], src_day=[])

    src_cols = raw.set_index("comment_uk")
    cmt = pd.DataFrame({
        "统计日": det["stat_day"],
        "来源日": det["src_day"],
        "是否补数": (det["stat_day"] != det["src_day"]).map({True: "是", False: ""}),
        "渠道": det["platform_code"],
        "评论ID": det.get("comment_id"),
        "作品ID": det.get("work_id"),
        "发布时间": det["publish_dt"].astype(str) if len(det) else [],
        "sentiment_score原值": det["comment_uk"].map(src_cols["sentiment_score"])
        if len(det) and "sentiment_score" in src_cols else None,
        "sentiment_label原值": det["comment_uk"].map(src_cols["sentiment_label"])
        if len(det) and "sentiment_label" in src_cols else None,
        "判定": det["sentiment"].map(SENT_NAME),
        "评论内容": det.get("content", pd.Series(dtype=object)).astype(str).str[:CONTENT_SHOW]
        if len(det) else [],
    })

    # 维度明细：一条评论的 dimension_tags 炸开，每个维度按自己的情感判定（同 build_dimension_facts）
    policy = getattr(st, "unknown_dimension_policy", "keep")
    dim_rows = []
    for r in det.itertuples(index=False):
        for t in parse_json_array(getattr(r, "dimension_tags", None)):
            if not isinstance(t, dict):
                continue
            l1 = str(t.get("dim1") or "").strip()
            if not l1 or (l1 not in D.L1_DIMENSIONS and policy == "drop"):
                continue
            s = D.judge_sentiment(t.get("sentiment"), t.get("sentiment_label"))
            dim_rows.append({"统计日": r.stat_day, "来源日": r.src_day,
                             "是否补数": "是" if r.stat_day != r.src_day else "",
                             "渠道": r.platform_code, "评论ID": getattr(r, "comment_id", ""),
                             "一级维度": l1, "二级维度": str(t.get("dim2") or "").strip(),
                             "三级维度": str(t.get("dim3") or "").strip(),
                             "维度情感原值": t.get("sentiment"),
                             "维度情感标签原值": t.get("sentiment_label"),
                             "判定": SENT_NAME[s], "_s": s})
    dim = pd.DataFrame(dim_rows, columns=["统计日", "来源日", "是否补数", "渠道", "评论ID",
                                          "一级维度", "二级维度", "三级维度", "维度情感原值",
                                          "维度情感标签原值", "判定", "_s"])
    # 自检：和引擎的 build_dimension_facts 炸出来的条数一致（口径没有走样）
    if len(det):
        eng = build_dimension_facts(det, policy)
        if len(eng) != len(dim):
            print(f"⚠ {spec['name']} 维度明细条数与引擎不一致：脚本 {len(dim)}，引擎 {len(eng)}")

    # ---- 2. 统计值 ----
    by_day = pd.DataFrame(day_rows)
    pos = float(by_day["好评"].sum()) if len(by_day) else 0.0
    neu = float(by_day["中评"].sum()) if len(by_day) else 0.0
    neg = float(by_day["差评"].sum()) if len(by_day) else 0.0
    tot = pos + neu + neg

    dims = list(D.L1_DIMENSIONS) + sorted(set(dim["一级维度"]) - set(D.L1_DIMENSIONS))
    dim_stat = []
    for d1 in dims:
        g = dim[dim["一级维度"] == d1]
        p, n, b = (float((g["_s"] == s).sum()) for s in (D.POSITIVE, D.NEUTRAL, D.NEGATIVE))
        expr, sc = score_steps(p, n, b, formula, weights)
        dim_stat.append({"一级维度": d1, "好评": int(p), "中评": int(n), "差评": int(b),
                         "合计": int(p + n + b), "算式": expr, "得分": sc})
    dim_df = pd.DataFrame(dim_stat)

    # ---- 3/4. 公式与计算值 ----
    expr, overall = score_steps(pos, neu, neg, formula, weights)
    st_overall, st_dims, st_total = None, {}, None
    st_info = "库里没有这一行（还没跑过这一天，或已被删除）"
    if stored_row is not None:
        st_overall = float(stored_row["overall_sentiment_score"])
        for it in json.loads(stored_row["dimension_breakdown"] or "[]"):
            st_dims[it.get("dimension1")] = float(it.get("dimension1Score"))
        st_total = int(stored_row["comment_total"])
        st_info = (f"评论总数：重算 {_num(tot)}，库里 comment_total {st_total} → "
                   f"{'一致' if st_total == int(tot) else '不一致（得分对不上先看这里：样本条数不同）'}"
                   f"；库里这一行写入时间 {stored_row['etl_time']}")

    dim_df["库里的值"] = [st_dims.get(d1) for d1 in dim_df["一级维度"]]
    dim_df["对比"] = [_cmp(m, t) for m, t in zip(dim_df["得分"], dim_df["库里的值"])]

    pct = (lambda x: f"{x / tot * 100:.2f}%") if tot else (lambda x: "-")
    fill_days = int((by_day["是否补数"] == "是").sum()) if len(by_day) else 0
    empty_days = int((by_day["是否补数"] == "无数据").sum()) if len(by_day) else 0
    lines = [
        "=" * 72,
        f"景区 {scenic} {scenic_name} | 日期 {date} | 周期 {spec['name']}（{spec['label']}）"
        f" | 渠道 {channel}",
        f"本期区间 {_ds(lo)} ~ {_ds(hi)}（{len(days)} 天）| 参与渠道 {', '.join(channels)}",
        f"缺数回补：渠道当天没评论 → 用往前最近一天的评论，最多往前找 {lookback} 天"
        f"（按 {win} 日档位）；本期 {fill_days} 个「渠道·天」是补的，{empty_days} 个找不到按 0 算",
        "",
        "【2. 统计值】",
        f"  评论条数：好评 {_num(pos)}（{pct(pos)}）、中评 {_num(neu)}（{pct(neu)}）、"
        f"差评 {_num(neg)}（{pct(neg)}），合计 {_num(tot)}",
        f"  维度提及：合计 {len(dim)} 条（一条评论可命中多个维度）",
    ]
    for r in dim_stat:
        lines.append(f"    {r['一级维度']:<6s} 好 {r['好评']:>5d}  中 {r['中评']:>5d}  "
                     f"差 {r['差评']:>5d}  合计 {r['合计']:>5d}")
    lines += ["", "【3. 计算公式】"] + ["  " + x for x in formula_text(formula, weights)]
    lines += ["", "【4. 计算值】",
              "  综合得分",
              f"    {expr}",
              f"    = {overall:.6f}",
              f"    库里的值 {'' if st_overall is None else f'{st_overall:.6f}'}  "
              f"→ {_cmp(overall, st_overall)}",
              "  维度综合得分"]
    for r in dim_df.itertuples(index=False):
        theirs = "" if r.库里的值 is None or pd.isna(r.库里的值) else f"{r.库里的值:.6f}"
        lines.append(f"    {r.一级维度:<6s} {r.算式} = {r.得分:.6f}  库里 {theirs} → {r.对比}")
    lines += [f"  {st_info}", "=" * 72]

    os.makedirs(out_dir, exist_ok=True)
    files = {
        "1_评论明细.csv": cmt,
        "1_维度明细.csv": dim.drop(columns=["_s"]),
        "2_统计值_按天按渠道.csv": by_day,
        "2_统计值_维度.csv": dim_df[["一级维度", "好评", "中评", "差评", "合计"]],
        "4_计算值_维度.csv": dim_df,
    }
    for name, df in files.items():
        df.to_csv(os.path.join(out_dir, name), index=False, encoding="utf-8-sig")
    with open(os.path.join(out_dir, "3_4_公式与计算值.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    # ---- 屏幕输出 ----
    print()
    print("#" * 72)
    print(f"# 周期 {spec['name']}（{spec['label']}）")
    with pd.option_context("display.max_columns", None, "display.width", 200,
                           "display.unicode.east_asian_width", True):
        print("【1. 明细】评论 %d 条、维度提及 %d 条（屏幕只显示前 %d 条，全部见文件）"
              % (len(cmt), len(dim), show))
        if len(cmt) and show > 0:
            print(cmt.head(show).to_string(index=False))
    print("\n".join(lines))

    row = {"周期": spec["name"], "周期名称": spec["label"],
           "本期区间": f"{_ds(lo)}~{_ds(hi)}", "好评": int(pos), "中评": int(neu),
           "差评": int(neg), "评论数": int(tot),
           "库里评论数": st_total, "综合得分": overall, "库里综合得分": st_overall,
           "综合得分对比": _cmp(overall, st_overall)}
    for r in dim_df.itertuples(index=False):
        row[f"{r.一级维度}得分"] = r.得分
        row[f"{r.一级维度}对比"] = r.对比
    return row


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="综合得分 / 维度综合得分 核对（明细 → 统计值 → 公式 → 计算值）")
    ap.add_argument("--scenic", required=True, help="景区编码")
    ap.add_argument("--date", required=True, help="跑数日期 yyyyMMdd（= 大盘表 travel_date）")
    ap.add_argument("--gran", default=DEFAULT_GRANS,
                    help=f"周期（= 大盘表 time_granularity），逗号分隔，默认 {DEFAULT_GRANS}")
    ap.add_argument("--channel", default=D.MACRO_CHANNEL_ALL, help="渠道，默认 all（整体）")
    ap.add_argument("--out", help="输出目录，默认 output/score_explain/<景区>_<日期>_<渠道>/")
    ap.add_argument("--show", type=int, default=10, help="屏幕上每个周期的明细打印几行，默认 10")
    args = ap.parse_args(argv)

    scenic, date = args.scenic.strip(), args.date.strip()
    channel = args.channel.strip().lower()
    st = load_settings()
    ctx = RunContext(settings=st, output_dates=[date], load_start=date, load_end=date,
                     scenic_codes=[scenic])
    specs = {g["name"]: g for g in ctx.macro_granularities}
    grans = [g.strip() for g in args.gran.split(",") if g.strip()]
    bad = [g for g in grans if g not in specs]
    if bad or not grans:
        print(f"没有周期 {'、'.join(bad) or '（空）'}，可选：{' / '.join(specs)}")
        return 2
    chosen = [specs[g] for g in dict.fromkeys(grans)]

    # ---- 取数范围：各周期本期区间往前再多取补数上限，合并成一段，只查一次库 ----
    spans = []
    for spec in chosen:
        lo, hi = D.macro_period_ranges(spec, date)["cur"]
        win = D.macro_backfill_window(spec)
        lb = int(ctx.backfill_lookback([win]).get(win, 0))
        spans.append((lo - pd.Timedelta(days=lb), hi))
    fetch_lo, fetch_hi = min(a for a, _ in spans), max(b for _, b in spans)

    db = MySQL(st.db)
    try:
        raw = fetch_comments(db, _ds(fetch_lo), _ds(fetch_hi), [scenic])
        ads = db.cfg.ads_db()
        tbl = f"`{ads}`.`{TABLE_MACRO}`" if ads else f"`{TABLE_MACRO}`"
        try:
            stored = db.query_df(
                f"select time_granularity, overall_sentiment_score, dimension_breakdown, "
                f"comment_total, etl_time from {tbl} where scenic_id = %s and travel_date = %s "
                f"and channel = %s and time_granularity in ({','.join(['%s'] * len(chosen))})",
                [scenic, int(date), channel] + [g["name"] for g in chosen])
        except Exception as e:  # 表不存在等：不影响重算
            print(f"（读大盘表失败，只做重算：{e}）")
            stored = pd.DataFrame(columns=["time_granularity"])
    finally:
        db.close()

    # ---- 标准化：与跑批同一个函数（情感判定 / 日期归属）----
    if raw is None or raw.empty:
        raw = pd.DataFrame(columns=["scenic_id"])
    raw = raw.reset_index(drop=True)
    raw["comment_uk"] = raw.index          # 借这一列把源表原值（情感原文）带回明细
    cf = build_comment_facts(raw) if not raw.empty else build_comment_facts(pd.DataFrame())
    if not cf.empty:
        cf = cf[(cf["scenic_spot_code"] == scenic)
                & cf["travel_date"].between(_ds(fetch_lo), _ds(fetch_hi))]
    scenic_name = (cf["scenic_spot_name"].iloc[0] if not cf.empty else "")

    out = args.out or os.path.join(ROOT, "output", "score_explain",
                                   f"{scenic}_{date}_{channel}")
    summary = []
    for spec in chosen:
        hit = stored[stored["time_granularity"] == spec["name"]]
        summary.append(explain_period(
            spec, ctx=ctx, cf=cf, raw=raw, stored_row=hit.iloc[0] if len(hit) else None,
            scenic=scenic, scenic_name=scenic_name, date=date, channel=channel,
            out_dir=os.path.join(out, f"{spec['name']}_{spec['label']}"), show=args.show))

    # ---- 各周期汇总 ----
    sm = pd.DataFrame(summary)
    os.makedirs(out, exist_ok=True)
    sm.to_csv(os.path.join(out, "0_汇总_各周期得分.csv"), index=False, encoding="utf-8-sig")
    dims = [c[:-2] for c in sm.columns if c.endswith("得分") and c not in ("综合得分", "库里综合得分")]
    print()
    print("#" * 72)
    print(f"# 汇总：景区 {scenic} {scenic_name} | 日期 {date} | 渠道 {channel}")
    cols = [("周期", 20, False), ("本期区间", 19, False), ("评论数", 8, True),
            ("综合得分", 11, True)] + [(d + "得分", 13, True) for d in dims]
    print("".join(_pad(c, w, right) for c, w, right in cols))
    for r in summary:
        vals = [f"{r['周期']}({r['周期名称']})", r["本期区间"], str(r["评论数"]),
                f"{r['综合得分']:.6f}"] + [f"{r[d + '得分']:.6f}" for d in dims]
        print("".join(_pad(v, w, right) for v, (_, w, right) in zip(vals, cols)))
    marks = [c for c in sm.columns if c.endswith("对比")]
    bad = [(r["周期"], c[:-2]) for r in summary for c in marks
           if r[c] not in ("一致", "—")]
    miss = [r["周期"] for r in summary if r["库里综合得分"] is None or pd.isna(r["库里综合得分"])]
    if bad:
        print("与库里不一致：" + "、".join(f"{g} {c}" for g, c in bad))
    elif miss:
        print("库里没有这些周期的行，只做了重算：" + "、".join(miss))
    else:
        print("各周期综合得分、维度得分与库里全部一致")
    print(f"文件已写到：{out}（每个周期一个子目录，0_汇总_各周期得分.csv 是汇总）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
