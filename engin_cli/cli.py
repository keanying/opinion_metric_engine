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
import json
import logging
import os
import sys
import time
from dataclasses import replace
from typing import List, Optional

import pandas as pd

from .analytics import (dimension_trend, kpi_cards, negative_surge, overall_score,
                        platform_distribution, top_dimensions, trend_stacked, wordcloud)
from .db import MySQL
from .loader import (TABLE_CONTENT, TABLE_CORE, TABLE_DIMENSION,
                     TABLE_DRILL_ANALYSIS, TABLE_MACRO, TABLE_PLATFORM)
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


def _date_range(start: str, end: str) -> List[str]:
    lo = pd.to_datetime(str(start), format="%Y%m%d")
    hi = pd.to_datetime(str(end), format="%Y%m%d")
    if lo > hi:
        raise SystemExit(f"开始日期 {start} 晚于结束日期 {end}")
    return [d.strftime("%Y%m%d") for d in pd.date_range(lo, hi, freq="D")]


def _explicit_dates(args) -> Optional[List[str]]:
    """命令行写了日期就返回日期清单，一个都没写返回 None。

    下面几种写法等价，都是「只跑/只推这一天」：
        --date 20260916
        --start-date 20260916 --end-date 20260916
        --start-date 20260916            （只写开始 = 只跑那一天）
        --end-date 20260916              （只写结束 = 只跑那一天）
    """
    date = getattr(args, "date", None)
    start = getattr(args, "start_date", None)
    end = getattr(args, "end_date", None)
    if date:
        return _date_range(date, date)
    if start or end:
        return _date_range(start or end, end or start)
    return None


def _dates(args) -> List[str]:
    """run 的目标日期。什么都不写 = 昨天（当天评论还在陆续产生，跑当天会偏低）。"""
    return _explicit_dates(args) or [
        (pd.Timestamp.now().normalize() - pd.Timedelta(days=1)).strftime("%Y%m%d")]


def _parse_scenic(text: Optional[str]) -> Optional[List[str]]:
    """--scenic "A,B" → ["A", "B"]（去空格、去重、保持顺序）；不写 / all → None（= 全部景区）。"""
    text = (text or "").strip()
    if not text or text.lower() == "all":
        return None
    out = []
    for part in text.split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out or None


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
    """按景区逐个跑：每个景区算完、落库、推送，再跑下一个。

    一个景区失败不影响其他景区（--fail-fast 除外），最后打印汇总。
    CSV 写在 <output_dir>/<景区编码>/ 下，push 子命令按这个目录补推。
    """
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
    if args.no_macro:
        st.enable_macro_metric = False
    _apply_push_args(st, args)
    _setup_log(args.log_level or st.log_level)

    # 要推送却没配地址：一个景区都不跑，直接退出 —— 跑完才发现推不出去白算一场
    if st.push_enabled and not st.push_url and not st.push_dry_run:
        print("未配置推送地址：在 settings_local.py 里填 push_url，"
              "或设环境变量 OPINION_PUSH_URL，或用 --push-url 传入")
        return 2

    dates = _dates(args)
    comments_df = fetch_comments_csv(args.comments_csv) if args.comments_csv else None
    works_df = fetch_works_csv(args.works_csv) if args.works_csv else None

    db = None
    if comments_df is None or st.write_db:
        db = MySQL(st.db)

    from . import __version__
    from .pipeline import compute_load_range
    from .source import list_scenic_codes
    t0 = time.time()
    summary = []
    try:
        scenics = _parse_scenic(args.scenic)
        if scenics is None:
            # 不指定 = 取数区间内有评论的全部景区
            if comments_df is not None:
                scenics = sorted(comments_df["scenic_id"].astype(str).str.strip().unique())
            else:
                lo, hi = compute_load_range(dates, st)
                scenics = sorted(list_scenic_codes(db, lo, hi))
        if not scenics:
            print(f"取数区间内没有任何景区的评论（目标日期 {dates[0]} ~ {dates[-1]}）")
            return 1

        print("=" * 72)
        print(f"engin_cli v{__version__}   目标日期 {dates[0]} ~ {dates[-1]}   "
              f"景区 {len(scenics)} 个"
              + (f"   推送 → {st.push_url}" if st.push_enabled else ""))
        print("=" * 72)
        for i, sc in enumerate(scenics, 1):
            print(f"\n[{i}/{len(scenics)}] 景区 {sc}")
            ok, line = _run_one_scenic(st, sc, dates, db, comments_df, works_df,
                                       verbose=bool(args.verbose))
            summary.append((sc, ok, line))
            if not ok and args.fail_fast:
                print("  --fail-fast：停止，后面的景区不再跑")
                break
    finally:
        if db is not None:
            db.close()

    print("\n汇总" + f"（用时 {time.time() - t0:.1f}s）")
    for sc, ok, line in summary:
        print(f"  {sc:<24} {'✓' if ok else '✗'}  {line}")
    stopped = len(summary) < len(scenics)
    return 0 if all(ok for _, ok, _ in summary) and not stopped else 1


