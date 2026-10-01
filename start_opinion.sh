#!/bin/bash

# 切换到 opinion 用户并执行后续命令
su - opinion <<'EOF'
cd /www/wwwroot/opinion_metric_engine || exit 1
source .venv/bin/activate
# 获取当前日期，格式 YYYYMMDD
CURRENT_DATE=$(date +%Y%m%d)
python -m engin_cli.cli run --push all --scenic PFTSCE01001721 --start-date $CURRENT_DATE --end-date $CURRENT_DATE
python -m engin_cli.cli run --push all --scenic PFTSCA01018066 --start-date $CURRENT_DATE --end-date $CURRENT_DATE
EOF
