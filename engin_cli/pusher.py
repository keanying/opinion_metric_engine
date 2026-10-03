# -*- coding: utf-8 -*-
"""数据推送 —— 跑批算完之后，把结果以 HTTP API 形式推给下游。

全部可配置：开关、地址、请求头、报文字段名、每张表的 tableName 与 pkId、
分批大小、超时、重试。默认**关闭**，不配就不推。

报文长这样（字段名都能改，见 settings 的 push_body_fields）：

    POST https://www.demo.com/data/sync
    Header: Authorization: Bearer sk_xxx
            Content-Type: application/json

    {
      "tableName": "ads_trf_social_opinion_comment_core_di",
      "pkId": ["scenic_spot_code", "travel_date"],
      "data": [ {...一行一个对象...} ]
    }

设计上的几个决定
--------------
· **只用标准库 urllib**，不引入 requests —— 这套引擎跑在客户的机器上，
  少一个依赖少一份装不上的风险。
· **推送失败不改变跑批的成败**：数据已经落库了，推不出去是下游的事。
  失败会计入 PushResult 并打进跑批报告，退出码由调用方决定
  （CLI 用 `--push-strict` 可以把推送失败也算作跑批失败）。
· **分批推**：一次 90 万行的 content 表塞进一个 POST 谁都受不了。
  默认 500 行一批，按 push_batch_size 配。
· **重试只针对可重试的错误**（网络异常、5xx、429），4xx 不重试 ——
  报文格式错了重试一百次也还是错。
· **日志不打请求头**：Authorization 里是密钥，落到日志文件就等于泄露。
· **HTTP 200 不等于成功**：网关把业务失败放在响应体里
  （`{"status": false, "code": 5011, "msg": "...", "trace_id": "..."}`）。
  响应体是 JSON 且带 push_success_field（默认 status）时，它为假就算失败，
  报告里带上 code / msg / trace_id；这类失败不重试（跟 4xx 一样是报文/配置问题）。
"""

from __future__ import annotations

import errno
import json
import logging
import socket
import threading
import time
import http.client
import ipaddress
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .loader import (TABLE_CONTENT, TABLE_CORE, TABLE_DIMENSION,
                     TABLE_DRILL_ANALYSIS, TABLE_MACRO, TABLE_PLATFORM)
from .metric_calc_domain import MACRO_KEYS

log = logging.getLogger(__name__)

# ---- 各表的业务主键（对齐 DDL 的唯一索引，不是自增 id）----
# 下游按这组字段做幂等 upsert，所以必须与建表语句里的 unique 一致。
# settings.push_pk 可整表覆盖。
DEFAULT_PUSH_PK: Dict[str, List[str]] = {
    TABLE_CORE: ["scenic_spot_code", "travel_date", "publish_time"],
    TABLE_PLATFORM: ["scenic_spot_code", "platform_code", "travel_date", "publish_time"],
    TABLE_DIMENSION: ["scenic_spot_code", "travel_date", "dimension_level1",
                      "dimension_level2", "dimension_level3"],
    TABLE_CONTENT: ["scenic_spot_code", "travel_date", "emotion_word"],
    # 下钻表的景区列叫 scenic_id（跟另外四张不一样），但它的幂等键是 detail_uk，
    # 所以这里不受改名影响
    TABLE_DRILL_ANALYSIS: ["detail_uk"],
    # 大盘表：景区 × 日期 × 周期粒度 × 渠道，对齐 uk_macro_gran_metric
    TABLE_MACRO: list(MACRO_KEYS),
}

# 报文字段名的默认值。下游叫 table/keys/rows 的话，在 settings 里改这三个。
DEFAULT_BODY_FIELDS = {"table": "tableName", "pk": "pkId", "data": "data"}

# 可重试的 HTTP 状态码：网关抖动和限流值得再试，4xx 是自己报文错了，重试没意义
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


@dataclass
class PushResult:
    table: str
    rows: int = 0               # 成功推送的行数
    batches: int = 0            # 成功的批次数
    failed_batches: int = 0
    skipped: bool = False       # 未配置/被开关关掉/空表
    unreachable: bool = False   # 推送地址连不上（拒绝连接 / 域名解析失败），后面的表不再推
    error: str = ""
    elapsed: float = 0.0
    # pkId 有重复 / 空值时的提示（照常发送，只是提醒），以及示例请求和完整请求文件
    warning: str = ""
    pk_sample: str = ""         # 一组重复行组成的请求 JSON（单行，可直接拿去推）
    pk_dump: str = ""           # 全部重复行组成的请求 JSON 文件路径

    @property
    def ok(self) -> bool:
        return not self.error and self.failed_batches == 0


