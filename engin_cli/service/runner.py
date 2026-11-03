# -*- coding: utf-8 -*-
"""执行器：把任务配置翻译成 engin_cli 命令，排队、一次一个地在子进程里跑。

· 子进程跑：一次执行出错 / 卡死不会拖垮服务，也能随时取消；
· 一次只跑一个：同一个库同时跑两批会互相删数据（写库是按景区 × 日期先删后插）；
· 输出写到 <data_dir>/runs/<id>.log，页面实时看；run 命令另写一份 --summary-json，
  里面有各景区成败、数据质量检测结果、写库 / 推送行数，页面据此展示。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import queue
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

from .store import Store, now_str

log = logging.getLogger(__name__)

# 项目根目录（engin_cli 的上一级）：子进程在这里跑，settings_local.py / output/ / logs/ 都按它找
APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TASK_DEFAULTS: Dict[str, Any] = {
    # 跑完后复制给其他景区（engin_cli mock：把源景区刚算好的 ADS 结果复制给新景区编号并推送，不写本地库）
    # [{"from_scenic": 源景区, "to_scenic": 新景区编号, "to_name": 新景区名称}]；日期、表和本次跑批一样
    "copies": [],
    "scenics": "",              # 逗号分隔；空 = 取数区间内有评论的全部景区
    "tables": "",               # 计算 / 输出哪些表（逗号分隔简写）；空 = 全部
    "date_mode": "today",       # today / yesterday / recent（近 N 天）/ range（固定区间）
    "recent_days": 3,
    "start_date": "",
    "end_date": "",
    "rerun_days": None,         # None = 用 settings.rerun_days
    "push": "drill,macro",      # 推哪些表；"" = 不推送
    "skip_db": False,           # True = 不写 ADS 表（只算 + 推送）
    "drill_keyword_filter": False,
    "quality_gate": True,
    "quality_retries": None,    # None = 用 settings.quality_retries
    "backfill_lookback": "",    # "" = 用 settings
    "verbose": True,
}

DATE_MODES = ("today", "yesterday", "recent", "range")
TABLE_KEYS = ("drill", "macro", "core", "platform", "dimension", "content")

# 推送设置（推送页面上改，存在服务的 kv 表里）。None / "" = 用 settings_local.py 里的
PUSH_DEFAULTS: Dict[str, Any] = {
    "enabled": True,              # 总开关：关掉后任务一律只算不推（补推照常可用）
    "push_url": "",
    "push_batch_size": None,
    "push_timeout": None,
    "push_retries": None,
    "push_dry_run": False,        # 试推：只组报文不真发
    "push_strict": False,         # 推送失败也算执行失败
}


def normalize_push_settings(data: Optional[dict]) -> Dict[str, Any]:
    out = dict(PUSH_DEFAULTS)
    for k, v in (data or {}).items():
        if k in out:
            out[k] = v
    out["push_url"] = str(out["push_url"] or "").strip()
    if out["push_url"] and not out["push_url"].lower().startswith(("http://", "https://")):
        raise ValueError("推送地址要以 http:// 或 https:// 开头")
    for k, lo in (("push_batch_size", 1), ("push_retries", 0)):
        out[k] = None if out[k] in (None, "") else max(int(out[k]), lo)
    out["push_timeout"] = None if out["push_timeout"] in (None, "") else max(float(out["push_timeout"]), 1.0)
    for k in ("enabled", "push_dry_run", "push_strict"):
        out[k] = bool(out[k])
    return out


def push_overrides(ps: Dict[str, Any]) -> Dict[str, Any]:
    """推送设置 → 传给子进程的 OPINION_SETTINGS_JSON（只放页面上改过的）。"""
    out = {k: v for k, v in ps.items() if k.startswith("push_") and v not in (None, "")}
    for k in ("push_dry_run", "push_strict"):
        if not ps.get(k):
            out.pop(k, None)               # 关着 = 用 settings_local 的
    return out


def normalize_notify_settings(data: Optional[dict]) -> Dict[str, str]:
    """飞书通知设置（页面上配，存系统表 service_setting 的 notify）：留空 = 用 settings_local / 环境变量的。"""
    d = data or {}
    out = {"feishu_webhook": str(d.get("feishu_webhook") or "").strip(),
           "feishu_secret": str(d.get("feishu_secret") or "").strip()}
    if out["feishu_webhook"] and not out["feishu_webhook"].lower().startswith("https://"):
        raise ValueError("飞书机器人地址要以 https:// 开头")
    return out


def notify_overrides(ns: Dict[str, str]) -> Dict[str, str]:
    """通知设置 → 传给子进程的 OPINION_SETTINGS_JSON（只放页面上配了的）。"""
    out = {}
    if ns.get("feishu_webhook"):
        out["notify_feishu_webhook"] = ns["feishu_webhook"]
        out["notify_feishu_secret"] = ns.get("feishu_secret", "")
    return out


def apply_push_switch(argv: List[str], enabled: bool) -> List[str]:
    """推送总开关关着：run 命令去掉 --push xxx，换成 --no-push；mock 改成只预览报文、不发送。"""
    if enabled or not argv:
        return list(argv)
    if argv[0] == "mock":
        return list(argv) if "--preview" in argv else list(argv) + ["--preview"]
    if argv[0] != "run":
        return list(argv)
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a == "--push":
            skip = True
            continue
        if a != "--no-push":
            out.append(a)
    return out + ["--no-push"]


def _tables(text, what: str) -> str:
    parts = [t.strip().lower() for t in str(text or "").replace("，", ",").split(",") if t.strip()]
    bad = [t for t in parts if t not in TABLE_KEYS]
    if bad:
        raise ValueError(f"{what}里有不认识的表：{', '.join(bad)}")
    return ",".join(t for t in TABLE_KEYS if t in parts)


def _ymd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def _valid_ymd(s: str) -> bool:
    try:
        dt.datetime.strptime(str(s), "%Y%m%d")
        return True
    except ValueError:
        return False


def normalize_config(cfg: Optional[dict]) -> Dict[str, Any]:
    """补齐默认值并校验。配错直接抛 ValueError（页面上显示）。"""
    cfg = dict(cfg or {})
    if cfg.get("task_type") == "mock":
        # 早先单独存的「复制任务」：转成「跑源景区 + 跑完复制」，别变成跑全部景区
        cfg.setdefault("scenics", "")
        if not cfg["scenics"]:
            cfg["scenics"] = cfg.get("from_scenic", "")
        cfg["copies"] = [{k: cfg.get(k, "") for k in ("from_scenic", "to_scenic", "to_name")}]
    out = dict(TASK_DEFAULTS)
    for k, v in cfg.items():
        if k in out:
            out[k] = v
    out["scenics"] = ",".join(s.strip() for s in str(out["scenics"] or "").replace("，", ",")
                              .split(",") if s.strip())
    out["copies"] = _copies(out["copies"], out["scenics"])
    if out["date_mode"] not in DATE_MODES:
        raise ValueError(f"日期方式只能是 {DATE_MODES}")
    out["recent_days"] = max(int(out["recent_days"] or 1), 1)
    if out["date_mode"] == "range":
        if not (_valid_ymd(out["start_date"]) and _valid_ymd(out["end_date"])):
            raise ValueError("固定区间要填开始、结束日期（yyyyMMdd）")
        if str(out["start_date"]) > str(out["end_date"]):
            raise ValueError("开始日期晚于结束日期")
    for k in ("rerun_days", "quality_retries"):
        out[k] = None if out[k] in (None, "") else max(int(out[k]), 0)
    out["tables"] = _tables(out["tables"], "计算的表")
    if out["copies"] and cfg.get("skip_db"):
        raise ValueError("「只算不写库」时不能跑完复制：复制要读库里刚算好的结果")
    out["push"] = _tables(out["push"], "推送的表")
    if out["tables"]:
        # 没算的表推不了
        out["push"] = ",".join(t for t in out["push"].split(",") if t and t in out["tables"].split(","))
    for k in ("skip_db", "drill_keyword_filter", "quality_gate", "verbose"):
        out[k] = bool(out[k])
    out["backfill_lookback"] = str(out["backfill_lookback"] or "").strip()
    return out


def task_dates(cfg: Dict[str, Any], today: Optional[dt.date] = None) -> Tuple[str, str]:
    """任务这次要跑的目标日期区间 (start, end)。"""
    today = today or dt.date.today()
    mode = cfg["date_mode"]
    if mode == "today":
        return _ymd(today), _ymd(today)
    if mode == "yesterday":
        y = today - dt.timedelta(days=1)
        return _ymd(y), _ymd(y)
    if mode == "recent":
        return _ymd(today - dt.timedelta(days=cfg["recent_days"] - 1)), _ymd(today)
    return str(cfg["start_date"]), str(cfg["end_date"])


def build_run_argv(cfg: Dict[str, Any], today: Optional[dt.date] = None) -> Tuple[List[str], str]:
    """任务配置 → `engin_cli.cli run` 的参数（不含 --summary-json），以及一句话说明。"""
    cfg = normalize_config(cfg)
    start, end = task_dates(cfg, today)
    argv = ["run", "--start-date", start, "--end-date", end, "--no-progress"]
    if cfg["scenics"]:
        argv += ["--scenic", cfg["scenics"]]
    if cfg["rerun_days"] is not None:
        argv += ["--rerun-days", str(cfg["rerun_days"])]
    if cfg["tables"]:
        argv += ["--tables", cfg["tables"]]
    argv += ["--push", cfg["push"]] if cfg["push"] else ["--no-push"]
    if cfg["skip_db"]:
        argv.append("--skip-db")
    if cfg["drill_keyword_filter"]:
        argv.append("--drill-keyword-filter")
    if not cfg["quality_gate"]:
        argv.append("--no-quality-gate")
    if cfg["quality_retries"] is not None:
        argv += ["--quality-retries", str(cfg["quality_retries"])]
    if cfg["backfill_lookback"]:
        argv += ["--backfill-lookback", cfg["backfill_lookback"]]
    if cfg["verbose"]:
        argv.append("-v")
    title = (f"{cfg['scenics'] or '全部景区'} · "
             f"{start if start == end else f'{start}~{end}'}")
    return argv, title


def _copies(items, scenics: str) -> List[Dict[str, str]]:
    """校验「跑完后复制」：源景区默认 = 任务里唯一的景区；源景区要在本次跑批的景区里。"""
    run_sc = [s for s in scenics.split(",") if s]
    out, seen = [], set()
    for c in items or []:
        c = {k: str((c or {}).get(k) or "").strip() for k in ("from_scenic", "to_scenic", "to_name")}
        if not any(c.values()):
            continue                                   # 页面上加了一行没填，忽略
        if not c["from_scenic"] and len(run_sc) == 1:
            c["from_scenic"] = run_sc[0]
        if not (c["from_scenic"] and c["to_scenic"] and c["to_name"]):
            raise ValueError("跑完后复制：每一行都要填源景区、新景区编号、新景区名称")
        if c["from_scenic"] == c["to_scenic"]:
            raise ValueError(f"跑完后复制：新景区编号不能和源景区一样（{c['to_scenic']}）")
        if run_sc and c["from_scenic"] not in run_sc:
            raise ValueError(f"跑完后复制：源景区 {c['from_scenic']} 不在这个任务要跑的景区里")
        if c["to_scenic"] in seen:
            raise ValueError(f"跑完后复制：新景区 {c['to_scenic']} 重复了")
        seen.add(c["to_scenic"])
        out.append(c)
    return out


def build_copy_steps(cfg: Dict[str, Any], start: str, end: str) -> List[Dict[str, Any]]:
    """跑批之后要接着执行的复制：`engin_cli.cli mock`，日期、表和本次跑批一样。"""
    steps = []
    for c in cfg["copies"]:
        argv = ["mock", "--from-scenic", c["from_scenic"], "--to-scenic", c["to_scenic"],
                "--to-name", c["to_name"], "--start-date", start, "--end-date", end, "--no-progress"]
        if cfg["tables"]:
            argv += ["--tables", cfg["tables"]]
        steps.append({"from_scenic": c["from_scenic"], "kind": "mock", "argv": argv,
                      "title": f"复制 {c['from_scenic']} → {c['to_scenic']}（{c['to_name']}） · "
                               f"{start if start == end else f'{start}~{end}'}"})
    return steps


def build_task_argv(cfg: Dict[str, Any], today: Optional[dt.date] = None):
    """任务配置 → (执行类型, 命令参数, 一句话说明, 跑完后接着执行的复制)。定时、手动执行都走这里。"""
    cfg = normalize_config(cfg)
    argv, title = build_run_argv(cfg, today)
    start, end = task_dates(cfg, today)
    return "run", argv, title, build_copy_steps(cfg, start, end)


def build_push_argv(scenics: str = "", tables: str = "", start: str = "",
                    end: str = "") -> Tuple[List[str], str]:
    """补推：`engin_cli.cli push`，从库里读算好的结果再推（日期必填）。"""
    if not start:
        raise ValueError("补推要选日期")
    argv = ["push", "--no-progress"]
    if tables:
        argv += ["--tables", tables]
    if scenics:
        argv += ["--scenic", scenics]
    if start:
        if not _valid_ymd(start) or (end and not _valid_ymd(end)):
            raise ValueError("日期格式是 yyyyMMdd")
        argv += ["--start-date", start, "--end-date", end or start]
    title = f"补推 {tables or '全部表'} · {scenics or '全部景区'}" + (f" · {start}~{end or start}" if start else "")
    return argv, title


def final_status(exit_code: int, summary: dict) -> str:
    if exit_code == 0:
        return "success"
    if any(s.get("quality_blocked") for s in (summary or {}).get("scenics") or []):
        return "blocked"
    return "failed"


class Runner:
    """排队执行。submit() 立刻返回执行记录 id，worker 线程一个一个跑。"""

    def __init__(self, store: Store, data_dir: str, python: str = sys.executable,
                 app_root: str = APP_ROOT):
        self.store = store
        self.data_dir = data_dir
        self.python = python
        self.app_root = app_root
        self.run_dir = os.path.join(data_dir, "runs")
        os.makedirs(self.run_dir, exist_ok=True)
        self._q: "queue.Queue[int]" = queue.Queue()
        self._proc: Optional[subprocess.Popen] = None
        self._current: Optional[int] = None
        self._cancelled: set = set()
        self._then: Dict[int, tuple] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="runner", daemon=True)

    def start(self):
        n = self.store.mark_interrupted()
        if n:
            log.warning("上次服务退出时有 %d 条执行没跑完，已标记为失败", n)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._q.put(0)
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()

    def notify_settings(self) -> Dict[str, str]:
        try:
            return normalize_notify_settings(self.store.get_kv("notify", {}))
        except ValueError:
            return normalize_notify_settings({})

    def push_settings(self) -> Dict[str, Any]:
        try:
            return normalize_push_settings(self.store.get_kv("push", {}))
        except ValueError:
            return dict(PUSH_DEFAULTS)

    def queued(self) -> int:
        return self._q.qsize()

    @property
    def current(self) -> Optional[int]:
        return self._current

    def submit(self, kind: str, argv: List[str], title: str, trigger: str = "manual",
               task: Optional[dict] = None, then: Optional[List[Dict[str, Any]]] = None) -> int:
        """then：跑完后接着执行的复制（build_copy_steps），源景区这次通过了才执行。"""
        run_id = self.store.create_run(kind, argv, title, trigger, task)
        self.store.update_run(run_id, log_path=os.path.join(self.run_dir, f"{run_id}.log"))
        if then:
            self._then[run_id] = (then, trigger, task)
        self._q.put(run_id)
        return run_id

    def _follow_up(self, run_id: int, status: str, summary: dict, log_path: str) -> None:
        """跑批结束：源景区这次通过了，就接着提交复制；没通过就跳过并说明原因。"""
        item = self._then.pop(run_id, None)
        if not item:
            return
        steps, trigger, task = item
        ok = {str(s.get("scenic")): bool(s.get("ok")) for s in (summary or {}).get("scenics") or []}
        lines = []
        for st in steps:
            src = st["from_scenic"]
            if status == "cancelled":
                why = "跑批被取消"
            elif src not in ok:
                why = f"这次跑批没有 {src} 的结果"
            elif not ok[src]:
                why = f"{src} 这次没通过（失败或质量检测回退）"
            else:
                why = ""
            if why:
                lines.append(f"跳过 {st['title']}：{why}")
                self.store.add_event(f"#{run_id} 跑完后复制跳过：{st['title']}（{why}）", task, "warn")
                continue
            nid = self.submit(st["kind"], st["argv"], st["title"], trigger, task)
            lines.append(f"已提交 #{nid} {st['title']}")
            self.store.add_event(f"#{run_id} 跑完后复制：已提交执行 #{nid}（{st['title']}）", task)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n跑完后复制：\n" + "\n".join("  · " + l for l in lines) + "\n")
        except OSError:
            pass

    def _record_checks(self, run: dict, summary: dict) -> None:
        """跑批结束：每个景区记一条数据质量检测记录（检测过程从执行摘要里挪出来）。"""
        if run.get("kind") != "run":
            return
        trigger = "repair" if str(run.get("title") or "").startswith("数据修复") else (run.get("trigger") or "manual")
        task = {"id": run["task_id"], "name": run["task_name"]} if run.get("task_id") else None
        dates = (summary or {}).get("dates") or []
        for sc in (summary or {}).get("scenics") or []:
            trace = sc.pop("quality_trace", None) or []
            d = sc.get("dates") or dates or ["", ""]
            last = trace[-1] if trace else {}
            errs = list(last.get("errors") or sc.get("errors") or [])
            if sc.get("exception") and not trace:
                status, text = "fail", f"跑批异常，没走到检测：{sc['exception']}"
            elif sc.get("quality_blocked"):
                status, text = "blocked", f"{len(trace)} 次检测都不通过，已回退：" + (errs[0] if errs else "")
            elif errs:
                status, text = "fail", "检测不通过（闸门关闭，照常写库）：" + errs[0]
            elif len(trace) > 1:
                status, text = "fixed", f"第 {len(trace)} 次重算后通过（{len(last.get('steps') or [])} 项）"
            else:
                status, text = "pass", f"全部通过（{len(last.get('steps') or [])} 项）"
            if len(errs) > 1 and status in ("blocked", "fail"):
                text += f" 等 {len(errs)} 项"
            self.store.add_check(
                check_type="run", trigger=trigger, scenic=str(sc.get("scenic") or ""), start=str(d[0] or ""),
                end=str(d[-1] or ""), status=status, attempts=len(trace) or int(sc.get("quality_attempts") or 0),
                items=len(last.get("steps") or []), issues=len(errs) if status != "pass" and status != "fixed" else 0,
                summary=text, run_id=int(run["id"]), task=task,
                detail={"attempts": trace, "dates": d, "run_title": run.get("title"), "exception": sc.get("exception", ""),
                        "tables": sc.get("tables") or {}, "command": run.get("command") or []})

    def cancel(self, run_id: int) -> bool:
        run = self.store.get_run(run_id)
        if not run or run["status"] not in ("queued", "running"):
            return False
        with self._lock:
            self._cancelled.add(int(run_id))
            if self._current == int(run_id) and self._proc and self._proc.poll() is None:
                self._proc.terminate()
        if run["status"] == "queued":
            self.store.update_run(run_id, status="cancelled", finished_at=now_str(),
                                  message="排队中被取消")
        return True

    # ------------------------------------------------------------------
    def _loop(self):
        while not self._stop.is_set():
            run_id = self._q.get()
            if self._stop.is_set() or not run_id:
                continue
            if run_id in self._cancelled:
                continue
            try:
                self._execute(run_id)
            except Exception as e:                    # noqa: BLE001 —— worker 不能死
                log.exception("执行 %s 出错", run_id)
                self.store.update_run(run_id, status="failed", finished_at=now_str(),
                                      message=f"服务内部错误：{e}")
            finally:
                self._current = None

    def _execute(self, run_id: int):
        run = self.store.get_run(run_id)
        if not run or run["status"] != "queued":
            return
        ps = self.push_settings()
        argv = apply_push_switch(run["command"], ps["enabled"])
        push_cut = argv != list(run["command"])
        summary_path = os.path.join(self.run_dir, f"{run_id}.summary.json")
        if argv and argv[0] == "run":
            argv += ["--summary-json", summary_path]
        cmd = [self.python, "-m", "engin_cli.cli"] + argv
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        over = push_overrides(ps)
        nover = notify_overrides(self.notify_settings())
        if over or nover:
            env["OPINION_SETTINGS_JSON"] = json.dumps({**over, **nover}, ensure_ascii=False)
        else:
            env.pop("OPINION_SETTINGS_JSON", None)
        self._current = run_id
        self.store.update_run(run_id, status="running", started_at=now_str())
        with open(run["log_path"], "w", encoding="utf-8") as logf:
            logf.write(f"$ {' '.join(cmd)}\n（工作目录 {self.app_root}）\n")
            if push_cut:
                logf.write("（推送总开关已关闭：本次只算不推）\n")
            if over:
                logf.write("（推送设置来自服务页面：" + "，".join(
                    f"{k}={v}" for k, v in over.items()) + "）\n")
            if nover:
                logf.write("（飞书通知地址来自服务页面）\n")       # 地址里带令牌，不写进日志
            logf.write("\n")
            logf.flush()
            with self._lock:
                self._proc = subprocess.Popen(cmd, cwd=self.app_root, env=env,
                                              stdout=logf, stderr=subprocess.STDOUT)
            code = self._proc.wait()
        summary = {}
        if os.path.exists(summary_path):
            try:
                with open(summary_path, encoding="utf-8") as f:
                    summary = json.load(f)
            except (OSError, ValueError):
                summary = {}
        if run_id in self._cancelled:
            status, msg = "cancelled", "执行中被取消"
        else:
            status = final_status(code, summary)
            msg = {"success": "", "blocked": "数据质量检测不通过，已回退（未写库/未推送）",
                   "failed": f"退出码 {code}"}[status]
        try:
            self._record_checks(run, summary)        # 检测过程存进检测记录表，执行摘要里不再留一份
        except Exception:                             # noqa: BLE001 —— 记录失败不影响执行结果
            log.exception("执行 #%s 的数据质量检测记录保存失败", run_id)
        self.store.update_run(run_id, status=status, exit_code=code, finished_at=now_str(),
                              summary=summary, message=msg)
        self._follow_up(run_id, status, summary, run["log_path"])
        log.info("执行 #%s 结束：%s（退出码 %s）", run_id, status, code)
