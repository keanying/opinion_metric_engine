# -*- coding: utf-8 -*-
"""口径唯一性守卫。

metric_calc_domain.py 是口径的唯一真源。这个测试盯住两件事：

1. 公式函数只在 domain 里定义，别处不许再写一个同名实现；
2. 别处不许硬编码窗口字段后缀（`"_7d"` 这类），一律查 domain 的注册表。

这两条一旦破了，改口径就会漏改，而漏改在数据上是「悄悄错」——
跑批照样成功、校验照样通过，只有对账时才会发现某个字段的算法和别处不一样。
"""

import re
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "engin_cli"
DOMAIN = PKG / "metric_calc_domain.py"

# 公式函数名：只允许在 domain 里 def
FORMULA_FUNCS = ["official_score", "official_score_from_counts", "confidence",
                 "growth_rate", "ratio", "star_rating", "judge_sentiment",
                 "word_surge", "safe_div"]

# 硬编码的窗口后缀字面量，如 "_7d" / '_365d'
SUFFIX_LITERAL = re.compile(r"""['"]_\d+d['"]""")


def _other_modules():
    return [p for p in PKG.rglob("*.py")
            if p != DOMAIN and p.name != "__init__.py"]


@pytest.mark.parametrize("func", FORMULA_FUNCS)
def test_formula_defined_only_in_domain(func):
    pattern = re.compile(rf"^def {func}\(", re.M)
    assert pattern.search(DOMAIN.read_text(encoding="utf-8")), f"domain 里应定义 {func}"
    for p in _other_modules():
        assert not pattern.search(p.read_text(encoding="utf-8")), \
            f"{p.name} 重复定义了公式 {func}，口径必须只留在 metric_calc_domain.py"


def test_no_hardcoded_window_suffix_outside_domain():
    offenders = []
    for p in _other_modules():
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if SUFFIX_LITERAL.search(line):
                offenders.append(f"{p.name}:{i} {line.strip()}")
    assert not offenders, "窗口字段后缀应查 domain 注册表，不要硬编码：\n" + "\n".join(offenders)


def test_builders_do_not_compute_scores_themselves():
    """builder 里不许出现得分公式的痕迹（× 5、min(1, n/100) 之类）。"""
    bad = re.compile(r"(\*\s*5\b|min\(1[,.]|/\s*100\b|5\s*\+\s*)")
    offenders = []
    for p in (PKG / "builders").glob("*.py"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#")[0]
            if bad.search(code):
                offenders.append(f"{p.name}:{i} {line.strip()}")
    assert not offenders, "builder 只做搬运，公式请调用 domain：\n" + "\n".join(offenders)
