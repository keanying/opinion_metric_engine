-- 指标计算服务（python -m engin_cli.cli serve）的系统表，MySQL。
--
-- 命名沿用数仓规范：分层_业务域_实体名_粒度，全小写下划线，表和字段都带注释。
--   · 业务域 trf（流量、渠道），实体前缀和 ADS 表一致：trf_social_opinion_*
--   · 配置类（任务、服务设置）：dim_ 维度表，非分区全量 _f
--   · 流水类（执行记录、调度日志）：dwd_ 明细表，天级增量 _di，分区字段 travel_date（YYYYMMDD）
--
-- 库：和计算指标用的是同一个库（settings_local.py 里 DB 的 database），统一维护。
-- 服务启动时表不存在就自动执行本文件建表（代码里一律写「库.表」）；
-- 账号没有建表权限时，让 DBA 在那个库里手工执行本文件。
--
-- 状态类字段的取值和含义都写在注释里。JSON 字段是页面 / 服务读写的结构化配置，不做业务统计。

-- ════════════════════════════════════════════════════════════════════
-- 指标计算任务配置（一行 = 一个任务）
-- ════════════════════════════════════════════════════════════════════
create table if not exists dim_trf_social_opinion_task_f
(
    task_id          bigint auto_increment comment '任务ID（自增）' primary key,
    task_name        varchar(128)  default ''  not null comment '任务名称，如：九嶷山每日跑批',
    task_status      tinyint       default 1   not null comment '任务状态：1-启用（到点自动执行） 0-停用（只能手动执行）',
    task_config      text                      null comment '跑批参数 JSON：scenics-景区编码（逗号分隔，空=全部景区） tables-计算哪些表（空=全部） date_mode-目标日期方式（today-当天 yesterday-昨天 recent-近N天 range-固定区间） recent_days/start_date/end_date-日期参数 push-推送哪些表（空=不推送） skip_db-只算不写库 drill_keyword_filter-下钻表按采集关键字过滤 quality_gate-质量检测不通过就回退 quality_retries-重算次数 rerun_days-往前重跑天数 backfill_lookback-补数上限 verbose-详细日志',
    schedule_config  text                      null comment '定时 JSON：{"times":["08:10","20:10"]} 每天这些时刻（服务器本地时间）自动执行；times 为空=只手动执行',
    last_fire_config text                      null comment '各定时时刻最近一次触发的日期 JSON：{"08:10":"2026-11-02"}，保证同一时刻一天只触发一次',
    create_time      datetime                  not null comment '创建时间',
    update_time      datetime                  not null comment '最后修改时间'
)
    comment '舆情指标计算服务-任务配置' charset = utf8mb4 row_format = DYNAMIC;

-- ════════════════════════════════════════════════════════════════════
-- 服务级设置（一行 = 一个设置项）
-- ════════════════════════════════════════════════════════════════════
create table if not exists dim_trf_social_opinion_service_setting_f
(
    setting_code  varchar(64)  not null comment '设置项编码：scheduler-定时总开关 push-推送设置 notify-飞书通知设置' primary key,
    setting_value text         null comment '设置内容 JSON。scheduler：{"enabled":true}（true-定时开启 false-定时暂停，只能手动执行）；push：{"enabled":推送总开关（false-任务只算不推）,"push_url":推送地址,"push_batch_size":每批行数,"push_timeout":超时秒,"push_retries":重试次数,"push_dry_run":试推,"push_strict":推送失败算执行失败}，值为空=用 settings_local.py 的配置',
    update_time   datetime     not null comment '最后修改时间'
)
    comment '舆情指标计算服务-服务级设置' charset = utf8mb4 row_format = DYNAMIC;

-- ════════════════════════════════════════════════════════════════════
-- 任务执行记录（一行 = 一次执行：跑批 / 补推 / 通知测试）
-- ════════════════════════════════════════════════════════════════════
create table if not exists dwd_trf_social_opinion_task_run_di
(
    run_id       bigint auto_increment comment '执行ID（自增，页面上的 #编号）' primary key,
    travel_date  int                         not null comment '分区日期 YYYYMMDD：执行记录创建当天',
    task_id      bigint                      null comment '任务ID（dim_trf_social_opinion_task_f.task_id）；临时执行 / 补推 / 通知测试为空',
    task_name    varchar(128) default ''     not null comment '任务名称（创建执行时的快照，任务改名 / 删除不影响）',
    run_type     varchar(16)  default 'run'  not null comment '执行类型：run-跑批（计算+写库+推送） push-补推（不重算，从库里读算好的结果再推） notify-飞书通知测试',
    trigger_type varchar(16)  default 'manual' not null comment '触发方式：schedule-定时触发 manual-页面手动',
    run_status   varchar(16)  default 'queued' not null comment '执行状态：queued-排队中 running-执行中 success-成功 failed-失败 blocked-数据质量检测不通过已回退（未写库、未推送） cancelled-已取消',
    command_text text                        null comment '执行的 engin_cli 命令参数 JSON 数组，如 ["run","--start-date","20261102",…]',
    run_title    varchar(512) default ''     not null comment '一句话说明：景区 + 目标日期',
    create_time  datetime                    not null comment '创建（提交）时间',
    start_time   datetime                    null comment '开始执行时间；排队中为空',
    finish_time  datetime                    null comment '结束时间；未结束为空',
    exit_code    int                         null comment '子进程退出码：0-成功 1-有景区失败或质量检测不通过 2-参数/配置错误；未结束为空',
    log_path     varchar(512) default ''     not null comment '执行日志文件路径（服务所在服务器本地 service_data/runs/<run_id>.log）',
    summary_json mediumtext                  null comment '执行结果摘要 JSON（run --summary-json）：各景区成败、质量检测次数、各表产出 / 写入 / 推送行数、错误信息',
    run_message  varchar(1000) default ''    not null comment '失败原因 / 备注，如：退出码 1、执行中被取消、服务重启执行被中断'
)
    comment '舆情指标计算服务-任务执行记录' charset = utf8mb4 row_format = DYNAMIC;

