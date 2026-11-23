# -*- coding: utf-8 -*-
"""造演示数据：把一个景区在库里的结果整份复制给另一个景区编号，再推给下游。

用途：下游要一个新景区的样子看效果，又还没有它的评论。拿现成景区（如九嶷山 PFTSCA01016184）
一段日期的结果，景区编号 / 名称换成新的，其余原样，走正常的推送接口推过去。

    python -m engin_cli.cli mock --from-scenic PFTSCA01016184 \\
        --to-scenic PFTSCA09999999 --to-name 演示景区 \\
        --start-date 20260701 --end-date 20261019 --tables drill,macro

几个决定
--------
· **只读本地 ADS 库、只推送，不写库**：演示数据不混进本地结果表，删起来也只用找下游。
· **只取引擎自己产出的列**（各表 COLUMNS 与库表列的交集）：客户在表上加的列
  （handle_flag / created_time / 自增 id …）不推，跟跑批直推的报文一致。
· **下钻表的 detail_uk 按新景区重算**：它是 md5(景区+词+渠道+作品+评论+日期)，
  不重算的话两个景区的行 pkId 相同，下游 upsert 会把原景区的数据改成新景区。
· **新旧编号相同直接拒绝**：那等于拿演示名称覆盖真实景区的数据。
· **关键字里的源景区名换成新名称**：词云、情感词、下钻片段里常带景区名（「长白山好看」），
  原样复制过去一眼就露馅。默认把源景区名称（库里的 scenic_name，连同去掉「景区 / 风景区…」
  后缀的叫法）换成新名称：长白山好看 → A级景区好看。--replace-words 再加别的词（如 天池、长白山北坡），
  也都换成新名称，一段文字里有几个都一起换；--no-replace-words 关掉。
  只改 KEYWORD_COLUMNS 里的列，指标数值不动。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence

import pandas as pd

from . import metric_calc_domain as D
from .drill_analysis import COLUMNS as DRILL_COLUMNS, _uk
from .loader import (TABLE_CONTENT, TABLE_CORE, TABLE_DIMENSION,
                     TABLE_DRILL_ANALYSIS, TABLE_MACRO, TABLE_PLATFORM, scenic_key)

log = logging.getLogger(__name__)

# 每张表推送哪些列 —— 与跑批直推的 DataFrame 一致
TABLE_COLUMNS: Dict[str, List[str]] = {
    TABLE_CORE: list(D.CORE_COLUMNS),
    TABLE_PLATFORM: list(D.PLATFORM_COLUMNS),
    TABLE_DIMENSION: list(D.DIMENSION_COLUMNS),
    TABLE_CONTENT: list(D.CONTENT_COLUMNS),
    TABLE_DRILL_ANALYSIS: list(DRILL_COLUMNS),
    TABLE_MACRO: list(D.MACRO_COLUMNS),
}

# 景区名称列：跟景区编号列一样，下钻表 / 大盘表叫 scenic_name，其余四张叫 scenic_spot_name
SCENIC_NAME_COL = {TABLE_DRILL_ANALYSIS: "scenic_name", TABLE_MACRO: "scenic_name"}


def scenic_name_col(table: str) -> str:
    return SCENIC_NAME_COL.get(table, "scenic_spot_name")


# ══════════════════════════════════════════════════════════════════
# 关键字替换：源景区名 → 新景区名
# ══════════════════════════════════════════════════════════════════
# 关键字类的列：普通文本列直接替换；JSON 列（大盘表词云）只换里面的 word
KEYWORD_COLUMNS: Dict[str, List[str]] = {
    TABLE_CONTENT: ["emotion_word"],
    TABLE_DRILL_ANALYSIS: ["emotion_word", "content_snippet"],
}
KEYWORD_JSON_COLUMNS: Dict[str, List[str]] = {TABLE_MACRO: ["wordcloud_map"]}
# 景区名常见后缀：「长白山景区」在评论里多半只说「长白山」，两种都要换
NAME_SUFFIXES = ("国家级风景名胜区", "风景名胜区", "生态旅游区", "旅游度假区", "旅游景区", "风景区",
                 "旅游区", "度假区", "景区", "景点")


def default_replace_words(names: Sequence[str]) -> List[str]:
    """源景区名称 → 要替换的词：名称本身 + 去掉常见后缀的叫法（剩下不到 2 个字的不要，免得误伤）。"""
    out: List[str] = []
    for n in names:
        n = str(n or "").strip()
        if len(n) < 2:
            continue
        cands = [n]
        for suf in NAME_SUFFIXES:
            if n.endswith(suf) and len(n) - len(suf) >= 2:
                cands.append(n[:-len(suf)])
                break
        out += [c for c in cands if c not in out]
    return out


_WORD_SEP = re.compile(r"[,，]")


def parse_words(text) -> List[str]:
    """命令行 / 页面上填的替换目标 → 词列表（去空、去重、保持顺序）。
    字符串用逗号分隔（中英文逗号都认）；也可以直接传列表。"""
    items = ([str(x) for x in text] if isinstance(text, (list, tuple))
             else _WORD_SEP.split(str(text or "")))
    out: List[str] = []
    for it in items:
        it = it.strip()
        if it and it not in out:
            out.append(it)
    return out


def make_replacer(words: Sequence[str], to_name: str) -> Optional[Callable[[str], str]]:
    """把这些词全部换成新景区名称，一段文字里有几个、有几种都一起换。

    · 一次扫描：每个位置只换一次，新名称里含旧词也不会越换越长（长白山 → 长白山演示）；
    · 长词优先：「长白山景区好看」按「长白山景区」换，不会先换掉「长白山」剩下「A级景区景区好看」；
    · 和新名称一样的词不换。
    """
    ws = sorted({w for w in words if w and w != to_name}, key=len, reverse=True)
    if not ws:
        return None
    pat = re.compile("|".join(re.escape(w) for w in ws))
    return lambda v: pat.sub(to_name, v) if isinstance(v, str) else v


def _replace_wordcloud(text, rep: Callable[[str], str]):
    """wordcloud_map：{"positiveWord":[{"word","rate"},...],...}，换 word；换完同名的词合并（rate 相加）。"""
    if not isinstance(text, str) or not text.strip():
        return text
    try:
        obj = json.loads(text)
    except ValueError:
        return rep(text)
    if not isinstance(obj, dict):
        return text
    changed = False
    for key, items in obj.items():
        if not isinstance(items, list):
            continue
        merged: Dict[str, dict] = {}
        for it in items:
            if not isinstance(it, dict) or "word" not in it:
                merged[f"\x00{len(merged)}"] = it
                continue
            w = rep(it["word"])
            changed |= w != it["word"]
            if w in merged and isinstance(merged[w].get("rate"), (int, float)) \
                    and isinstance(it.get("rate"), (int, float)):
                merged[w]["rate"] = round(merged[w]["rate"] + it["rate"], 6)
            else:
                merged[w] = dict(it, word=w)
        obj[key] = list(merged.values())
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) if changed else text


def replace_keywords(table: str, df: pd.DataFrame, rep: Optional[Callable[[str], str]]) -> tuple:
    """把这张表关键字类列里的旧名称换掉。出参：(新 DataFrame, 改了几行)。"""
    if rep is None:
        return df, 0
    out = df.copy()
    hit = pd.Series(False, index=out.index)
    for c in KEYWORD_COLUMNS.get(table, []):
        if c in out.columns:
            new = out[c].map(rep)
            hit |= (new != out[c]) & out[c].notna()
            out[c] = new
    for c in KEYWORD_JSON_COLUMNS.get(table, []):
        if c in out.columns:
            new = out[c].map(lambda v: _replace_wordcloud(v, rep))
            hit |= (new != out[c]) & out[c].notna()
            out[c] = new
    return out, int(hit.sum())


# 替换后主键可能撞车（「长白山好看」「长白山景区好看」都变成「A级景区好看」），下游按主键只会留一条
_DUP_KEYS = {TABLE_CONTENT: ["emotion_type", "emotion_word", "publish_time", "travel_date"],
             TABLE_DRILL_ANALYSIS: ["detail_uk"]}


def _dup_note(table: str, df: pd.DataFrame) -> str:
    keys = [k for k in _DUP_KEYS.get(table, []) if k in df.columns]
    if not keys:
        return ""
    d = df[df.duplicated(keys, keep=False)]
    if d.empty:
        return ""
    sample = d["emotion_word"].iloc[0] if "emotion_word" in d.columns else ""
    return (f"{table}：替换后有 {len(d):,} 行主键重复（如「{sample}」，原来是不同的词），"
            "下游按主键只会留一条；不想合并就用 --replace-words 只换更具体的词")


def drill_detail_uk(df: pd.DataFrame) -> List[str]:
    """下钻表 detail_uk，与 drill_analysis._expand 同一个公式。"""
    return [_uk(*k) for k in zip(df["scenic_id"], df["emotion_word"], df["channel"],
                                  df["work_id"], df["comment_id"], df["travel_date"])]


def retarget(table: str, df: pd.DataFrame, to_scenic: str, to_name: str,
             etl_time: Optional[datetime] = None,
             replacer: Optional[Callable[[str], str]] = None) -> pd.DataFrame:
    """把一张表的行改到新景区名下：编号、名称换掉，关键字里的旧名称换成新名称（replacer），
    下钻表重算 detail_uk（含 emotion_word，所以在替换之后算），etl_time 记成现在。"""
    out, _ = replace_keywords(table, df, replacer)
    out[scenic_key(table)] = to_scenic
    name_col = scenic_name_col(table)
    if name_col in out.columns:
        out[name_col] = to_name
    if table == TABLE_DRILL_ANALYSIS and "detail_uk" in out.columns:
        out["detail_uk"] = drill_detail_uk(out)
    if "etl_time" in out.columns:
        out["etl_time"] = pd.Timestamp(etl_time or datetime.now()).floor("s")
    return out


def _table_columns(db, table: str) -> List[str]:
    """库里这张表实际有哪些列（库表比代码旧时少几列，照样能推）。"""
    df = db.query_df(
        "select column_name from information_schema.columns "
        "where table_schema=%s and table_name=%s", (db.cfg.ads_db(), table))
    return [str(c) for c in df.iloc[:, 0].tolist()] if not df.empty else []


def fetch_ads_tables(db, tables: Sequence[str], scenics: Optional[Sequence[str]] = None,
                     start: Optional[str] = None, end: Optional[str] = None) -> tuple:
    """从本地 ADS 库读各表数据（补推 / 造演示数据 / report 都从库里取，不再读本地 CSV）。

    scenics：只读这些景区，None = 全部景区；start / end：travel_date 区间（yyyyMMdd），None = 不限。
    只取引擎自己产出的列（各表 COLUMNS 与库表列的交集），跟跑批直推的报文一致。
    出参：({表: DataFrame}, [说明])；库里没有的表、没数据的表不在结果里。
    """
    out: Dict[str, pd.DataFrame] = {}
    notes: List[str] = []
    ads = db.cfg.ads_db()
    scenics = [str(x) for x in scenics] if scenics else None
    for table in tables:
        have = _table_columns(db, table)
        if not have:
            notes.append(f"{table}：库里没有这张表，跳过")
            continue
        want = TABLE_COLUMNS[table]
        cols = [c for c in want if c in have]
        missing = [c for c in want if c not in have]
        if missing:
            notes.append(f"{table}：库表缺 {len(missing)} 列，不推这几列：{', '.join(missing)}")
        full = f"`{ads}`.`{table}`" if ads else f"`{table}`"
        where, params = [], []
        if scenics:
            where.append(f"`{scenic_key(table)}` in ({','.join(['%s'] * len(scenics))})")
            params += scenics
        if start:
            where.append("`travel_date` between %s and %s")
            params += [int(start), int(end or start)]
        sql = (f"select {', '.join(f'`{c}`' for c in cols)} from {full}"
               + (" where " + " and ".join(where) if where else ""))
        df = db.query_df(sql, params or None)
        if df.empty:
            rng = f"{start}~{end or start}" if start else "全部日期"
            notes.append(f"{table}：{'、'.join(scenics) if scenics else '全部景区'} 在 {rng} 没有数据")
            continue
        df["travel_date"] = pd.to_numeric(df["travel_date"]).astype("int64")
        out[table] = df.sort_values("travel_date", kind="stable").reset_index(drop=True)
    return out, notes


def fetch_scenic_tables(db, from_scenic: str, start: str, end: str,
                        tables: Sequence[str]) -> tuple:
    """从本地 ADS 库读一个景区一段日期的各表数据。出参同 fetch_ads_tables。"""
    return fetch_ads_tables(db, tables, [from_scenic], start, end)


def build_mock(db, from_scenic: str, to_scenic: str, to_name: str,
               start: str, end: str, tables: Sequence[str],
               replace_words=None, replace: bool = True) -> tuple:
    """读出来 + 换到新景区。出参：({表: DataFrame}, [说明])。

    关键字替换：源景区名称（default_replace_words）+ replace_words 里的词，全部换成 to_name。
    replace_words：额外的替换目标，列表或逗号分隔的字符串（parse_words）。
    replace=False：关键字原样复制（旧行为）。
    """
    if str(from_scenic).strip() == str(to_scenic).strip():
        raise ValueError(f"新景区编号和原景区一样（{to_scenic}）：这样会用演示数据覆盖真实景区，"
                         "请换一个新编号")
    src, notes = fetch_scenic_tables(db, from_scenic, start, end, tables)
    now = datetime.now()
    rep, words = None, []
    if replace:
        names = []
        for t, df in src.items():
            col = scenic_name_col(t)
            if col in df.columns:
                names += [str(x) for x in df[col].dropna().unique() if str(x).strip()]
        words = default_replace_words(sorted(set(names), key=names.index))
        words += [w for w in parse_words(replace_words) if w not in words]
        words = [w for w in words if w != to_name]
        rep = make_replacer(words, to_name)
    out, counts = {}, []
    for t, df in src.items():
        df, n = replace_keywords(t, df, rep)
        if n:
            counts.append(f"{t} {n:,} 行")
        out[t] = retarget(t, df, to_scenic, to_name, now)
        note = _dup_note(t, out[t]) if rep is not None else ""
        if note:
            notes.append(note)
    if not replace:
        notes.append("关键字替换：已关闭，关键字原样复制")
    elif rep is None:
        notes.append("关键字替换：没有可替换的词（源景区没有名称，也没指定 --replace-words）")
    else:
        notes.append(f"关键字替换：{'、'.join(words)} → {to_name}："
                     + ("、".join(counts) if counts else "这段日期的关键字里没有出现"))
    return out, notes
