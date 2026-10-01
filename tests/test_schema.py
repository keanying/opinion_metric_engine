# -*- coding: utf-8 -*-
"""表结构一致性：metric_calc_domain 的列清单必须与目标表 DDL 逐列相同。

列名对不上时 pymysql 会在 executemany 那一刻才报 Unknown column，
一次跑批要跑几分钟才炸；这个测试 0.1 秒就能发现。

builder 的 COLUMNS 直接引用 domain 的清单，所以断言 domain 就等于断言了 builder
（下面 test_builders_reuse_domain_columns 盯住这个引用关系）。
"""

import re
from pathlib import Path

import pytest

from engin_cli.metric_calc_domain import (CONTENT_COLUMNS as CONTENT_COLS,
                                          CORE_COLUMNS as CORE_COLS,
                                          DIMENSION_COLUMNS as DIM_COLS,
                                          PLATFORM_COLUMNS as PLAT_COLS)
from engin_cli.drill_analysis import COLUMNS as DRILL_COLS

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
DDL_PATH = SQL_DIR / "ads_ddl_reference.sql"
# 下钻表的建表语句单独一个文件 —— 之前没被这个测试覆盖，
# 结果改字段名（scenic_spot_code → scenic_id）时它是唯一没有护栏的一张表。
DRILL_DDL_PATH = SQL_DIR / "ads_drill_analysis_ddl.sql"

_COL_RE = re.compile(
    r"^([a-z_0-9]+)\s+(bigint|varchar|int|decimal|datetime|text|longtext|timestamp|char)")


def _ddl_columns(path: Path = None):
    sql = (path or DDL_PATH).read_text(encoding="utf-8")
    out = {}
    # `if not exists` 是可选的 —— 下钻表的建表语句带这段，不兜住就整张表漏检
    for name, body in re.findall(
            r"create table (?:if not exists\s+)?(\w+)\s*\((.*?)\n\)", sql, re.S):
        cols = []
        for line in body.split("\n"):
            m = _COL_RE.match(line.strip())
            if m and m.group(1) != "id":
                cols.append(m.group(1))
        out[name] = cols
    return out


@pytest.fixture(scope="module")
def ddl():
    return {**_ddl_columns(), **_ddl_columns(DRILL_DDL_PATH)}


@pytest.mark.parametrize("table,builder_cols", [
    ("ads_trf_social_opinion_comment_core_di", CORE_COLS),
    ("ads_trf_social_opinion_comment_platform_di", PLAT_COLS),
    ("ads_trf_social_opinion_comment_dimension_score_di", DIM_COLS),
    ("ads_trf_social_opinion_comment_content_di", CONTENT_COLS),
    ("ads_trf_social_opinion_drill_analysis_di", DRILL_COLS),
])
def test_builder_columns_match_ddl(ddl, table, builder_cols):
    expected = ddl[table]
    missing = [c for c in expected if c not in builder_cols]
    extra = [c for c in builder_cols if c not in expected]
    assert not missing, f"{table} 缺少列：{missing}"
    assert not extra, f"{table} 多出列：{extra}"
    assert len(builder_cols) == len(expected)


def test_no_duplicate_columns():
    for cols in (CORE_COLS, PLAT_COLS, DIM_COLS, CONTENT_COLS):
        assert len(cols) == len(set(cols))


def test_builders_reuse_domain_columns():
    """builder 不许自己维护一份列清单 —— 必须引用 domain 的同一个对象。"""
    from engin_cli.builders import content, core, dimension, platform
    assert core.COLUMNS is CORE_COLS
    assert platform.COLUMNS is PLAT_COLS
    assert dimension.COLUMNS is DIM_COLS
    assert content.COLUMNS is CONTENT_COLS


# ══════════════════════════════════════════════════════════════════
# 下钻表的景区列跟另外四张不一样，别改串了
# ══════════════════════════════════════════════════════════════════
def test_drill_uses_scenic_id_not_scenic_spot_code():
    """下钻表用 scenic_id / scenic_name（与源表同名），其余四张用 scenic_spot_code。

    这两套命名混用过一次，所以钉死：下钻表里不许出现 scenic_spot_*，
    另外四张里不许出现裸的 scenic_id。
    """
    assert DRILL_COLS[0] == "scenic_id"
    assert "scenic_name" in DRILL_COLS
    assert not [c for c in DRILL_COLS if c.startswith("scenic_spot_")]

    for cols in (CORE_COLS, PLAT_COLS, DIM_COLS, CONTENT_COLS):
        assert "scenic_spot_code" in cols
        assert "scenic_id" not in cols


def test_loader_knows_each_table_s_scenic_column():
    """delete+insert 的 WHERE 用的列名要按表取，写死一个名字会报 Unknown column。"""
    from engin_cli import loader as L
    assert L.scenic_key(L.TABLE_DRILL_ANALYSIS) == "scenic_id"
    for t in (L.TABLE_CORE, L.TABLE_PLATFORM, L.TABLE_DIMENSION, L.TABLE_CONTENT):
        assert L.scenic_key(t) == "scenic_spot_code"
