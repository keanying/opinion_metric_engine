#!/usr/bin/env bash
# 指标计算服务（python -m engin_cli.cli serve）的启停脚本：检测 / 启动 / 重启 / 停止
#
#   bash scripts/service.sh status      # 检测：在不在运行、PID、运行了多久，以及服务 / 系统表库 / 指标库 / 定时 / 执行器各项状态
#   bash scripts/service.sh start       # 启动（已经在运行就不重复启动），等到页面能访问才返回
#   bash scripts/service.sh restart     # 重启 = 停止 + 启动（改了代码 / settings_local.py 之后用）
#   bash scripts/service.sh stop        # 停止：先正常退出，15 秒还没退再强制结束
#
# 正在执行跑批时，stop / restart 会把它中断（执行记录标成「服务重启，执行被中断」），
# 所以默认拒绝；确认要停就加 -f：
#   bash scripts/service.sh restart -f
#
# 可选环境变量：
#   APP_DIR     项目目录，默认是本脚本所在目录的上一级
#   PYTHON      用哪个 python，默认 $APP_DIR/.venv/bin/python，没有就用 python3
#   SERVE_ARGS  额外传给 serve 的参数，例如 "--port 8765" 或 "--no-scheduler"
#   RUN_USER    用哪个系统用户运行（例如 opinion）；root 执行时会切到这个用户，免得日志文件变成 root 的
#
# 文件：PID 在 service_data/serve.pid；控制台输出追加到 logs/service.out（启动失败时脚本会打印最后几行）
# 退出码：0 正常；1 没在运行 / 启动失败 / 拒绝停止；2 在运行但有依赖不可用（只有 status 会返回）

set -u

ACTION=${1:-status}
FORCE=0
for a in "${@:2}"; do
    case "$a" in
        -f|--force) FORCE=1 ;;
        *) echo "不认识的参数：$a"; exit 1 ;;
    esac
done

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}
SERVE_ARGS=${SERVE_ARGS:-}

# root 执行且指定了 RUN_USER：切到那个用户再执行同一条命令
if [ -n "${RUN_USER:-}" ] && [ "$(id -un)" != "$RUN_USER" ]; then
    exec su - "$RUN_USER" -c "APP_DIR='$APP_DIR' PYTHON='${PYTHON:-}' SERVE_ARGS='$SERVE_ARGS' bash '$SCRIPT_DIR/service.sh' $*"
fi

cd "$APP_DIR" || { echo "项目目录不存在：$APP_DIR"; exit 1; }
if [ -z "${PYTHON:-}" ]; then
    if [ -x "$APP_DIR/.venv/bin/python" ]; then PYTHON="$APP_DIR/.venv/bin/python"; else PYTHON=python3; fi
fi
PID_FILE="$APP_DIR/service_data/serve.pid"
OUT_FILE="$APP_DIR/logs/service.out"

# ---------------------------------------------------------------- 找进程
# 是本项目目录下的 engin_cli serve 进程才算（PID 可能被别的进程复用）
is_ours() {
    local pid=$1
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    ps -p "$pid" -o args= 2>/dev/null | grep -q "engin_cli.cli serve" || return 1
    [ "$(readlink "/proc/$pid/cwd" 2>/dev/null || echo "$APP_DIR")" = "$APP_DIR" ]
}

find_pid() {
    local pid
    if [ -f "$PID_FILE" ]; then
        pid=$(cat "$PID_FILE" 2>/dev/null)
        if is_ours "$pid"; then echo "$pid"; return 0; fi
    fi
    # 没有 PID 文件（比如是手动 nohup 起的）：按命令行 + 工作目录找
    for pid in $(pgrep -f "engin_cli.cli serve" 2>/dev/null); do
        if is_ours "$pid"; then echo "$pid"; return 0; fi
    done
    return 1
}