@dataclass
class PushReport:
    results: List[PushResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def errors(self) -> List[str]:
        return [f"推送 {r.table} 失败：{r.error}" for r in self.results if not r.ok]

    def notes(self) -> List[str]:
        out = []
        for r in self.results:
            if r.skipped:
                continue
            state = "成功" if r.ok else f"失败（{r.error}）"
            out.append(f"推送 {r.table}：{r.rows:,} 行 / {r.batches} 批，"
                       f"{r.elapsed:.1f}s，{state}")
            if r.warning:
                out.append(f"⚠ {r.table}：{r.warning}")
        return out


# ══════════════════════════════════════════════════════════════════════
# 报文构造
# ══════════════════════════════════════════════════════════════════════
def _jsonable(v: Any) -> Any:
    """把 pandas/numpy 的值转成 json 能序列化的原生类型。

    这一步不能省：np.int64 / pd.Timestamp / NaN 直接扔给 json.dumps 会炸，
    NaN 就算侥幸过了也会变成非法 JSON 的裸 NaN，下游解析必挂。
    """
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        f = float(v)
        return f if np.isfinite(f) else None
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, (pd.Timestamp,)):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if v is pd.NaT or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (np.ndarray, list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def build_payload(table: str, rows: pd.DataFrame, pk: Sequence[str],
                  body_fields: Optional[Dict[str, str]] = None,
                  table_name: Optional[str] = None,
                  extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """一批数据 → 一个请求体。

    入参：table 本地表名；rows 这一批的行；pk 主键字段清单；
          body_fields 报文字段名（默认 tableName/pkId/data）；
          table_name 推给下游的表名（不填就用 table）；
          extra 额外的顶层字段（比如 batchNo、source），原样带上
    出参：dict，直接 json.dumps 就能发
    """
    f = {**DEFAULT_BODY_FIELDS, **(body_fields or {})}
    data = [{k: _jsonable(v) for k, v in rec.items()}
            for rec in rows.to_dict(orient="records")]
    body = {f["table"]: table_name or table, f["pk"]: list(pk), f["data"]: data}
    if extra:
        body.update(extra)
    return body


# ══════════════════════════════════════════════════════════════════════
# 发送
# ══════════════════════════════════════════════════════════════════════
class BusinessError(RuntimeError):
    """HTTP 2xx，但响应体说失败了（status=false）。不重试。"""


class UnreachableError(RuntimeError):
    """推送地址连不上（拒绝连接 / 域名解析失败），或者连得上但一直没有响应（每次都超时）。

    这种情况下后面每一批、每一张表都会一样失败，每批还要白等完整的超时 + 重试 ——
    表现出来就是「卡住了」。所以一旦确认，整次推送立即停止。
    """


# 超时最多只重试这么多次：对方连得上却一直不回，再等几轮 push_timeout 也大概率一样，
# 按 push_retries（默认 3 次、每次 120s）重试到底，一批就要白等 8 分钟。
TIMEOUT_RETRIES = 1


_UNREACHABLE_ERRNO = {getattr(errno, n) for n in
                      ("ECONNREFUSED", "ENETUNREACH", "EHOSTUNREACH", "EADDRNOTAVAIL")
                      if hasattr(errno, n)}


def _is_unreachable(reason: Any) -> bool:
    """URLError.reason 是不是「地址连不上」。超时不算（可能只是这一批慢）。"""
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return False
    if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
        return True
    return isinstance(reason, OSError) and getattr(reason, "errno", None) in _UNREACHABLE_ERRNO


def _reason_text(reason: Any) -> str:
    """把常见网络错误翻成人话，原文附在后面方便查。"""
    if isinstance(reason, ConnectionRefusedError):
        return f"连接被拒绝（地址上没有服务在监听）[{reason}]"
    if isinstance(reason, socket.gaierror):
        return f"域名解析失败 [{reason}]"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "超时"
    return str(reason)


# 发往本机的请求不走任何代理。
# Python 的 urllib 在 Windows 上会读「系统代理」（Clash / 公司代理等），代理没把 localhost
# 排除在外时，推到 localhost 的请求会绕到代理那里卡住 —— 而 curl / Postman 默认不走这个代理，
# 于是出现「curl 能通、engin_cli 一直超时」。本机地址走代理没有任何意义，一律直连。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _is_local_host(host: Optional[str]) -> bool:
    if not host:
        return False
    if host.lower() in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def proxy_for(url: str) -> Optional[str]:
    """engin_cli 发这个地址时实际会走的代理；None = 直连。"""
    u = urllib.parse.urlsplit(url)
    if _is_local_host(u.hostname):
        return None
    proxy = urllib.request.getproxies().get(u.scheme)
    if not proxy or urllib.request.proxy_bypass(u.hostname or ""):
        return None
    return proxy


def _encode(body: Dict[str, Any]) -> bytes:
    return json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _post(url: str, body: Dict[str, Any], headers: Dict[str, str],
          timeout: float) -> tuple:
    """发一个 POST，返回 (status, 响应文本)。只用标准库。"""
    req = urllib.request.Request(url, data=_encode(body), method="POST")
    # 与 curl 示例一致：application/json（JSON 本来就是 UTF-8，不带 charset 参数）
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, str(v))
    opener = _NO_PROXY_OPENER if _is_local_host(urllib.parse.urlsplit(url).hostname) else None
    with (opener.open(req, timeout=timeout) if opener else
          urllib.request.urlopen(req, timeout=timeout)) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


# ══════════════════════════════════════════════════════════════════════
# 推送诊断：push --diagnose
# ══════════════════════════════════════════════════════════════════════
def _try(fn, timeout: float) -> tuple:
    """跑一次发送，返回 (是否成功, 用时, 说明)。"""
    t0 = time.time()
    try:
        status, text = fn()
        ok = 200 <= status < 300
        try:
            check_response(text)
        except BusinessError as e:
            return False, time.time() - t0, f"HTTP {status}，但业务失败：{e}"
        return ok, time.time() - t0, f"HTTP {status}：{(text or '').strip()[:200]}"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200] if e.fp else ""
        return False, time.time() - t0, f"HTTP {e.code}：{body}"
    except urllib.error.URLError as e:
        return False, time.time() - t0, f"网络错误：{_reason_text(e.reason)}"
    except (socket.timeout, TimeoutError):
        return False, time.time() - t0, f"{timeout:g}s 内没有响应"
    except OSError as e:
        return False, time.time() - t0, f"网络错误：{_reason_text(e)}"


