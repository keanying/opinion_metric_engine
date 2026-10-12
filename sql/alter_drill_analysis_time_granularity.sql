-- 下钻表新增 time_granularity（客户 2026-10：下钻表与大盘表所有周期都要对得上）
--
-- 已经建过下钻表的库执行一次即可；新建表直接用 ads_drill_analysis_ddl.sql。下游推送目标表同样要加。
--
-- 口径变化：travel_date 从「评论所在日」改为「跑数日期」（同大盘表），每个周期各存一份补齐后的明细。
-- 旧数据口径不同，执行完后把要看的日期用 repair / run 重跑一遍（按 scenic_id + travel_date 先删后写，
-- 会把这些日期的旧行一起删掉）；下游推送表建议先清空下钻表再重推，detail_uk 的算法也变了。

alter table ads_trf_social_opinion_drill_analysis_di
    add column time_granularity varchar(32) default 'td' not null
        comment '周期粒度，同大盘表 time_granularity（td/latest_1d/latest_7d/wtd/latest_30d/mtd/latest_60d/latest_90d/qtd）'
        after travel_date,
    modify column travel_date int not null comment '跑数日期（yyyyMMdd），同大盘表 travel_date',
    drop index idx_drill_analysis_query,
    add index idx_drill_analysis_query (scenic_id, travel_date, time_granularity, emotion_word);