def _run_one_scenic(st, scenic: str, dates: List[str], db, comments_df, works_df,
                    verbose: bool = False) -> tuple:
    """跑一个景区。返回 (是否成功, 汇总行文字)。"""
    s1 = replace(st, output_dir=os.path.join(st.output_dir, scenic))
    c = w = None
    if comments_df is not None:
        c = comments_df[comments_df["scenic_id"].astype(str).str.strip() == scenic]
    if works_df is not None and "scenic_id" in works_df.columns:
        w = works_df[works_df["scenic_id"].astype(str).str.strip() == scenic]
    try:
        res = run_pipeline(s1, dates, scenic_codes=[scenic], db=db,
                           comments_df=c, works_df=w)
    except Exception as e:                      # noqa: BLE001 —— 一个景区炸了不拖累其他景区
        logging.getLogger(__name__).exception("景区 %s 跑批异常", scenic)
        print(f"  ✗ 异常：{e}")
        return False, f"异常：{e}"

    for t, df in res.tables.items():
        print(f"  {t:<52} {len(df):>9,} 行")
    if verbose:
        for n in res.notes:
            print(f"  · {n}")
    for r in res.load_results:
        print(f"  写入 {r.table:<48} {r.rows:>8,} 行  [{r.strategy}]"
              + (f"  清理 {r.deleted} 行" if r.deleted else ""))
    push_note = ""
    if res.push is not None:
        for r in res.push.results:
            if r.skipped and not r.error:
                continue
            print(f"  推送 {r.table:<48} {r.rows:>8,} 行  [{r.batches} 批 {r.elapsed:.1f}s]"
                  + ("" if r.ok else f"  ✗ {r.error}"))
        push_note = "推送 ✓" if res.push.ok else f"推送 ✗（{len(res.push.errors)} 张表失败，可用 push 补推）"
    if res.errors:
        for e in res.errors:
            print(f"  ✗ {e}")
        return False, "；".join(res.errors[:2]) + (f" 等 {len(res.errors)} 项" if len(res.errors) > 2 else "")
    print(f"  校验通过 ✓  用时 {res.elapsed:.1f}s")
    return True, push_note


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
    "macro": TABLE_MACRO,
    "kpi": TABLE_MACRO,
}


def _resolve_tables(text: str) -> Optional[List[str]]:
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


# 测试与旧代码里叫这个名字
_parse_tables = _resolve_tables


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


# 补推读 CSV 时，这些列一律按字符串读：它们进了 pkId（下游的查重键），
# 读成数字会变成 1.0 / 丢前导 0，跟跑批直推的键对不上，下游就多出重复行。
_STR_COLS = ["scenic_spot_code", "scenic_spot_name", "scenic_id", "scenic_name",
             "platform_code", "platform_name", "channel", "channel_name",
             "time_granularity", "publish_time", "emotion_word", "emotion_type",
             "dimension_level1", "dimension_level2", "dimension_level3",
             "detail_uk", "work_id", "comment_id", "root_comment_id"]