# ---------------------------------------------------------------- 检测接口
# 读 settings 拿端口和访问密码（密码不出现在命令行里），请求 /api/health。
#   health        打印各项状态；退出码 0 全部正常  2 有依赖不可用  3 连不上  4 密码不对
#   health quiet  不打印，退出码同上
#   health busy   不打印；正在执行跑批返回 0，否则 1
health() {
    SERVE_ARGS="$SERVE_ARGS" MODE="${1:-}" "$PYTHON" - <<'PY'
import base64, json, os, re, sys, urllib.error, urllib.request
sys.path.insert(0, os.getcwd())
from engin_cli.settings import load_settings
st = load_settings()
args = os.environ.get("SERVE_ARGS", "")
m = re.search(r"--port[ =](\d+)", args)
port = int(m.group(1)) if m else int(st.service_port)
m = re.search(r"--host[ =](\S+)", args)
host = m.group(1) if m else st.service_host
host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
url = f"http://{host}:{port}/api/health?refresh=1"
req = urllib.request.Request(url, headers={"X-Requested-With": "service.sh"})
if st.service_password:
    tok = base64.b64encode(f"admin:{st.service_password}".encode()).decode()
    req.add_header("Authorization", "Basic " + tok)
mode = os.environ.get("MODE", "")
quiet = mode in ("quiet", "busy")
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(req, timeout=10) as r:
        d = json.loads(r.read().decode("utf-8"))
    # 库是在服务后台线程里检测的：等这一轮测完再报（最多 20 秒），不然会显示「检测中」
    if mode != "busy":
        import time
        req.full_url = url.replace("?refresh=1", "")
        for _ in range(40):
            if not d.get("checking"):
                break
            time.sleep(0.5)
            with opener.open(req, timeout=10) as r:
                d = json.loads(r.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    if not quiet:
        print(f"  地址      http://{host}:{port}/")
        print("  检测      ✗ 访问密码不对（401）" if e.code == 401 else f"  检测      ✗ HTTP {e.code}")
    sys.exit(1 if mode == "busy" else 4 if e.code == 401 else 3)
except Exception as e:                                  # noqa: BLE001
    if not quiet:
        print(f"  地址      http://{host}:{port}/")
        print(f"  检测      ✗ 连不上：{e}")
    sys.exit(1 if mode == "busy" else 3)
if not quiet:
    print(f"  地址      http://{host}:{port}/")
    mark = {True: "✓", False: "✗", None: "·"}
    for c in d.get("checks", []):
        label = c["label"] + "  " * max(0, 4 - len(c["label"]))     # 中文按两个字符宽对齐
        print(f"  {label}  {mark[c['ok']]} {c['detail']}")
if mode == "busy":
    sys.exit(0 if any(c.get("key") == "runner" and str(c.get("detail", "")).startswith("正在执行")
                      for c in d.get("checks", [])) else 1)
sys.exit(0 if d.get("ok") else 2)
PY
}

# ---------------------------------------------------------------- 各个命令
do_status() {
    local pid
    if ! pid=$(find_pid); then
        echo "● 指标计算服务：没有在运行"
        [ -f "$PID_FILE" ] && rm -f "$PID_FILE"
        return 1
    fi
    echo "● 指标计算服务：运行中（PID $pid，已运行 $(ps -p "$pid" -o etime= | tr -d ' ')）"
    health
    local rc=$?
    case $rc in
        0) echo "  结论      全部正常" ;;
        2) echo "  结论      服务在运行，但有依赖不可用（看上面打 ✗ 的项）"; return 2 ;;
        *) echo "  结论      进程在，但页面接口没有响应（看 $OUT_FILE）"; return 2 ;;
    esac
    return 0
}

do_start() {
    local pid
    if pid=$(find_pid); then
        echo "已经在运行（PID $pid），不重复启动。要重启用：bash scripts/service.sh restart"
        return 0
    fi
    mkdir -p "$APP_DIR/logs" "$APP_DIR/service_data"
    echo "启动指标计算服务……（python：$PYTHON${SERVE_ARGS:+，参数：$SERVE_ARGS}）"
    local mark
    mark="===== $(date '+%F %T') 启动（$$）====="
    echo "$mark" >> "$OUT_FILE"
    # shellcheck disable=SC2086
    nohup "$PYTHON" -m engin_cli.cli serve $SERVE_ARGS >> "$OUT_FILE" 2>&1 < /dev/null &
    pid=$!
    echo "$pid" > "$PID_FILE"
    # 等到接口有响应（最多 30 秒）；进程中途退出就是启动失败
    for _ in $(seq 1 60); do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$PID_FILE"
            echo "✗ 启动失败，最后的输出（完整日志：$OUT_FILE）："
            awk -v m="$mark" 'f{print} $0==m{f=1}' "$OUT_FILE" | tail -n 15 | sed 's/^/    /'
            return 1
        fi
        if health quiet; [ $? -ne 3 ]; then
            echo "✓ 已启动（PID $pid）"
            health; local rc=$?
            [ $rc -eq 2 ] && echo "  注意：有依赖不可用（看上面打 ✗ 的项），服务本身已经起来了"
            return 0
        fi
        sleep 0.5
    done
    echo "✗ 进程在（PID $pid），但 30 秒内页面接口一直没有响应，看日志：$OUT_FILE"
    return 1
}

do_stop() {
    local pid
    if ! pid=$(find_pid); then
        echo "没有在运行"
        rm -f "$PID_FILE"
        return 0
    fi
    if [ "$FORCE" -ne 1 ] && health busy >/dev/null 2>&1; then
        echo "✗ 正在执行跑批，现在停会把它中断（执行记录标成「服务重启，执行被中断」）。"
        echo "  等它跑完再停，或者确认要停：bash scripts/service.sh $ACTION -f"
        return 1
    fi
    echo "停止指标计算服务（PID $pid）……"
    kill -TERM "$pid" 2>/dev/null
    for _ in $(seq 1 30); do
        kill -0 "$pid" 2>/dev/null || { rm -f "$PID_FILE"; echo "✓ 已停止"; return 0; }
        sleep 0.5
    done
    echo "15 秒没有退出，强制结束"
    pkill -KILL -P "$pid" 2>/dev/null          # 连同它起的跑批子进程
    kill -KILL "$pid" 2>/dev/null
    rm -f "$PID_FILE"
    echo "✓ 已停止（强制）"
    return 0
}

case "$ACTION" in
    status|check) do_status ;;
    start)        do_start ;;
    stop)         do_stop ;;
    restart)      do_stop && do_start ;;
    -h|--help|help) sed -n '2,24p' "$0" ;;
    *) echo "用法：bash scripts/service.sh {status|start|restart|stop} [-f]"; exit 1 ;;
esac