create index idx_task_run_date on dwd_trf_social_opinion_task_run_di (travel_date);
create index idx_task_run_task on dwd_trf_social_opinion_task_run_di (task_id, run_id);
create index idx_task_run_status on dwd_trf_social_opinion_task_run_di (run_status);

-- ════════════════════════════════════════════════════════════════════
-- 调度日志（一行 = 一条：定时触发了谁、跳过了谁、为什么）
-- ════════════════════════════════════════════════════════════════════
create table if not exists dwd_trf_social_opinion_schedule_log_di
(
    log_id      bigint auto_increment comment '日志ID（自增）' primary key,
    travel_date int                       not null comment '分区日期 YYYYMMDD：日志产生当天',
    log_time    datetime                  not null comment '日志时间',
    task_id     bigint                    null comment '任务ID；服务级事件（如定时总开关、推送总开关变化）为空',
    task_name   varchar(128) default ''   not null comment '任务名称（快照）',
    log_level   varchar(8)   default 'info' not null comment '日志级别：info-信息（到点触发） warn-警告（跳过错过的时刻、开关关闭） error-错误（任务配置有误没有执行）',
    log_content varchar(1000) default ''  not null comment '日志内容，如：08:10 到点，已触发执行 #12；跳过 09:05：服务当时没在运行'
)
    comment '舆情指标计算服务-调度日志' charset = utf8mb4 row_format = DYNAMIC;

create index idx_schedule_log_date on dwd_trf_social_opinion_schedule_log_di (travel_date);
create index idx_schedule_log_task on dwd_trf_social_opinion_schedule_log_di (task_id, log_id);

-- ════════════════════════════════════════════════════════════════════
-- 数据质量检测记录（一行 = 一次检测：跑批时每个景区自动检测一次 / 页面上手动检测一次）
-- ════════════════════════════════════════════════════════════════════
create table if not exists dwd_trf_social_opinion_quality_check_di
(
    check_id      bigint auto_increment comment '检测记录ID（自增）' primary key,
    travel_date   int                         not null comment '分区日期 YYYYMMDD：检测当天',
    check_type    varchar(16)  default 'run'  not null comment '检测方式：run-跑批时自动检测（算完写库前） manual-数据质量页面手动检测（读库里已有的大盘表）',
    trigger_type  varchar(16)  default ''     not null comment '触发方式：schedule-定时跑批 manual-手动执行 repair-数据修复 page-页面检测',
    run_id        bigint                      null comment '跑批执行ID（dwd_trf_social_opinion_task_run_di.run_id）；页面检测为空',
    task_id       bigint                      null comment '任务ID；临时执行 / 页面检测为空',
    task_name     varchar(128) default ''     not null comment '任务名称（快照）',
    scenic_id     varchar(64)  default ''     not null comment '景区编码',
    start_date    int                         null comment '检测的目标日期起 YYYYMMDD',
    end_date      int                         null comment '检测的目标日期止 YYYYMMDD',
    check_status  varchar(16)  default 'pass' not null comment '检测结果：pass-通过 fixed-重算后通过 blocked-不通过已回退（未写库、未推送） fail-不通过（关了闸门照常写库 / 页面检测发现问题）',
    attempt_cnt   int          default 1      not null comment '检测次数：跑批不通过会重新取数重算再检测',
    item_cnt      int          default 0      not null comment '检查了几项（核心表、渠道表、区间一致 …）',
    issue_cnt     int          default 0      not null comment '发现的问题数（最后一次检测）',
    check_summary varchar(1000) default ''    not null comment '一句话结果，如：全部通过；区间相同的行指标不一致，3 处',
    check_detail  mediumtext                  null comment '检测过程 JSON：每次检测的每一项查了什么（规则）、核对了多少行、哪些不对、对比的数值',
    operator      varchar(64)  default ''     not null comment '操作人：页面检测 / 手动执行时的登录用户；定时为空',
    create_time   datetime                    not null comment '检测时间'
)
    comment '舆情指标计算服务-数据质量检测记录' charset = utf8mb4 row_format = DYNAMIC;

create index idx_quality_check_date on dwd_trf_social_opinion_quality_check_di (travel_date);
create index idx_quality_check_scenic on dwd_trf_social_opinion_quality_check_di (scenic_id, check_id);
create index idx_quality_check_run on dwd_trf_social_opinion_quality_check_di (run_id);