def _read_output_csv(path: str) -> pd.DataFrame:
    """读跑批产出的 CSV，类型与跑批直推时一致：
    travel_date 是整数、空串还是空串（不变成 NaN/null）、上面那些键列是字符串。"""
    df = pd.read_csv(path, dtype={c: str for c in _STR_COLS},
                     keep_default_na=False, na_values=[], encoding="utf-8-sig")
    if "travel_date" in df.columns:
        df["travel_date"] = pd.to_numeric(df["travel_date"]).astype("int64")
    return df


def _push_sources(base: str, scenics: Optional[List[str]]) -> List[tuple]:
    """找要补推的 CSV：[(景区或 None, 目录)]。

    新布局：<output_dir>/<景区>/<表>.csv（run 按景区写）。
    兼容老布局：<output_dir>/<表>.csv（老版本 run 平铺写），按景区列过滤。
    """
    known = set(TABLE_ALIAS.values())

    def _has_tables(d):
        return os.path.isdir(d) and any(f"{t}.csv" in os.listdir(d) for t in known)

    out = []
    if os.path.isdir(base):
        subs = sorted(d for d in os.listdir(base) if _has_tables(os.path.join(base, d)))
        for sc in subs:
            if scenics is None or sc in scenics:
                out.append((sc, os.path.join(base, sc)))
        if _has_tables(base):
            out.append((None, base))
    return out


def _mask_headers(headers: dict) -> dict:
    """预览时把令牌打码：只露前 6 位。"""
    out = {}
    for k, v in (headers or {}).items():
        v = str(v)
        out[k] = v[:6] + "***" if len(v) > 6 else "***"
    return out


