# -*- coding: utf-8 -*-
"""演示景区数据生成：给一个没有真实评论的景区（默认 PFTSCM00000001 A级景区）凭空造评论，
再走**正常的跑批计算**产出各张 ADS 表，写库 / 推送。

    python -m engin_cli.cli mock-gen --start-date 20260101 --end-date 20261122
    python -m engin_cli.cli mock-gen --start-date 20261101 --end-date 20261122 \\
        --tables drill,macro --push drill,macro

几个决定
--------
· **造的是源评论，不是直接造结果**：评论在内存里生成，交给 pipeline 算，指标口径、
  窗口、环比、补数、数据质量检测全部和真实景区一样，表与表之间天然对得上；
  主逻辑一行不改，造数只是换了一个「源」。
· **每天的数据由「景区 + 日期」决定**：同一天不管哪次生成、和哪段区间一起生成，
  评论都一模一样；所以分几次生成、重复生成同一段日期，结果都一致（只有 etl_time 不同），
  窗口指标也接得上。每天的评论量、好中差比例、渠道分布、维度、关键词都随日期变化
  （周末 / 节假日多，情感有缓慢起伏 + 每天的随机波动）。
· **不碰源表**：生成的评论只在内存里，不写 src_opinion_social_work_comment_di。
· **防止覆盖真实景区**：写库是按「景区 + 日期」先删后插，所以景区编号在源表里有真实评论时拒绝执行。
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

MOCK_SCENIC = "PFTSCM00000001"
MOCK_NAME = "A级景区"
# 默认生成的表：全部 6 张（下钻表、大盘表、四张 ADS 指标表）
DEFAULT_TABLES = ("drill", "macro", "core", "platform", "dimension", "content")

CHANNELS = ["douyin", "xiaohongshu", "weibo", "ctrip", "kuaishou", "tongcheng"]
CHANNEL_WEIGHTS = [26, 22, 16, 16, 12, 8]

# (一级, 二级, 三级, 该维度偏向：>0 偏好评，<0 偏差评)
DIMS = [
    ("游玩体验", "景色观赏", "自然风光", 0.15), ("游玩体验", "景色观赏", "人文景观", 0.1),
    ("游玩体验", "游玩项目", "演艺表演", 0.05), ("游玩体验", "排队时长", "", -0.2),
    ("游玩体验", "性价比", "门票价格", -0.1),
    ("服务质量", "工作人员", "态度", 0.05), ("服务质量", "导游服务", "讲解水平", 0.1),
    ("服务质量", "售票服务", "线上购票", 0.0),
    ("设施环境", "基础设施", "厕所", -0.1), ("设施环境", "环境卫生", "整体清洁", 0.05),
    ("设施环境", "休憩设施", "座椅", 0.0),
    ("餐饮购物", "餐饮", "价格", -0.15), ("餐饮购物", "餐饮", "口味", 0.0),
    ("餐饮购物", "购物", "纪念品", -0.05),
    ("安全秩序", "秩序维护", "人流疏导", -0.05), ("安全秩序", "人身安全", "防护措施", 0.05),
    ("交通接驳", "外部交通", "停车场", -0.15), ("交通接驳", "内部交通", "观光车", 0.0),
]
DIM_WEIGHTS = [16, 10, 6, 8, 6, 6, 5, 4, 4, 5, 2, 4, 3, 3, 3, 2, 5, 4]

POS_WORDS = ["风景优美", "心旷神怡", "值得一去", "名不虚传", "服务热情", "干净整洁", "拍照好看",
             "空气清新", "讲解专业", "流连忘返", "性价比高", "交通方便", "适合遛娃", "值得再来"]
NEU_WORDS = ["人比较多", "周末", "自驾", "亲子游", "节假日", "一日游", "跟团", "坐观光车"]
NEG_WORDS = ["排队太久", "门票贵", "停车难", "厕所少", "吃饭贵", "人太挤", "指示不清", "服务冷淡"]
OPENERS = {1: ["这次来玩", "带家人来", "周末打卡", "第二次来了", "朋友推荐来的"],
           0: ["今天来了", "路过顺便看看", "跟团来的", "整体来说"],
           -1: ["有点失望", "体验一般", "说实话", "提醒一下大家"]}
LOCATIONS = ["IP属地：广东", "北京", "上海市", "四川成都", "浙江省", "江苏", "IP属地:湖北", "湖南长沙",
             "山东", "河南", "福建厦门", "陕西西安", "重庆", "安徽", "江西", "广西壮族自治区", "云南", ""]
LOCATION_WEIGHTS = [14, 8, 8, 7, 7, 7, 5, 6, 5, 5, 4, 4, 4, 3, 3, 3, 3, 4]

BASE_DAILY = 90               # 平日每天大约多少条评论
WORKS_PER_CHANNEL = 15


def _seed(*parts) -> int:
    return int(hashlib.md5("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)


def _uk(*parts) -> str:
    return hashlib.md5("\x1f".join(str(p) for p in parts).encode()).hexdigest()


def _is_holiday(d: date) -> bool:
    """大致的节假日 / 旺季：春节前后、五一、暑假、国庆。"""
    md = (d.month, d.day)
    return (d.month in (7, 8) or (1, 25) <= md <= (2, 15) or (5, 1) <= md <= (5, 5)
            or (10, 1) <= md <= (10, 7))


def day_profile(scenic: str, d: date) -> dict:
    """这一天的「盘面」：评论量、好 / 中 / 差的概率。只由景区 + 日期决定。"""
    rng = random.Random(_seed(scenic, "profile", d.isoformat()))
    k = d.toordinal()
    vol = BASE_DAILY * (1.0 + (0.4 if d.weekday() >= 5 else 0.0) + (0.6 if _is_holiday(d) else 0.0))
    vol *= 1.0 + 0.15 * math.sin(2 * math.pi * k / 29.0)           # 缓慢起伏
    vol *= rng.uniform(0.75, 1.25)                                   # 每天的随机波动
    # 情感：好评率在 0.55~0.80 之间缓慢漂移，差评率 0.06~0.18，旺季人多差评略高
    pos = 0.68 + 0.07 * math.sin(2 * math.pi * k / 41.0) + rng.uniform(-0.05, 0.05)
    neg = 0.11 + 0.03 * math.sin(2 * math.pi * k / 23.0 + 1.3) + rng.uniform(-0.03, 0.03)
    if _is_holiday(d):
        pos, neg = pos - 0.04, neg + 0.03
    pos, neg = min(max(pos, 0.5), 0.85), min(max(neg, 0.04), 0.22)
    return {"n": max(int(round(vol)), 8), "pos": pos, "neg": neg}


def gen_works(scenic: str = MOCK_SCENIC, name: str = MOCK_NAME) -> pd.DataFrame:
    """作品：每个渠道固定 WORKS_PER_CHANNEL 个（下钻表跳转用）。"""
    rows = []
    for ch in CHANNELS:
        for i in range(WORKS_PER_CHANNEL):
            wid = f"{ch}_{scenic}_{i:03d}"
            rows.append({"scenic_id": scenic, "scenic_name": name, "channel": ch, "work_id": wid,
                         "work_url": f"https://example.com/{ch}/{wid}",
                         "title": f"{name}游玩攻略 {i + 1}", "author_name": f"{ch}旅行博主{i + 1}",
                         "publish_time": "2025-01-01 10:00:00"})
    return pd.DataFrame(rows)


def gen_day(scenic: str, name: str, d: date) -> List[dict]:
    """生成一天的评论（字段同 source.COMMENT_COLS）。同一景区同一天结果固定。"""
    prof = day_profile(scenic, d)
    rng = random.Random(_seed(scenic, "comments", d.isoformat()))
    ymd = d.strftime("%Y%m%d")
    # 当天各渠道的热度也有波动
    cw = [w * rng.uniform(0.7, 1.3) for w in CHANNEL_WEIGHTS]
    out = []
    for j in range(prof["n"]):
        ch = rng.choices(CHANNELS, weights=cw)[0]
        wid = f"{ch}_{scenic}_{rng.randrange(WORKS_PER_CHANNEL):03d}"
        dim = rng.choices(DIMS, weights=DIM_WEIGHTS)[0]
        pos = min(max(prof["pos"] + dim[3], 0.2), 0.95)
        neg = min(max(prof["neg"] - dim[3] / 2, 0.02), 0.6)
        r = rng.random()
        s = 1 if r < pos else (-1 if r < pos + neg else 0)
        dims = [{"dim1": dim[0], "dim2": dim[1], "dim3": dim[2], "sentiment": s}]
        if rng.random() < 0.25:                       # 一条评论提到两个维度
            d2 = rng.choices(DIMS, weights=DIM_WEIGHTS)[0]
            if d2[:3] != dim[:3]:
                dims.append({"dim1": d2[0], "dim2": d2[1], "dim3": d2[2],
                             "sentiment": rng.choice([s, s, 1, 0, -1])})
        pool = POS_WORDS if s > 0 else (NEG_WORDS if s < 0 else NEU_WORDS)
        kws = rng.sample(pool, k=rng.choice([1, 1, 2, 2, 3]))
        cid = f"m{scenic}_{ymd}_{j:04d}"
        lvl = "level_1" if j == 0 or rng.random() < 0.75 else "level_2"
        root = cid if lvl == "level_1" else f"m{scenic}_{ymd}_{rng.randrange(j):04d}"
        ts = datetime(d.year, d.month, d.day, rng.randint(7, 23), rng.randint(0, 59), rng.randint(0, 59))
        text = f"{rng.choice(OPENERS[s])}，{name}的{dim[2] or dim[1]}{'，'.join([''] + kws)}。"
        out.append({
            "scenic_id": scenic, "scenic_name": name, "channel": ch, "work_id": wid,
            "comment_level": lvl, "comment_parent_id": "" if lvl == "level_1" else root,
            "comment_id": cid, "root_comment_id": root, "commenter_name": f"游客{rng.randint(1000, 99999)}",
            "content": text, "likes": int(rng.expovariate(1 / 25)),
            "location": rng.choices(LOCATIONS, weights=LOCATION_WEIGHTS)[0],
            "sentiment_label": {1: "正向", 0: "中性", -1: "负向"}[s], "sentiment_score": s,
            "dimension_tags": json.dumps(dims, ensure_ascii=False),
            "entity_tags": json.dumps([{"type": "景区地名", "value": name}], ensure_ascii=False),
            "keyword_tags": json.dumps(kws, ensure_ascii=False),
            "publish_time": ts.strftime("%Y-%m-%d %H:%M:%S"),
        })
    return out


def gen_comments(scenic: str, name: str, ranges: Sequence[Tuple[str, str]]) -> pd.DataFrame:
    """生成若干日期段（yyyyMMdd 闭区间）的评论；段之间重叠的日期只生成一次。"""
    days = set()
    for lo, hi in ranges:
        d, e = datetime.strptime(str(lo), "%Y%m%d").date(), datetime.strptime(str(hi), "%Y%m%d").date()
        while d <= e:
            days.add(d)
            d += timedelta(days=1)
    rows: List[dict] = []
    for d in sorted(days):
        rows.extend(gen_day(scenic, name, d))
    return pd.DataFrame(rows)


def source_ranges(settings, dates: Sequence[str]) -> List[Tuple[str, str]]:
    """跑这些目标日期，pipeline 要读哪几段源数据（取数区间 + 补数 + 大盘表的上期 / 去年同期）。
    和 pipeline._compute 的取数一致，造数只造这几段，不多造。"""
    from .builders.macro import macro_data_ranges
    from .context import RunContext
    from .pipeline import compute_load_range
    load_start, load_end = compute_load_range(list(dates), settings)
    ctx = RunContext(settings=settings, output_dates=sorted(dates),
                     load_start=load_start, load_end=load_end)
    if getattr(settings, "enable_macro_metric", True):
        return macro_data_ranges(ctx)
    return [(ctx.fill_start, load_end)]


def build_source(settings, dates: Sequence[str], scenic: str = MOCK_SCENIC,
                 name: str = MOCK_NAME) -> Tuple[pd.DataFrame, pd.DataFrame, List[Tuple[str, str]]]:
    """出参：(评论, 作品, 造了哪几段日期)。"""
    ranges = source_ranges(settings, dates)
    return gen_comments(scenic, name, ranges), gen_works(scenic, name), ranges


def real_comment_count(db, scenic: str, start: str, end: str) -> int:
    """源表里这个景区在这段日期有多少条真实评论（> 0 就不能拿来造数，会覆盖真实结果）。"""
    from .source import TABLE_COMMENT, _date_bounds
    lo, hi = _date_bounds(start, end)
    src = db.cfg.src_db()
    table = f"`{src}`.`{TABLE_COMMENT}`" if src else f"`{TABLE_COMMENT}`"
    df = db.query_df(f"select count(*) n from {table} where scenic_id=%s "
                     f"and publish_time >= %s and publish_time < %s", [scenic, lo, hi])
    return int(df.iloc[0, 0] or 0) if not df.empty else 0


def date_segments(dates: Sequence) -> List[Dict[str, object]]:
    """一串 yyyyMMdd 日期 → 连续的段 [{start, end, days}]（页面上显示「已生成」用）。"""
    ds = sorted({datetime.strptime(str(x), "%Y%m%d").date() for x in dates if str(x).strip()})
    out: List[Dict[str, object]] = []
    for d in ds:
        if out and d == out[-1]["_end"] + timedelta(days=1):
            out[-1]["_end"] = d
        else:
            out.append({"_start": d, "_end": d})
    return [{"start": s["_start"].strftime("%Y%m%d"), "end": s["_end"].strftime("%Y%m%d"),
             "days": (s["_end"] - s["_start"]).days + 1} for s in out]


def missing_segments(have: Sequence, start: str, end: str) -> List[Dict[str, object]]:
    """[start, end] 里还没生成的日期，按连续段返回。"""
    got = {str(x) for x in have}
    d, e = datetime.strptime(start, "%Y%m%d").date(), datetime.strptime(end, "%Y%m%d").date()
    miss = []
    while d <= e:
        if d.strftime("%Y%m%d") not in got:
            miss.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return date_segments(miss)
