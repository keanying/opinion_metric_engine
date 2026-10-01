# -*- coding: utf-8 -*-
"""命令行入口（opinion_metric_engine · 舆情指标引擎）。

  # 跑昨天（默认）
  python -m engin_cli.cli run

  # 跑指定一天 / 一段区间
  python -m engin_cli.cli run --date 20260901
  python -m engin_cli.cli run --start-date 20260801 --end-date 20260901

  # 只算不写库（先看 CSV）
  python -m engin_cli.cli run --date 20260901 --skip-db

  # 只跑某几个景区
  python -m engin_cli.cli run --date 20260901 --scenic PFTSCA01002434,PFT_S_00001

  # 离线跑：直接喂两张源表的 CSV，不连库
  python -m engin_cli.cli run --date 20260901 \
      --comments-csv sample/comments.csv --works-csv sample/works.csv --skip-db

  # 跑批后按规则文档打印一份看板取数结果（肉眼验收）
  python -m engin_cli.cli report --scenic PFTSCA01002434 --date 20260901 --period week
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional

import pandas as pd

from .analytics import (dimension_trend, kpi_cards, negative_surge, overall_score,
                        platform_distribution, top_dimensions, trend_stacked, wordcloud)
from .db import MySQL
from .loader import (TABLE_CONTENT, TABLE_CORE, TABLE_DIMENSION,
                     TABLE_DRILL_ANALYSIS, TABLE_PLATFORM)
from .pipeline import run as run_pipeline
from .repair import (CORE_SCORE_FIELDS, DIM_SCORE_FIELDS, repair_core,
                     repair_dimension)
from .settings import load_settings
from .source import fetch_comments_csv, fetch_works_csv


def _setup_log(level: str):
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S")


def _dates(args) -> List[str]:
    if args.date:
        return [str(args.date)]
    if args.start_date and args.end_date:
        rng = pd.date_range(pd.to_datetime(args.start_date, format="%Y%m%d"),
                            pd.to_datetime(args.end_date, format="%Y%m%d"), freq="D")
        return [d.strftime("%Y%m%d") for d in rng]
    # 默认跑昨天：评论当天还在陆续产生，跑当天会算出一个必然偏低的量
    return [(pd.Timestamp.now().normalize() - pd.Timedelta(days=1)).strftime("%Y%m%d")]


def _parse_lookback(text: str) -> dict:
    """解析 --backfill-lookback "1:5,7:10,30:20" → {1: 5, 7: 10, 30: 20}。

    只覆盖写到的档位，没写的仍用 domain.BACKFILL_LOOKBACK 的默认值。
    """
    out = {}
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise SystemExit(f"--backfill-lookback 格式错误：{part!r}，应形如 7:10")
        w, d = part.split(":", 1)
        out[int(w)] = int(d)
    return out


def cmd_run(args) -> int:
    st = load_settings()
    if args.lookback_days:
        st.lookback_days = int(args.lookback_days)
    if args.output_dir:
        st.output_dir = args.output_dir
    st.write_db = not args.skip_db
    st.write_csv = not args.skip_csv
    st.dry_run = bool(args.dry_run)
    if args.no_backfill:
        st.backfill_mode = "off"
    if args.backfill_lookback:
        st.backfill_lookback = _parse_lookback(args.backfill_lookback)
    if args.no_drill_analysis:
        st.enable_drill_analysis = False
    _apply_push_args(st, args)
    _setup_log(args.log_level or st.log_level)

    dates = _dates(args)
    scenic = [s.strip() for s in args.scenic.split(",")] if args.scenic else None

    comments_df = fetch_comments_csv(args.comments_csv) if args.comments_csv else None
    works_df = fetch_works_csv(args.works_csv) if args.works_csv else None

    db = None
    if comments_df is None or st.write_db:
        db = MySQL(st.db)

    try:
        res = run_pipeline(st, dates, scenic_codes=scenic, db=db,
                           comments_df=comments_df, works_df=works_df)
    finally:
        if db is not None:
            db.close()

    from . import __version__
    print("\n" + "=" * 72)
    print(f"跑批完成 · engin_cli v{__version__}   用时 {res.elapsed:.1f}s   "
          f"目标日期 {dates[0]} ~ {dates[-1]}")
    for t, df in res.tables.items():
        print(f"  {t:<52} {len(df):>8,} 行")
    for n in res.notes:
        print(f"  · {n}")
    for r in res.load_results:
        print(f"  写入 {r.table:<48} {r.rows:>8,} 行  [{r.strategy}]"
              + (f"  清理 {r.deleted} 行" if r.deleted else ""))
    if res.push is not None:
        for r in res.push.results:
            if r.skipped and not r.error:
                continue
            print(f"  推送 {r.table:<48} {r.rows:>8,} 行  "
                  f"[{r.batches} 批 {r.elapsed:.1f}s]"
                  + ("" if r.ok else f"  ✗ {r.error}"))
    if res.errors:
        print(f"\n校验未通过 {len(res.errors)} 项：")
        for e in res.errors:
            print(f"   ✗ {e}")
        return 1
    print("\n校验全部通过 ✓")
    return 0


def cmd_report(args) -> int:
    """从已产出的 CSV 里按规则文档打印一份看板取数结果。"""
    st = load_settings()
    _setup_log(args.log_level or st.log_level)
    d = args.output_dir or st.output_dir

    def _load(name):
        p = os.path.join(d, f"{name}.csv")
        if not os.path.exists(p):
            print(f"缺少 {p}，请先跑 run")
            sys.exit(2)
        return pd.read_csv(p, dtype={"travel_date": str})

    core, plat = _load(TABLE_CORE), _load(TABLE_PLATFORM)
    dim, content = _load(TABLE_DIMENSION), _load(TABLE_CONTENT)

    scenic = args.scenic or core["scenic_spot_code"].iloc[0]
    anchor = args.date or core[core.scenic_spot_code == scenic]["travel_date"].max()
    period = args.period

    print("=" * 72)
    print(f"[{scenic}] 锚点 {anchor}  周期「{period}」")
    print("=" * 72)

    k = kpi_cards(core, scenic, anchor, period)
    if k:
        print("\n■ 核心数值卡")
        print(f"   评论总数 {k['评论总数']['值']:>8,}   环比 {k['评论总数']['环比']:+.1%}")
        print(f"   好 评 率 {k['好评率']['值']:>8.1%}   环比 {k['好评率']['环比']:+.1%}")
        print(f"   差 评 率 {k['差评率']['值']:>8.1%}   环比 {k['差评率']['环比']:+.1%}")

    s = overall_score(core, scenic, anchor, period)
    if s:
        print("\n■ 游客总体评分")
        print(f"   周期得分 {s['周期得分']}   上期 {s['上期得分']}   "
              f"环比 {'—' if s['环比'] is None else format(s['环比'], '+.1%')}")
        tops = top_dimensions(dim, scenic, anchor, period)
        print("   维度好评 TOP3: " + "  ".join(f"{n}={v}" for n, v in tops))

    t = trend_stacked(core, scenic, anchor, period).tail(5)
    if not t.empty:
        print("\n■ 评论量趋势图（末 5 天）")
        for _, r in t.iterrows():
            print(f"   {r.travel_date}  正面 {int(r.正面):>6,}  中性 {int(r.中性):>6,}  "
                  f"负面 {int(r.负面):>6,}")

    dt = dimension_trend(dim, scenic, anchor, period)
    if not dt.empty:
        print("\n■ 维度评分（末日快照）")
        for kk, vv in dt.iloc[-1].sort_values(ascending=False).items():
            print(f"   {kk:<8} {vv:.4f}")

    p = platform_distribution(plat, scenic, anchor, period)
    if not p.empty:
        print("\n■ 平台评论分布")
        for _, r in p.iterrows():
            print(f"   {str(r.platform_name):<6} 评论 {int(r.评论总量):>8,}  "
                  f"占比 {r.评论占比:>6.1%}  好评占比 {r.好评占比:>6.1%}  "
                  f"环比 {r.好评环比:+.1%}")

    print("\n■ 关键词云 TOP8")
    for emo, lbl in (("positive", "正面"), ("neutral", "中性"), ("negative", "负面")):
        w = wordcloud(content, scenic, anchor, period, emo, 8)
        if not w.empty:
            print(f"   {lbl}: " + " ".join(f"{r.emotion_word}({int(r.出现次数)})"
                                           for _, r in w.iterrows()))

    sg = negative_surge(content, scenic, anchor, period)
    if not sg.empty:
        print("\n■ 负面突增词 TOP10（较上一周期）")
        for _, r in sg.iterrows():
            rate = "新增" if r.突增率 == float("inf") else f"{r.突增率:+.0%}"
            print(f"   {r.emotion_word:<14} 本期 {int(r.本期):>7,}  上期 {int(r.上期):>7,}  "
                  f"突增 {int(r.突增量):>+7,}  ({rate})")
    return 0


def cmd_repair(args) -> int:
    """按新口径重刷历史分数（只改 emotional_score / dimension_N_score）。"""
    st = load_settings()
    _setup_log(args.log_level or st.log_level)
    scenic = [x.strip() for x in args.scenic.split(",")] if args.scenic else None
    tables = [t.strip() for t in (args.tables or "core,dimension").split(",") if t.strip()]
    formula = args.formula or st.score_formula

    print("=" * 72)
    print(f"历史分数修正  {args.start_date} ~ {args.end_date}  口径={formula}"
          + ("   [dry-run 只看不改]" if args.dry_run else ""))
    print(f"只更新：core {len(CORE_SCORE_FIELDS)} 列 / dimension {len(DIM_SCORE_FIELDS)} 列，"
          f"其余字段一律不动")
    print("=" * 72)

    db = MySQL(st.db)
    results = []
    try:
        if "core" in tables:
            results.append(repair_core(db, args.start_date, args.end_date, scenic,
                                       formula=formula, dry_run=args.dry_run,
                                       batch_size=st.batch_size, settings=st))
        if "dimension" in tables:
            # 维度表回源重算：整窗前移的回补在维度粒度上自己做，不依赖 core
            results.append(repair_dimension(
                db, args.start_date, args.end_date, scenic, formula=formula,
                dry_run=args.dry_run, batch_size=st.batch_size,
                lookback_days=st.lookback_days,
                unknown_dimension_policy=st.unknown_dimension_policy,
                settings=st))
    finally:
        db.close()

    for r in results:
        print(f"\n■ {r.table}")
        print(f"   区间内 {r.scanned:,} 行，其中 {r.changed:,} 行分数有变化，"
              f"最大变化 {r.max_delta:.4f} 分")
        print(f"   实际写回 {r.updated:,} 行" + ("（dry-run 未写库）" if args.dry_run else ""))
        if r.note:
            print(f"   注：{r.note}")
        if not r.preview.empty and args.show_preview:
            # 新旧值必须成对显示 —— 只打 _old 等于什么都没说
            n_field = 3 if len(r.fields) > 3 else len(r.fields)
            view = r.preview[r.keys].copy()
            for fld in r.fields[:n_field]:
                old, new = r.preview[f"{fld}_old"].astype(float), r.preview[f"{fld}_new"].astype(float)
                view[f"{fld}|旧"] = old.round(4)
                view[f"{fld}|新"] = new.round(4)
                view[f"{fld}|Δ"] = (new - old).round(4)
            print(f"   前 10 行新旧对比（只列前 {n_field} 个字段，共 {len(r.fields)} 个）：")
            print(view.head(10).to_string(index=False))
    return 0


# 表名简写 → 完整表名。`--push core,platform` 比敲一长串好使。
TABLE_ALIAS = {
    "core": TABLE_CORE,
    "platform": TABLE_PLATFORM,
    "dimension": TABLE_DIMENSION,
    "dim": TABLE_DIMENSION,
    "content": TABLE_CONTENT,
    "word": TABLE_CONTENT,
    "drill": TABLE_DRILL_ANALYSIS,
}


def _resolve_tables(text: str) -> List[str]:
    """解析表名清单，认简写也认全名。"all" / 空 → None（= 全部）。

    入参形如 "core,platform" 或 "all" 或完整表名
    出参：完整表名列表；None 表示不限制
    """
    text = (text or "").strip()
    if not text or text.lower() == "all":
        return None
    out, bad = [], []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        full = TABLE_ALIAS.get(part.lower(), part)
        if full in TABLE_ALIAS.values():
            if full not in out:
                out.append(full)
        else:
            bad.append(part)
    if bad:
        raise SystemExit(
            f"不认识的表名：{', '.join(bad)}\n"
            f"可用简写：{', '.join(sorted(set(TABLE_ALIAS)))}，或写完整表名，或用 all 表示全部")
    return out or None


def _apply_push_args(st, args) -> None:
    """把命令行的推送开关盖到配置上。--no-push 优先于 --push。"""
    push = getattr(args, "push", None)
    if push is not None:
        # --push 不带值 → const="all"；--push core,platform → 只推这几张
        st.push_enabled = True
        st.push_tables = _resolve_tables(push)
    if getattr(args, "no_push", False):
        st.push_enabled = False
    if getattr(args, "push_url", None):
        st.push_url = args.push_url
        st.push_enabled = True
    if getattr(args, "push_dry_run", False):
        st.push_dry_run = True
        st.push_enabled = True
    if getattr(args, "push_strict", False):
        st.push_strict = True


def cmd_push(args) -> int:
    """把已产出的 CSV 推给下游 —— 跑批当时推失败了用它补推，不必重算。"""
    from .pusher import DEFAULT_PUSH_PK, push_all

    st = load_settings()
    _apply_push_args(st, args)
    st.push_enabled = True
    _setup_log(args.log_level or st.log_level)
    if not st.push_url:
        print("未配置推送地址：在 settings_local.py 里填 push_url，"
              "或用 --push-url 传入")
        return 2

    d = args.output_dir or st.output_dir
    names = _resolve_tables(args.tables) or list(DEFAULT_PUSH_PK)
    tables = {}
    for name in names:
        path = os.path.join(d, f"{name}.csv")
        if not os.path.exists(path):
            continue
        tables[name] = pd.read_csv(path, dtype={"travel_date": str,
                                                "publish_time": str})
    if not tables:
        print(f"{d} 下没有可推送的 CSV（找的是 {', '.join(names)}）")
        return 2

    print("=" * 72)
    print(f"补推 {len(tables)} 张表 → {st.push_url}"
          + ("   [dry-run 只组报文不发送]" if st.push_dry_run else ""))
    print("=" * 72)
    rep = push_all(tables, settings=st)
    for r in rep.results:
        print(f"  {r.table:<52} {r.rows:>8,} 行  [{r.batches} 批 {r.elapsed:.1f}s]"
              + ("" if r.ok else f"  ✗ {r.error}"))
    if not rep.ok:
        print(f"\n推送未全部成功：{len(rep.errors)} 项")
        return 1
    print("\n推送完成 ✓")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="engin_cli", description="舆情指标引擎：源表 → 四张 ADS 指标表 + 下钻明细表")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="跑批：源表 → 四张 ADS 表")
    r.add_argument("--date", help="单日 yyyyMMdd")
    r.add_argument("--start-date", help="区间起 yyyyMMdd")
    r.add_argument("--end-date", help="区间止 yyyyMMdd")
    r.add_argument("--scenic", help="景区编码，逗号分隔；不填=全部")
    r.add_argument("--lookback-days", type=int, help="取数回溯天数，默认 180（近半年）")
    r.add_argument("--output-dir", help="CSV 输出目录")
    r.add_argument("--skip-db", action="store_true", help="不写库")
    r.add_argument("--skip-csv", action="store_true", help="不出 CSV")
    r.add_argument("--dry-run", action="store_true", help="连库但不真正写入")
    r.add_argument("--no-backfill", action="store_true",
                   help="关闭缺数回补（backfill_mode=off），当期没数据就是 0")
    r.add_argument("--backfill-lookback",
                   help="覆盖各窗口最大前移天数，形如 1:5,7:10,30:20；只写要改的档")
    r.add_argument("--no-drill-analysis", action="store_true", help="不产出 5.2 下钻明细表")
    r.add_argument("--push", nargs="?", const="all", metavar="表名",
                   help="跑完推送下游。不带值=全部表；也可以只推其中几张，"
                        "如 --push core,platform（简写见下）")
    r.add_argument("--no-push", action="store_true", help="本次不推送")
    r.add_argument("--push-url", help="推送地址，覆盖配置")
    r.add_argument("--push-dry-run", action="store_true", help="只组报文不真发，验证配置用")
    r.add_argument("--push-strict", action="store_true", help="推送失败也让退出码非 0")
    r.add_argument("--comments-csv", help="离线源：评论表 CSV")
    r.add_argument("--works-csv", help="离线源：作品表 CSV")
    r.add_argument("--log-level", help="DEBUG/INFO/WARNING")
    r.set_defaults(func=cmd_run)

    p = sub.add_parser("report", help="按规则文档打印看板取数结果")
    p.add_argument("--scenic")
    p.add_argument("--date")
    p.add_argument("--period", default="today", choices=["today", "week", "month"])
    p.add_argument("--output-dir")
    p.add_argument("--log-level")
    p.set_defaults(func=cmd_report)

    u = sub.add_parser("push", help="把已产出的 CSV 推给下游（补推用，不重算）")
    u.add_argument("--tables", metavar="表名",
                   help="表名，逗号分隔；不填或 all=全部。"
                        "简写：core/platform/dimension/content/drill")
    u.add_argument("--output-dir", help="CSV 所在目录")
    u.add_argument("--push-url", help="推送地址，覆盖配置")
    u.add_argument("--push-dry-run", action="store_true", help="只组报文不真发")
    u.add_argument("--log-level")
    u.set_defaults(func=cmd_push)

    x = sub.add_parser("repair", help="按新口径重刷历史分数（只改得分字段）")
    x.add_argument("--start-date", required=True, help="修正区间起 yyyyMMdd")
    x.add_argument("--end-date", required=True, help="修正区间止 yyyyMMdd")
    x.add_argument("--scenic", help="景区编码，逗号分隔；不填=全部")
    x.add_argument("--tables", default="core,dimension",
                   help="要修的表：core / dimension，逗号分隔")
    x.add_argument("--formula", choices=["weighted_v2", "confidence_v1"],
                   help="目标口径，默认取 settings.score_formula")
    x.add_argument("--dry-run", action="store_true", help="只比对不写库")
    x.add_argument("--show-preview", action="store_true", help="打印前 10 行新旧对比")
    x.add_argument("--log-level")
    x.set_defaults(func=cmd_repair)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
