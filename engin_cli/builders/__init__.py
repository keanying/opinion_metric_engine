# -*- coding: utf-8 -*-
"""四张 ADS 表的计算 builder。

每个 builder 的签名统一为 build(facts..., ctx) -> pd.DataFrame，
返回的列名与列序**严格等于**目标表 DDL（除自增 id），loader 直接按列名写入。
"""

from .core import build_core
from .platform import build_platform
from .dimension import build_dimension
from .content import build_content

__all__ = ["build_core", "build_platform", "build_dimension", "build_content"]
