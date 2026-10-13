#!/bin/bash

# 切换到 opinion 用户并执行后续命令
su - opinion <<'EOF'
cd /www/wwwroot/opinion_metric_engine || exit 1
source .venv/bin/activate

# 结束日期：今天 (YYYYMMDD)
END_DATE=$(date +%Y%m%d)

# 开始日期：今天向前推40天 (YYYYMMDD)
START_DATE=$(date -d "40 days ago" +%Y%m%d)

python -m engin_cli.cli run --push drill,macro --scenic PFTSCE01001721 --start-date $START_DATE --end-date $END_DATE

python -m engin_cli.cli run --push drill,macro --scenic PFTSCA01018066 --start-date $START_DATE --end-date $END_DATE

python -m engin_cli.cli run --push drill,macro --scenic PFTSCA01016184 --start-date $START_DATE --end-date $END_DATE

EOF