# -*- coding: utf-8 -*-
"""测试用的假 ADS 库：只认 mock.fetch_ads_tables / report 发的几种 SQL。

结果只写库、不再出本地 CSV，补推 / 造演示数据 / report 都从库里读，测试里用它代替 MySQL：
· 查列名（information_schema）
· 按景区（`键` in (...)，可选）+ travel_date 区间（between，可选）取数
· report 找最近有数据的景区（order by `travel_date` desc limit 1）
"""

import re
from datetime import datetime

import pandas as pd


class FakeAdsDB:
    class cfg:                                   # noqa: N801
        @staticmethod
        def ads_db():
            return "ads"

    EXTRA = ["id", "handle_flag", "created_time"]

    def __init__(self, tables, extra_cols=True):
        """tables：{表名: DataFrame}；extra_cols=True 时补上客户表上才有的列，检查它们不会被推出去。"""
        self.tables = {}
        for t, df in tables.items():
            df = df.reset_index(drop=True)
            if extra_cols:
                df = df.assign(id=range(len(df)), handle_flag=0, created_time=datetime(2026, 10, 1))
            self.tables[t] = df
        self.queries = []

    def query_df(self, sql, params=()):
        self.queries.append((sql, params))
        params = list(params or [])
        if "information_schema" in sql:
            t = params[1]
            cols = list(self.tables[t].columns) if t in self.tables else []
            return pd.DataFrame({"column_name": cols})
        t = re.search(r"from `ads`\.`(\w+)`", sql).group(1)
        cols = re.findall(r"`(\w+)`", sql.split(" from ")[0])
        df = self.tables[t]
        m = re.search(r"`(\w+)` in \(([^)]*)\)", sql)
        if m:
            n = m.group(2).count("%s")
            df = df[df[m.group(1)].astype(str).isin([str(x) for x in params[:n]])]
            params = params[n:]
        if "between" in sql:
            lo, hi = params[:2]
            df = df[df["travel_date"].astype(int).between(int(lo), int(hi))]
        if "order by `travel_date` desc limit 1" in sql:
            df = df.sort_values("travel_date", ascending=False).head(1)
        return df[cols].reset_index(drop=True)

    def close(self):
        pass
