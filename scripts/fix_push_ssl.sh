#!/usr/bin/env bash
# 修复推送报 [SSL: CERTIFICATE_VERIFY_FAILED]（certificate has expired 等）
#
# 用 root 执行：
#   bash scripts/fix_push_ssl.sh             # 只修复 + 验证
#   bash scripts/fix_push_ssl.sh --repush    # 修好后顺便把没推出去的 drill,macro 补推
#
# 可选参数 / 环境变量：
#   --app-dir DIR   项目目录，默认 /www/wwwroot/opinion_metric_engine   （APP_DIR）
#   --user NAME     跑批用户，默认 opinion                             （RUN_USER）
#   --host HOST     推送域名，默认从 settings 的 push_url 里取            （HOST）
#   --port PORT     默认 443                                            （PORT）
#   --tables T      --repush 补推哪些表，默认 drill,macro                （TABLES）
#
# 做了什么（按顺序，哪一步好了就停）：
#   1. 看本机时间，能开 NTP 就开（时间不对，证书一定校验失败）
#   2. 更新系统根证书 ca-certificates（apt / yum / dnf 自动识别）
#   3. 更新项目虚拟环境里的 certifi
#   4. 用跟推送一模一样的方式（Python urllib）连一次推送地址：
#        · 通了                              → 修好了
#        · 系统根证书不行、certifi 的可以    → 在 .venv/bin/activate 里设 SSL_CERT_FILE 指向 certifi，
#                                              定时任务 source 这个文件，之后推送都用新的根证书
#        · 都不行                            → 对方服务器证书链有问题，打印证书链，找对方运维
# 全程**不关闭证书校验**。

set -u

APP_DIR=${APP_DIR:-/www/wwwroot/opinion_metric_engine}
RUN_USER=${RUN_USER:-opinion}
HOST=${HOST:-}
PORT=${PORT:-443}
TABLES=${TABLES:-drill,macro}
REPUSH=0

while [ $# -gt 0 ]; do
    case "$1" in
        --app-dir) APP_DIR=$2; shift 2 ;;
        --user)    RUN_USER=$2; shift 2 ;;
        --host)    HOST=$2; shift 2 ;;
        --port)    PORT=$2; shift 2 ;;
        --tables)  TABLES=$2; shift 2 ;;
        --repush)  REPUSH=1; shift ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "不认识的参数：$1（--help 看用法）"; exit 2 ;;
    esac
done

PY="$APP_DIR/.venv/bin/python"
ACTIVATE="$APP_DIR/.venv/bin/activate"
MARK="# opinion_metric_engine fix_push_ssl.sh"     # 写进 activate 那一行的行尾标记，重跑时按它替换

ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }
fail() { echo "  ✗ $*"; }
step() { echo; echo "== $* =="; }

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 root 执行（要更新系统根证书）"; exit 2
fi
if [ ! -x "$PY" ]; then
    echo "找不到 $PY —— 用 --app-dir 指定项目目录"; exit 2
fi

# 以跑批用户身份执行（文件属主跟定时任务一致）；用户不存在就直接执行
as_user() {
    if id "$RUN_USER" >/dev/null 2>&1 && [ "$RUN_USER" != "root" ]; then
        su - "$RUN_USER" -s /bin/bash -c "$1"
    else
        bash -c "$1"
    fi
}

if [ -z "$HOST" ]; then
    URL=$(cd "$APP_DIR" && "$PY" -c "from engin_cli.settings import load_settings; print(load_settings().push_url or '')" 2>/dev/null)
    HOST=$(echo "$URL" | sed -E 's#^[a-zA-Z]+://##; s#[/:].*$##')
    HOST=${HOST:-data.12301dev.com}
fi
echo "推送域名：$HOST:$PORT    项目：$APP_DIR    跑批用户：$RUN_USER"

# Python urllib 连一次（跟推送同一条路）。有 HTTP 响应（哪怕 404/405）就说明证书校验过了。
# $1 = 根证书文件（空 = Python 默认的系统根证书）
py_check() {
    # 去掉 SSL_CERT_FILE / SSL_CERT_DIR：测的是「系统根证书」或「指定的根证书文件」本身
    env -u SSL_CERT_FILE -u SSL_CERT_DIR "$PY" - "$HOST" "$PORT" "${1:-}" <<'PYEOF'
import ssl, sys, urllib.error, urllib.request
host, port, cafile = sys.argv[1], sys.argv[2], sys.argv[3]
ctx = ssl.create_default_context(cafile=cafile or None)
try:
    urllib.request.urlopen(f"https://{host}:{port}/", timeout=15, context=ctx)
except urllib.error.HTTPError:
    pass
except Exception as e:
    print(f"    {e}")
    sys.exit(1)
PYEOF
}

