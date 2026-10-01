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
import time
import urllib.error
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
    """推送地址根本连不上（每次都是拒绝连接 / 域名解析失败）。

    这种情况下后面每一批、每一张表都会一样失败，每批还要白等完整的重试退避 ——
    表现出来就是「卡住了」。所以一旦确认连不上，整次推送立即停止。
    """


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


def _post(url: str, body: Dict[str, Any], headers: Dict[str, str],
          timeout: float) -> tuple:
    """发一个 POST，返回 (status, 响应文本)。只用标准库。"""
    raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for k, v in (headers or {}).items():
        req.add_header(k, str(v))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


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


def _post_with_retry(url: str, body: Dict[str, Any], headers: Dict[str, str],
                     timeout: float, retries: int, backoff: float,
                     response_fields: Optional[Dict[str, str]] = None,
                     on_retry=None) -> None:
    """失败重试。不可重试的错误直接抛，不浪费时间。

    on_retry(第几次重试, 共几次, 原因, 等几秒)：每次重试前回调，进度条用它把「在重试」显示出来。
    每一次都是「连不上」→ 抛 UnreachableError，调用方据此停止整次推送。
    """
    last = None
    unreachable = True
    for attempt in range(max(int(retries), 0) + 1):
        try:
            status, text = _post(url, body, headers, timeout)
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
            unreachable = unreachable and _is_unreachable(e.reason)
            last = f"网络错误：{_reason_text(e.reason)}"
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                last = f"超时（{timeout:g}s 没有响应）"
        except (socket.timeout, TimeoutError):
            unreachable = False
            last = f"超时（{timeout:g}s 没有响应）"
        if attempt < retries:
            wait = backoff * (2 ** attempt)        # 指数退避，别把下游打死
            if on_retry is not None:
                on_retry(attempt + 1, int(retries), last, wait)
            time.sleep(wait)
    if unreachable:
        raise UnreachableError(f"{last}（地址 {url}）")
    raise RuntimeError(last or "未知错误")


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
        on_retry = None
        if progress is not None:
            def on_retry(attempt, n, reason, wait, _b=batch_no):
                progress.retry(_b, attempt, n, reason, wait)
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
                                 on_retry=on_retry)
            res.batches += 1
            res.rows += len(chunk)
            if progress is not None:
                progress.batch_done(len(chunk), ok=True)
        except UnreachableError as e:
            # 连不上：剩下的批次一样会失败，别再一批一批地白等重试
            res.unreachable = True
            res.failed_batches += total - res.batches
            res.error = f"推送地址连不上，已停止推送：{e}"
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
        progress.finish("✓" if res.ok else "✗ 连不上，已停止" if res.unreachable
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
                                              error="未推送：推送地址连不上（见上一张表的报错）"))
            break
    return rep
