"""插件库：放客户特意化的结果处理逻辑，和主口径分开。

怎么加一个插件
----
在本目录新建一个 .py 文件，里面定义 PLUGIN = Plugin(...) 就行，启动时自动发现：

    from . import Plugin

    def _apply(tables, ctx):
        ...                       # 改 tables 里的 DataFrame（就地替换 tables[表名]）
        return "一句话说明改了什么"  # 写进执行日志；返回 None 不写

    PLUGIN = Plugin(name="my_plugin", label="页面上显示的名字",
                    description="悬停提示：做了什么", apply=_apply)

怎么开
----
  · 任务页「插件」里逐个打开（任务配置 plugins，按名字存）；
  · 命令行 run --plugins a,b；settings_local.py 里 "plugins": ["a"] 作为默认。
  · 都不开 = 原样输出计算结果。

什么时候执行
----
数据质量检测**之后**、落库和推送**之前**，按开启的顺序逐个执行。
检测核对的是真实计算结果，插件改过的数据不再参与检测。
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

import pandas as pd


@dataclass(frozen=True)
class Plugin:
    name: str                 # 唯一名字，任务配置 / 命令行里用它
    label: str                # 页面上显示的名字
    description: str          # 做了什么（页面悬停提示）
    # apply(tables, ctx) -> 说明文字或 None；tables = {表名: DataFrame}，就地替换
    apply: Callable[[Dict[str, pd.DataFrame], object], Optional[str]]


_REGISTRY: Optional[Dict[str, Plugin]] = None


def registry() -> Dict[str, Plugin]:
    """{名字: 插件}，按文件名排序；第一次调用时扫描本目录。"""
    global _REGISTRY
    if _REGISTRY is None:
        found: Dict[str, Plugin] = {}
        for m in sorted(pkgutil.iter_modules(__path__), key=lambda x: x.name):
            if m.name.startswith("_"):
                continue
            p = getattr(importlib.import_module(f"{__name__}.{m.name}"), "PLUGIN", None)
            if isinstance(p, Plugin):
                if p.name in found:
                    raise ValueError(f"插件名字重复：{p.name}")
                found[p.name] = p
        _REGISTRY = found
    return _REGISTRY


def parse_names(value) -> List[str]:
    """"a,b" / ["a","b"] → ["a","b"]（去重保序）；不认识的名字报错。"""
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else list(value)
    names: List[str] = []
    for x in (str(i).strip() for i in items):
        if x and x not in names:
            names.append(x)
    unknown = [n for n in names if n not in registry()]
    if unknown:
        raise ValueError(f"没有这个插件：{'、'.join(unknown)}（可用：{'、'.join(registry()) or '无'}）")
    return names


def run_plugins(names: Sequence[str], tables: Dict[str, pd.DataFrame], ctx) -> List[str]:
    """按顺序执行开启的插件，返回要写进执行日志的说明。"""
    notes: List[str] = []
    for n in parse_names(list(names)):
        p = registry()[n]
        msg = p.apply(tables, ctx)
        notes.append(f"插件「{p.label}」：{msg}" if msg else f"插件「{p.label}」已执行")
    return notes


def describe() -> List[dict]:
    """页面用：[{name, label, description}]。"""
    return [{"name": p.name, "label": p.label, "description": p.description}
            for p in registry().values()]