# ---------------------------------------------------------------- 1. 时间
step "1. 本机时间"
echo "  $(date '+%F %T %Z')"
if command -v timedatectl >/dev/null 2>&1; then
    timedatectl set-ntp true >/dev/null 2>&1 && ok "已开启 NTP 自动对时" || warn "开启 NTP 失败，请自行确认时间准确"
    timedatectl 2>/dev/null | grep -Ei "synchronized|NTP" | sed 's/^/  /'
else
    warn "没有 timedatectl，请自行确认上面的时间准确"
fi

# ---------------------------------------------------------------- 2. 系统根证书
step "2. 更新系统根证书"
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq --only-upgrade ca-certificates \
        && update-ca-certificates >/dev/null 2>&1; rc=$?
elif command -v dnf >/dev/null 2>&1; then
    dnf -y -q update ca-certificates && update-ca-trust extract; rc=$?
elif command -v yum >/dev/null 2>&1; then
    yum -y -q update ca-certificates && update-ca-trust extract; rc=$?
else
    rc=1; warn "不认识的包管理器，跳过（手动更新 ca-certificates）"
fi
[ "$rc" -eq 0 ] && ok "ca-certificates 已是最新" || warn "更新 ca-certificates 失败（网络 / 软件源问题），继续后面的步骤"

# ---------------------------------------------------------------- 3. certifi
step "3. 更新虚拟环境里的 certifi"
as_user "'$PY' -m pip install -q -U certifi" || "$PY" -m pip install -q -U certifi \
    || warn "pip 更新 certifi 失败（网络 / 源问题），用现有版本继续"
CERTIFI=$("$PY" -c "import certifi; print(certifi.where())" 2>/dev/null)
if [ -n "$CERTIFI" ]; then
    ok "certifi $("$PY" -c 'import certifi; print(certifi.__version__)')：$CERTIFI"
else
    warn "虚拟环境里没有 certifi"
fi

# ---------------------------------------------------------------- 4. 验证
step "4. 用推送同样的方式连 $HOST"
FIXED=""
if py_check ""; then
    ok "系统根证书校验通过"
    FIXED="system"
elif [ -n "$CERTIFI" ] && py_check "$CERTIFI"; then
    warn "系统根证书仍然不行，certifi 的可以 → 让推送改用 certifi 根证书"
    sed -i "/$MARK\$/d" "$ACTIVATE"                       # 重跑时先删掉上次写的那行
    printf 'export SSL_CERT_FILE="%s"  %s\n' "$CERTIFI" "$MARK" >> "$ACTIVATE"
    ok "已写入 $ACTIVATE：export SSL_CERT_FILE=$CERTIFI"
    echo "    定时任务里 source .venv/bin/activate 之后就生效；手动推送前也要先 source 一次"
    FIXED="certifi"
else
    fail "系统根证书和最新 certifi 都校验不过 —— 问题在对方服务器的证书链，本机修不了"
    echo "  对方下发的证书链（找有没有 NotAfter 早于今天的那张）："
    echo | openssl s_client -connect "$HOST:$PORT" -servername "$HOST" -showcerts 2>/dev/null \
        | grep -E "^ *[0-9]+ s:|^ *i:|NotAfter|Verify return" | sed 's/^/    /'
    echo "  → 请对方运维修正证书链（去掉过期的中间 / 交叉签名证书），修好后再执行本脚本 --repush"
    exit 1
fi

# ---------------------------------------------------------------- 5. 补推
if [ "$REPUSH" -eq 1 ]; then
    step "5. 补推没推出去的数据（$TABLES）"
    as_user "cd '$APP_DIR' && source .venv/bin/activate && python -m engin_cli.cli push --tables '$TABLES'"
    exit $?
fi

echo
echo "修复完成（$([ "$FIXED" = system ] && echo 系统根证书 || echo certifi 根证书)）。补推没推出去的数据："
echo "  bash $0 --repush"
echo "或：su - $RUN_USER，cd $APP_DIR，source .venv/bin/activate，python -m engin_cli.cli push --tables $TABLES"
