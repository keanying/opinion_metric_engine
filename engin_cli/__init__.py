# -*- coding: utf-8 -*-
"""opinion_metric_engine · 舆情指标引擎

景区社媒舆情指标计算：src → ADS，全内存，无中间表。
"""

__version__ = "1.9.2"

from .settings import EtlSettings, load_settings   # noqa: F401
from .context import RunContext                     # noqa: F401

__all__ = ["EtlSettings", "load_settings", "RunContext", "__version__"]
