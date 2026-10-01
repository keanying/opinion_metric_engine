#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构造两张源表的样例数据，用于离线跑通与回归测试。

结构、字段名、JSON 写法**完全对齐** src_opinion_social_work_comment_di /
src_opinion_social_work_di 的建表语句与需求文档给的数据样例，
包括故意混进去的脏数据（空 keyword_tags、单引号 JSON、缺 publish_time、
sentiment_score 为空只有 label、某天某平台只有 2 条评论触发回补）。

    python scripts/make_sample_data.py --days 120 --out sample
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
from datetime import datetime, timedelta

import pandas as pd

SCENICS = [("PFTSCA01002434", "九寨沟"), ("PFT_S_00001", "故宫博物院")]
CHANNELS = ["weibo", "douyin", "kuaishou", "xiaohongshu", "ctrip", "tongcheng"]

DIMS = [
    ("游玩体验", "景色观赏", "自然风光"), ("游玩体验", "景色观赏", "人文景观"),
    ("游玩体验", "排队时长", ""), ("游玩体验", "性价比", "门票价格"),
    ("服务质量", "工作人员", "态度"), ("服务质量", "导游服务", "讲解水平"),
    ("设施环境", "基础设施", "厕所"), ("设施环境", "环境卫生", "整体清洁"),
    ("餐饮购物", "餐饮", "价格"), ("餐饮购物", "购物", "纪念品"),
    ("安全秩序", "秩序维护", "人流疏导"), ("安全秩序", "人身安全", "防护措施"),
    ("交通接驳", "外部交通", "停车场"), ("交通接驳", "内部交通", "入口安检"),
]
POS_WORDS = ["鬼斧神工", "心旷神怡", "流连忘返", "名不虚传", "身临其境", "值得再来", "凉爽"]
NEU_WORDS = ["人多", "排队", "周末", "自驾", "亲子", "小长假"]
NEG_WORDS = ["太差", "超级累", "排队久", "宰客", "脏乱", "堵车", "滞留", "泥石流"]
# 发布地址：各平台写法不统一（带「IP属地」前缀、省/市全称、只写城市、境外、空），
# 用来覆盖地域归一（domain §1.11）。按评论 ID 取，不消耗随机数 —— 加这一列不改变其余样例数据。
LOCATIONS = ["IP属地：广东", "北京", "上海市", "四川成都", "浙江省", "江苏", "IP属地:湖北",
             "", "美国", "黑龙江哈尔滨", "广西壮族自治区", "广东省深圳市", "北京市"]


