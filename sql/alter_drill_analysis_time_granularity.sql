-- 下钻表新增 time_granularity、调整查询索引（客户 2026-10）
--
-- 已经建过下钻表的库执行一次即可；新建表直接用 ads_drill_analysis_ddl.sql。下游推送目标表同样要加列。
--
-- 下钻表按天存一份补齐后的明细，time_granularity 固定写 td；看板选某个周期时，
-- 用 travel_date BETWEEN 本期首日 AND 本期末日 查（见 sql/queries.sql）。
-- 补数复制来的评论 travel_date = 补到的那天、publish_time = 那天 00:00:00。
-- 执行完后把要看的日期用 repair 重跑一遍（按 scenic_id + travel_date 先删后写）；
-- detail_uk 算法变了，下游下钻表建议清空后重推。
--
-- 如果之前已经执行过本文件的旧版本（time_granularity 列已存在），只执行最后的索引部分即可。

alter table ads_trf_social_opinion_drill_analysis_di
    add column time_granularity varchar(32) default 'td' not null
        comment '固定 td（按天存；周期用 travel_date 区间查）'
        after travel_date;

alter table ads_trf_social_opinion_drill_analysis_di
    drop index idx_drill_analysis_query,
    add index idx_drill_analysis_query (scenic_id, emotion_word, travel_date),
    add index idx_drill_analysis_date (scenic_id, travel_date);