def cmd_push(args) -> int:
    """把已产出的 CSV 推给下游 —— 跑批当时推失败了用它补推，不必重算。

    可按景区、日期、表过滤；--preview 只打印每张表第一批报文，不发送。
    """
    from .loader import scenic_key
    from .pusher import DEFAULT_PUSH_PK, _table_config, build_payload, push_all

    st = load_settings()
    _apply_push_args(st, args)
    st.push_enabled = True
    _setup_log(args.log_level or st.log_level)
    preview = bool(args.preview) or bool(args.preview_rows)
    if not st.push_url and not preview and not st.push_dry_run:
        print("未配置推送地址：在 settings_local.py 里填 push_url，"
              "或设环境变量 OPINION_PUSH_URL，或用 --push-url 传入")
        return 2

    base = args.output_dir or st.output_dir
    names = _resolve_tables(args.tables) or list(DEFAULT_PUSH_PK)
    scenics = _parse_scenic(args.scenic)
    dates = _explicit_dates(args)
    date_set = {int(d) for d in dates} if dates else None

    sources = _push_sources(base, scenics)
    batches = []                         # [(景区, {表: df})]
    for sc, d in sources:
        tables = {}
        for name in names:
            path = os.path.join(d, f"{name}.csv")
            if not os.path.exists(path):
                continue
            df = _read_output_csv(path)
            key = scenic_key(name)
            if scenics is not None and key in df.columns:
                df = df[df[key].isin(scenics)]
            if date_set is not None and "travel_date" in df.columns:
                df = df[df["travel_date"].isin(date_set)]
            if not df.empty:
                tables[name] = df.reset_index(drop=True)
        if tables:
            batches.append((sc or "（平铺目录）", tables))

    if not batches:
        what = [f"表 {', '.join(names)}"]
        if scenics:
            what.append(f"景区 {', '.join(scenics)}")
        if dates:
            what.append(f"日期 {dates[0]}~{dates[-1]}")
        print(f"{base} 下没有可推送的数据（{'，'.join(what)}）。"
              f"\n先用 run 跑出 CSV（写在 {base}/<景区编码>/ 下），再补推。")
        return 2

    if preview:
        n = int(args.preview_rows or 2)
        print("=" * 72)
        print(f"报文预览（不发送）→ {st.push_url or '（未配置 push_url）'}")
        print(f"请求头：{json.dumps(_mask_headers(st.push_headers), ensure_ascii=False)}")
        print("=" * 72)
        for sc, tables in batches:
            for name, df in tables.items():
                cfg = _table_config(name, st)
                body = build_payload(name, df.head(n), cfg["pk"],
                                     body_fields=st.push_body_fields, table_name=cfg["name"],
                                     extra=st.push_extra_fields)
                size = max(int(st.push_batch_size), 1)
                print(f"\n# {sc} · {name}：{len(df):,} 行，"
                      f"{(len(df) + size - 1) // size} 批；第一批前 {min(n, len(df))} 行：")
                print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0

    print("=" * 72)
    print(f"补推 {len(batches)} 个景区 → {st.push_url}"
          + ("   [dry-run 只组报文不发送]" if st.push_dry_run else ""))
    print("=" * 72)
    ok = True
    for sc, tables in batches:
        print(f"\n景区 {sc}")
        rep = push_all(tables, settings=st)
        for r in rep.results:
            print(f"  {r.table:<52} {r.rows:>8,} 行  [{r.batches} 批 {r.elapsed:.1f}s]"
                  + ("" if r.ok else f"  ✗ {r.error}"))
        ok &= rep.ok
    if not ok:
        print("\n推送未全部成功，见上面的 ✗")
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
    r.add_argument("--scenic", help="景区编码，逗号分隔；不填或 all = 全部景区，逐个跑、逐个推")
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
    r.add_argument("--no-macro", action="store_true",
                   help="不产出 2.0 大盘 KPI 表（也就不会为同比多取去年同期的数据）")
    r.add_argument("--push", nargs="?", const="all", metavar="表名",
                   help="跑完推送下游。不带值=全部表；也可以只推其中几张，"
                        "如 --push core,platform（简写见下）")
    r.add_argument("--no-push", action="store_true", help="本次不推送")
    r.add_argument("--push-url", help="推送地址，覆盖配置")
    r.add_argument("--push-dry-run", action="store_true", help="只组报文不真发，验证配置用")
    r.add_argument("--push-strict", action="store_true", help="推送失败也让退出码非 0")
    r.add_argument("--comments-csv", help="离线源：评论表 CSV")
    r.add_argument("--works-csv", help="离线源：作品表 CSV")
    r.add_argument("--fail-fast", action="store_true", help="某个景区失败就停，不跑后面的")
    r.add_argument("-v", "--verbose", action="store_true",
                   help="额外打印回补触发情况、补零行数量等说明")
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
                        "简写：core/platform/dimension/content/drill/macro")
    u.add_argument("--scenic", help="只推这些景区，逗号分隔；不填 = 输出目录下全部景区")
    u.add_argument("--date", help="只推这一天 yyyyMMdd")
    u.add_argument("--start-date", help="只推这段日期：起 yyyyMMdd")
    u.add_argument("--end-date", help="只推这段日期：止 yyyyMMdd（只写起或止 = 只推那一天）")
    u.add_argument("--output-dir", help="CSV 所在目录（下面按景区分子目录）")
    u.add_argument("--push-url", help="推送地址，覆盖配置")
    u.add_argument("--push-dry-run", action="store_true", help="只组报文不真发（只统计批次）")
    u.add_argument("--preview", action="store_true",
                   help="打印每张表第一批报文（令牌打码），不发送")
    u.add_argument("--preview-rows", type=int, metavar="N",
                   help="预览时每张表打印前 N 行（默认 2），隐含 --preview")
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