def _uk(*p):
    return hashlib.md5("\x1f".join(str(x) for x in p).encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--end-date", default=None, help="yyyyMMdd，默认今天")
    ap.add_argument("--out", default="sample")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)
    end = (datetime.strptime(args.end_date, "%Y%m%d") if args.end_date
           else datetime.now().replace(hour=0, minute=0, second=0, microsecond=0))
    start = end - timedelta(days=args.days - 1)

    works, comments = [], []
    for code, name in SCENICS:
        # 作品：每个景区每个渠道 12 个
        for ch in CHANNELS:
            for i in range(12):
                wid = f"{ch}_{code}_{i:03d}"
                works.append({
                    "scenic_id": code, "scenic_name": name, "channel": ch,
                    "work_id": wid,
                    "work_url": f"https://example.com/{ch}/{wid}",
                    "author_id": f"u{i}", "author_name": f"{ch}用户{i}",
                    "title": f"{name}游记 {i}", "description": "", "label": "",
                    "image_list": "[]", "video_list": "[]",
                    "likes": random.randint(10, 5000), "collection_cnt": 0,
                    "comment_cnt": 0, "shares": 0, "location": "",
                    "publish_time": (start - timedelta(days=random.randint(0, 60))
                                     ).strftime("%Y-%m-%d %H:%M:%S"),
                    "crawl_time": end.strftime("%Y-%m-%d %H:%M:%S"),
                    "extra_content": "{}", "source_keyword": "", "task_id": "t1",
                    "work_uk": _uk(code, ch, wid),
                })

        base = 60 if code == "PFTSCA01002434" else 90
        for d in range(args.days):
            day = start + timedelta(days=d)
            # 周末更高、7~8 月更高，制造真实节律
            f = 1.0 + (0.35 if day.weekday() >= 5 else 0.0) + (0.5 if day.month in (7, 8) else 0.0)
            n = max(1, int(base * f * random.uniform(0.75, 1.25)))
            # 故意造一个「当日数据极少」的日子来触发回补
            if d % 37 == 0:
                n = 2
            for j in range(n):
                ch = random.choices(CHANNELS, weights=[18, 25, 12, 22, 15, 8])[0]
                wid = f"{ch}_{code}_{random.randint(0, 11):03d}"
                r = random.random()
                s = 1 if r < 0.62 else (0 if r < 0.80 else -1)
                dim = random.choice(DIMS)
                dims = [{"dim1": dim[0], "dim2": dim[1], "dim3": dim[2], "sentiment": s}]
                if random.random() < 0.25:      # 多维度评论
                    d2 = random.choice(DIMS)
                    dims.append({"dim1": d2[0], "dim2": d2[1], "dim3": d2[2],
                                 "sentiment": random.choice([1, 0, -1])})
                pool = POS_WORDS if s > 0 else (NEG_WORDS if s < 0 else NEU_WORDS)
                kws = random.sample(pool, k=random.randint(1, 3))

                cid = f"c{code}_{d}_{j}"
                lvl = "level_1" if random.random() < 0.7 else "level_2"
                ts = day + timedelta(hours=random.randint(0, 23),
                                     minutes=random.randint(0, 59))
                row = {
                    "scenic_id": code, "scenic_name": name, "channel": ch,
                    "work_id": wid, "comment_level": lvl,
                    "comment_parent_id": "" if lvl == "level_1" else f"c{code}_{d}_0",
                    "comment_id": cid, "commenter_id": f"u{j}",
                    "image_list": "[]", "video_list": "[]",
                    "location": LOCATIONS[int(_uk(cid)[:8], 16) % len(LOCATIONS)],
                    "content": f"{name}的{dim[1]}，{'很棒' if s > 0 else ('一般' if s == 0 else '很差')}",
                    "likes": random.randint(0, 300), "extra_content": "{}",
                    "sentiment_label": {1: "正向", 0: "中性", -1: "负向"}[s],
                    "sentiment_score": s,
                    "dimension_tags": json.dumps(dims, ensure_ascii=False),
                    "entity_tags": json.dumps(
                        [{"type": "景区地名", "value": name}], ensure_ascii=False),
                    "keyword_tags": json.dumps(kws, ensure_ascii=False),
                    "publish_time": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "crawl_time": end.strftime("%Y-%m-%d %H:%M:%S"),
                    "commenter_name": f"游客{j}",
                    "root_comment_id": cid if lvl == "level_1" else f"c{code}_{d}_0",
                    "sub_comment_count": 0, "task_id": "t1",
                    "comment_uk": _uk(code, ch, wid, cid),
                }
                # ---- 故意混入脏数据，验证清洗分支 ----
                if j % 53 == 0:
                    row["keyword_tags"] = ""                      # 空词数组
                if j % 71 == 0:
                    row["sentiment_score"] = None                 # 只有 label
                if j % 89 == 0:
                    row["dimension_tags"] = str(dims).replace('"', "'")   # 单引号 JSON
                if j % 211 == 0:
                    row["publish_time"] = None                    # 无发布时间，应被丢弃
                comments.append(row)

    os.makedirs(args.out, exist_ok=True)
    pd.DataFrame(comments).to_csv(os.path.join(args.out, "comments.csv"),
                                  index=False, encoding="utf-8")
    pd.DataFrame(works).to_csv(os.path.join(args.out, "works.csv"),
                               index=False, encoding="utf-8")
    print(f"生成 {len(comments):,} 条评论 / {len(works):,} 个作品 → {args.out}/")
    print(f"日期区间 {start:%Y%m%d} ~ {end:%Y%m%d}")


if __name__ == "__main__":
    main()