def _post_to_address(url: str, addr: str, body: Dict[str, Any], headers: Dict[str, str],
                     timeout: float, content_type: str = "application/json") -> tuple:
    """绕过域名解析和代理，直接把同一个请求发到某个 IP（Host 头仍是原来的）。只用于 http。"""
    u = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(addr, u.port or 80, timeout=timeout)
    try:
        path = u.path + (f"?{u.query}" if u.query else "")
        hdr = {"Host": u.netloc, "Content-Type": content_type, **(headers or {})}
        conn.request("POST", path or "/", body=_encode(body), headers=hdr)
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def diagnose(url: str, headers: Dict[str, str], body: Dict[str, Any],
             timeout: float = 15.0, out=print) -> Dict[str, Any]:
    """一步步排查「curl 能通、engin_cli 推送超时」：域名解析 / 代理 / 逐个地址直连 / 报文差异。

    每一步都用**同一个报文**（一般是 core 表的第一行），超时 timeout 秒。返回各步结果，供测试断言。
    """
    u = urllib.parse.urlsplit(url)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    result: Dict[str, Any] = {"addresses": {}, "actual": None, "variant": None}
    out(f"推送地址：{url}")
    out(f"测试报文：{body.get('tableName')} {len(body.get('data') or [])} 行，每步超时 {timeout:g}s\n")

    # 1. 域名解析
    try:
        addrs = list(dict.fromkeys(
            ai[4][0] for ai in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except OSError as e:
        out(f"1) 域名解析：{host} 解析失败（{e}）—— 地址写错了，或者 DNS 不通")
        return result
    out(f"1) 域名解析：{host} → {', '.join(addrs)}")

    # 2. 代理
    sys_proxy = urllib.request.getproxies().get(u.scheme)
    used = proxy_for(url)
    out(f"2) 系统代理：{sys_proxy or '无'}；engin_cli 实际：{'经代理 ' + used if used else '直连'}"
        + ("（本机地址一律直连，不走代理）" if sys_proxy and _is_local_host(host) else ""))

    # 3. 逐个地址直连（http 才做；https 需要证书校验，跳过）
    if u.scheme == "http":
        out("3) 逐个地址直连发送（不走代理）：")
        for a in addrs:
            ok, el, msg = _try(lambda a=a: _post_to_address(url, a, body, headers, timeout), timeout)
            result["addresses"][a] = ok
            out(f"     {a:<16} {'✓' if ok else '✗'} {el:5.1f}s  {msg}")
    else:
        out("3) 逐个地址直连：https 地址跳过")

    # 4. engin_cli 实际的发送方式
    ok, el, msg = _try(lambda: _post(url, body, headers, timeout), timeout)
    result["actual"] = ok
    out(f"4) engin_cli 实际发送：{'✓' if ok else '✗'} {el:.1f}s  {msg}")

    # 5. 都不通时，换成 curl 示例的写法（travel_date 用字符串）再试一次，看是不是报文的问题
    if not ok and not any(result["addresses"].values()):
        variant = dict(body)
        variant["data"] = [{k: (str(v) if k == "travel_date" and v is not None else v)
                            for k, v in row.items()} for row in body.get("data") or []]
        ok2, el2, msg2 = _try(lambda: _post(url, variant, headers, timeout), timeout)
        result["variant"] = ok2
        out(f"5) travel_date 改成字符串再发：{'✓' if ok2 else '✗'} {el2:.1f}s  {msg2}")

    # 结论
    out("")
    good = [a for a, v in result["addresses"].items() if v]
    bad = [a for a, v in result["addresses"].items() if not v]
    if result["actual"]:
        out("结论：engin_cli 的发送方式能通。之前卡住若是在推到本机地址时，原因是走了系统代理，"
            "新版本已改为本机地址直连；否则可能是对方服务偶发卡顿，重试即可。")
    elif good and bad:
        out(f"结论：{host} 解析出多个地址，其中 {', '.join(bad)} 没有正常响应、{', '.join(good)} 正常。"
            f"把 push_url 里的 {host} 改成 {good[0]} 即可。")
    elif good:
        out("结论：直连能通、engin_cli 发送不通 —— 是代理的问题。把这个地址加进系统代理的「不使用代理」清单，"
            "或设环境变量 NO_PROXY=" + str(host))
    elif result["variant"]:
        out("结论：travel_date 用字符串能通、用整数不通 —— 对方服务处理整数 travel_date 时出错卡住了，"
            "请对方修正（建表语句里 travel_date 是 int）。")
    else:
        out("结论：对方服务对这个请求一直没有正常响应。用 push --preview 打印报文，和能通的 curl 报文逐项对比，"
            "并查看对方服务的日志。")
    return result


_FALSY = {"false", "0", "fail", "failed", "failure", "error", "no", "n", ""}


def check_response(text: str, fields: Optional[Dict[str, str]] = None) -> None:
    """2xx 响应的业务成败判定。失败抛 BusinessError（带 code / msg / trace_id）。

    入参：text 响应体；fields {"success","code","message","trace"} → 响应体里的字段名，
          success 为空串时不检查
    规则：响应体不是 JSON 对象、或没有 success 字段 → 视为成功（只看 HTTP 状态码）；
          有这个字段且为假（false / 0 / "false" / "fail" …）→ 失败。
    """
    f = {"success": "status", "code": "code", "message": "msg", "trace": "trace_id",
         **(fields or {})}
    if not f["success"]:
        return
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return
    if not isinstance(data, dict) or f["success"] not in data:
        return
    v = data[f["success"]]
    ok = bool(v) if not isinstance(v, str) else v.strip().lower() not in _FALSY
    if ok:
        return
    parts = [f"{k}={data[name]}" for k, name in (("code", f["code"]), ("msg", f["message"]),
                                                  ("trace_id", f["trace"]))
             if name and name in data]
    raise BusinessError("业务失败（HTTP 200）：" + (" ".join(parts) or text[:500]))


def _post_watched(url: str, body: Dict[str, Any], headers: Dict[str, str],
                  timeout: float, on_wait=None) -> tuple:
    """同 _post，但等待响应期间每秒回调 on_wait(已等秒数)，进度条用它显示读秒 ——
    否则一个请求等 120 秒，屏幕上 120 秒一动不动，看起来就是卡死了。"""
    if on_wait is None:
        return _post(url, body, headers, timeout)
    box: Dict[str, Any] = {}

    def _work():
        try:
            box["ok"] = _post(url, body, headers, timeout)
        except BaseException as e:                  # noqa: BLE001 —— 原样转交给主线程
            box["err"] = e

    t0 = time.time()
    th = threading.Thread(target=_work, daemon=True)
    th.start()
    while th.is_alive():
        th.join(1.0)
        if th.is_alive():
            on_wait(time.time() - t0)
    if "err" in box:
        raise box["err"]
    return box["ok"]


def _post_with_retry(url: str, body: Dict[str, Any], headers: Dict[str, str],
                     timeout: float, retries: int, backoff: float,
                     response_fields: Optional[Dict[str, str]] = None,
                     on_retry=None, on_wait=None) -> None:
    """失败重试。不可重试的错误直接抛，不浪费时间。

    on_retry(第几次重试, 共几次, 原因, 等几秒)：每次重试前回调，进度条用它把「在重试」显示出来。
    on_wait(第几次尝试, 已等秒数)：等待响应期间每秒回调。
    每一次都是「连不上」或「超时没有响应」→ 抛 UnreachableError，调用方据此停止整次推送。
    超时最多重试 TIMEOUT_RETRIES 次（见上），不按 push_retries 重试到底。
    """
    last = None
    unreachable = True          # 每次都连不上或超时
    timeouts = 0
    for attempt in range(max(int(retries), 0) + 1):
        timed_out = False
        try:
            wait_cb = None if on_wait is None else (lambda s, _a=attempt + 1: on_wait(_a, s))
            status, text = _post_watched(url, body, headers, timeout, wait_cb)
            text = text or ""
            if 200 <= status < 300:
                check_response(text, response_fields)   # 业务失败直接抛，不重试
                return
            text = text[:500]
            last = f"HTTP {status}: {text}"
            if status not in RETRYABLE_STATUS:
                raise RuntimeError(last)           # 4xx：报文/鉴权问题，重试无意义
            unreachable = False
        except urllib.error.HTTPError as e:
            unreachable = False
            text = e.read().decode("utf-8", "replace")[:500] if e.fp else ""
            last = f"HTTP {e.code}: {text}"
            if e.code not in RETRYABLE_STATUS:
                raise RuntimeError(last) from None
        except urllib.error.URLError as e:
            timed_out = isinstance(e.reason, (socket.timeout, TimeoutError))
            unreachable = unreachable and (timed_out or _is_unreachable(e.reason))
            last = f"网络错误：{_reason_text(e.reason)}"
        except (socket.timeout, TimeoutError):
            timed_out = True
        if timed_out:
            timeouts += 1
            last = f"超时（{timeout:g}s 没有响应）"
            if timeouts > TIMEOUT_RETRIES:
                break
        if attempt < retries:
            wait = backoff * (2 ** attempt)        # 指数退避，别把下游打死
            if on_retry is not None:
                on_retry(attempt + 1, int(retries), last, wait)
            time.sleep(wait)
    if unreachable:
        if timeouts:
            raise UnreachableError(f"对方连续 {timeouts} 次超过 {timeout:g}s 没有响应，"
                                   f"服务可能卡住了（地址 {url}）")
        raise UnreachableError(f"{last}（地址 {url}）")
    raise RuntimeError(last or "未知错误")


def pk_issue_rows(df: pd.DataFrame, pk: Sequence[str]) -> pd.DataFrame:
    """pkId 有问题的行：pkId 有空值的行 + pkId 重复的行（重复的每一行都算）。"""
    if df.empty or not pk:
        return df.iloc[:0]
    keys = df[list(pk)]
    bad = keys.isna().any(axis=1) | keys.duplicated(keep=False)
    return df[bad]


def check_pk(table: str, df: pd.DataFrame, pk: Sequence[str]) -> str:
    """发送前检查 pkId：能不能唯一定位一行、有没有空值。有问题返回说明，没问题返回空串。

    **只提醒，不拦截**：照常发送，下游按 pkId 自己查重。
    下游网关按 pkId「先查询、再更新」，pkId 重复的行到下游会互相覆盖、只剩一行，
    所以要提醒出来，并把这些行的请求 JSON 留下来方便对照（见 push_table）。
    空串不算空值（例如维度表没有三级维度时 dimension_level3 就是空串，是合法取值）。
    """
    if df.empty or not pk:
        return ""
    keys = df[list(pk)]
    msgs = []
    nulls = int(keys.isna().any(axis=1).sum())
    if nulls:
        cols = [c for c in pk if keys[c].isna().any()]
        msgs.append(f"pkId {list(pk)} 有 {nulls:,} 行为空（字段 {cols}）")
    dup = keys.duplicated(keep=False)
    if dup.any():
        n_rows = int(dup.sum())
        n_lost = int(keys.duplicated().sum())
        sample = keys[dup].head(3).astype(str).agg(" / ".join, axis=1).tolist()
        msgs.append(f"pkId {list(pk)} 不能唯一定位一行：{n_rows:,} 行的 pkId 有重复"
                    f"（例如 {'；'.join(sample)}），下游按 pkId 先查再更新时最多会互相覆盖掉 "
                    f"{n_lost:,} 行")
    if not msgs:
        return ""
    hint = DEFAULT_PUSH_PK.get(table)
    if hint and list(pk) != list(hint):
        msgs.append(f"能唯一定位一行的默认 pkId 是 {hint}")
    return "；".join(msgs) + "。已照常发送"


def _dump_pk_issue(table: str, rows: pd.DataFrame, pk: Sequence[str], settings,
                   cfg: Dict[str, Any]) -> tuple:
    """pkId 有问题的行 → (单行示例请求 JSON, 完整请求 JSON 文件路径)。

    示例 = 第一组重复的行（或第一行空值行）组成的一个请求，格式与真实请求完全一样，
    拿去 curl 就能在下游复现；文件里是全部有问题的行，写在 <output_dir>/push_debug/ 下。
    """
    import os
    from datetime import datetime

    def _body(part):
        return build_payload(table, part, pk,
                             body_fields=getattr(settings, "push_body_fields", None),
                             table_name=cfg["name"],
                             extra=getattr(settings, "push_extra_fields", None))

    keys = rows[list(pk)].astype(str)
    first = keys.iloc[0].tolist()
    group = rows[(keys == first).all(axis=1)]
    sample = json.dumps(_body(group), ensure_ascii=False, separators=(",", ":"))
    path = ""
    try:
        d = os.path.join(getattr(settings, "output_dir", "output") or "output", "push_debug")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{table}_pkId_{datetime.now():%Y%m%d_%H%M%S}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_body(rows), fh, ensure_ascii=False, indent=2)
    except OSError as e:                         # 写不了文件不影响推送
        log.warning("pkId 问题行的请求 JSON 写文件失败：%s", e)
        path = ""
    return sample, path


def push_table(table: str, df: pd.DataFrame, *, settings, progress=None) -> PushResult:
    """推一张表。分批发，任何一批失败都记下来但不中断其余批次。

    例外：推送地址连不上（UnreachableError）时立即停止，res.unreachable = True。
    progress：进度条（见 progress.PushProgress），None 就不显示。
    """
    res = PushResult(table=table)
    t0 = time.time()
    if df is None or df.empty:
        res.skipped = True
        return res

    cfg = _table_config(table, settings)
    pk = cfg["pk"]
    missing = [c for c in pk if c not in df.columns]
    if missing:
        res.error = f"主键字段不在表里：{missing}（检查 push_pk 配置）"
        res.elapsed = time.time() - t0
        return res
    warn = check_pk(table, df, pk)
    if warn:
        # 只提醒、照常发送（下游按 pkId 自己查）；把有问题的行的请求 JSON 留下来方便对照
        res.warning = warn
        res.pk_sample, res.pk_dump = _dump_pk_issue(table, pk_issue_rows(df, pk), pk,
                                                    settings, cfg)
        log.debug("推送 %s：%s", table, warn)        # CLI 会连同请求 JSON 一起打印

    size = max(int(getattr(settings, "push_batch_size", 500)), 1)
    url = settings.push_url
    headers = dict(getattr(settings, "push_headers", None) or {})
    timeout = float(getattr(settings, "push_timeout", 30))
    retries = int(getattr(settings, "push_retries", 2))
    backoff = float(getattr(settings, "push_retry_backoff", 1.0))
    total = (len(df) + size - 1) // size
    if progress is not None:
        progress.start()

    for i in range(0, len(df), size):
        chunk = df.iloc[i:i + size]
        batch_no = i // size + 1
        on_retry = on_wait = None
        if progress is not None:
            def on_retry(attempt, n, reason, wait, _b=batch_no):
                progress.retry(_b, attempt, n, reason, wait)

            def on_wait(attempt, secs, _b=batch_no):
                progress.waiting(_b, attempt, secs, timeout)
        body = build_payload(table, chunk, pk,
                             body_fields=getattr(settings, "push_body_fields", None),
                             table_name=cfg["name"],
                             extra=getattr(settings, "push_extra_fields", None))
        try:
            if getattr(settings, "push_dry_run", False):
                log.info("[push dry-run] %s 第 %d 批 %d 行，不发送",
                         table, res.batches + 1, len(chunk))
            else:
                _post_with_retry(url, body, headers, timeout, retries, backoff,
                                 response_fields=_response_fields(settings),
                                 on_retry=on_retry, on_wait=on_wait)
            res.batches += 1
            res.rows += len(chunk)
            if progress is not None:
                progress.batch_done(len(chunk), ok=True)
        except UnreachableError as e:
            # 连不上：剩下的批次一样会失败，别再一批一批地白等重试
            res.unreachable = True
            res.failed_batches += total - res.batches
            res.error = f"推送地址连不上或没有响应，已停止推送：{e}"
            if progress is not None:
                progress.batch_failed(batch_no, str(e))
            else:
                log.error("推送 %s 第 %d 批：%s", table, batch_no, res.error)
            break
        except Exception as e:                      # noqa: BLE001 —— 推送失败不能拖垮跑批
            res.failed_batches += 1
            if not res.error:
                res.error = f"第 {batch_no} 批（{len(chunk)} 行）{e}"
            if progress is not None:
                progress.batch_done(len(chunk), ok=False)
                progress.batch_failed(batch_no, str(e))
            else:
                log.warning("推送 %s 第 %d 批失败：%s", table, batch_no, e)
    res.elapsed = time.time() - t0
    if progress is not None:
        progress.finish("✓" if res.ok else "✗ 连不上或没有响应，已停止" if res.unreachable
                        else f"✗ {res.failed_batches} 批失败")
    return res


def _response_fields(settings) -> Dict[str, str]:
    """响应体里成败 / 错误码 / 错误信息 / 追踪号 的字段名（settings 可改）。"""
    return {"success": getattr(settings, "push_success_field", "status"),
            "code": getattr(settings, "push_code_field", "code"),
            "message": getattr(settings, "push_message_field", "msg"),
            "trace": getattr(settings, "push_trace_field", "trace_id")}


def _table_config(table: str, settings) -> Dict[str, Any]:
    """这张表推什么名字、用哪组主键。settings 可逐表覆盖。"""
    pk_map = {**DEFAULT_PUSH_PK, **(getattr(settings, "push_pk", None) or {})}
    name_map = getattr(settings, "push_table_names", None) or {}
    return {"pk": list(pk_map.get(table, [])), "name": name_map.get(table, table)}


def push_all(tables: Dict[str, pd.DataFrame], *, settings,
             progress_factory=None) -> PushReport:
    """跑批产物 → 逐表推送。开关关着、地址没配就整体跳过。

    入参：tables {表名: DataFrame}（就是 PipelineResult.tables）
          progress_factory(表名, 总批数, 总行数) → 进度条；None 不显示
    出参：PushReport（.ok / .errors / .notes()）
    推送地址连不上时，剩下的表不再推，记为失败（error 里写明原因），可之后用 push 补推。
    """
    rep = PushReport()
    if not getattr(settings, "push_enabled", False):
        return rep
    if not getattr(settings, "push_url", ""):
        rep.results.append(PushResult(table="-", skipped=True,
                                      error="push_enabled=True 但 push_url 没配"))
        return rep

    only = getattr(settings, "push_tables", None)
    only = set(only) if only else None
    size = max(int(getattr(settings, "push_batch_size", 500)), 1)
    todo = [(t, df) for t, df in tables.items() if only is None or t in only]
    for k, (table, df) in enumerate(todo):
        progress = None
        if progress_factory is not None and df is not None and not df.empty:
            progress = progress_factory(table, (len(df) + size - 1) // size, len(df))
        r = push_table(table, df, settings=settings, progress=progress)
        rep.results.append(r)
        if r.unreachable:
            for t2, _ in todo[k + 1:]:
                rep.results.append(PushResult(table=t2, skipped=True,
                                              error="未推送：推送地址连不上或没有响应（见上一张表的报错）"))
            break
    return rep
