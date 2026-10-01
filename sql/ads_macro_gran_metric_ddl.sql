-- 需求 2.0：舆情大盘不同周期粒度 KPI 指标表
--
-- 一行 = 景区 × 日期 × 周期粒度 × 渠道。看板「总览」页的周期页签 + 平台下拉，
-- 选中的就是这一行：
--
--   select * from ads_trf_social_opinion_macro_gran_metric_di
--    where scenic_id = 'PFTSCA01002434' and travel_date = 20260917
--      and time_granularity = 'latest_7d' and channel = 'all';
--
-- 口径（详见 README「大盘 KPI 表」与 engin_cli/metric_calc_domain.py §1.10 / 第 10 章）：
--   · 占比 / 环比 / 同比全部是**百分数**（× 100，6 位小数），其余四张 ADS 表是 [0,1] 比率，别混用
--   · 分母为 0 时环比 / 同比 / 占比取 0
--   · channel = 'all' 的行 = 各渠道相加；comment_rate 在 all 行恒为 0
--   · 近 N 日周期按渠道做缺数回补（与 core/platform 表一致），日历周期与同比不回补
--
-- 幂等：uk_macro_gran_metric 唯一，跑批走 INSERT ... ON DUPLICATE KEY UPDATE。
-- 景区列叫 scenic_id / scenic_name（需求原文，与下钻表一致），不是 scenic_spot_*。

create table if not exists ads_trf_social_opinion_macro_gran_metric_di
(
    id                          bigint auto_increment comment '自增主键' primary key,

    scenic_id                   varchar(64)     default ''  not null comment '景区ID',
    scenic_name                 varchar(255)    default ''  null comment '景区名称',
    channel                     varchar(32)                 not null comment '社媒渠道标识 ctrip|douyin|kuaishou|tongcheng|weibo|xiaohongshu|all',
    channel_name                varchar(64)     default ''  null comment '社媒渠道名称 携程|抖音|快手|同程|微博|小红书|整体',
    time_granularity            varchar(32)                 not null comment '周期粒度 today今日/latest_1d近一日/latest_7d近7日/this_week本周/latest_30d近30日/this_month本月/latest_60d近60日/latest_90d近90日/this_quarter本季度（可配置）',

    overall_sentiment_score     decimal(10, 6)  default 0   null comment '本期游客综合情感得分 S=5×(好评率×w好+中评率×w中+差评率×w差)，权重可配，默认 1.0/0.9/0.5',
    pre_overall_sentiment_score decimal(10, 6)  default 0   null comment '上期游客综合情感得分（同一公式）',

    comment_total               bigint          default 0   null comment '总评论数：all=全平台，非 all=该渠道',
    comment_rate                decimal(18, 6)  default 0   null comment '评论占比：all=0；非 all=渠道总评数×100/全渠道总评论数',
    comment_total_yoy           decimal(18, 6)  default 0   null comment '总评数同比 (本期-去年同期)×100/去年同期',
    comment_total_mom           decimal(18, 6)  default 0   null comment '总评数环比 (本期-上期)×100/上期',

    positive_comment_cnt        bigint          default 0   null comment '好评数',
    neutral_comment_cnt         bigint          default 0   null comment '中评数',
    negative_comment_cnt        bigint          default 0   null comment '差评数',
    positive_comment_rate       decimal(18, 6)  default 0   null comment '好评占比 好评数×100/总评数',
    neutral_comment_rate        decimal(18, 6)  default 0   null comment '中评占比 中评数×100/总评数',
    negative_comment_rate       decimal(18, 6)  default 0   null comment '差评占比 差评数×100/总评数',
    positive_comment_mom        decimal(18, 6)  default 0   null comment '好评环比 (本期-上期)×100/上期',
    neutral_comment_mom         decimal(18, 6)  default 0   null comment '中评环比',
    negative_comment_mom        decimal(18, 6)  default 0   null comment '差评环比',
    positive_comment_yoy        decimal(18, 6)  default 0   null comment '好评同比 (本期-去年同期)×100/去年同期',
    neutral_comment_yoy         decimal(18, 6)  default 0   null comment '中评同比',
    negative_comment_yoy        decimal(18, 6)  default 0   null comment '差评同比',

    dimension_breakdown         text                        null comment '维度明细 JSON：[{"dimension1","dimension1Score","preDimension1","preDimension1Score"}]，6 个一级维度全量',
    wordcloud_map               text                        null comment '词云 JSON：{"positiveWord":[{"word","rate"}],"neutralWord":[...],"negativeWord":[...]}，各 Top30，rate=次数×100/周期内总关键词数',
    period_comment_heatmap      text                        null comment '周期评论热力地图 JSON：[{"region","heat"}]，heat=评论数，region=省级简称',

    publish_time                varchar(32)                 null comment '发布时间（默认 yyyyMMdd，同 travel_date）',
    travel_date                 int                         not null comment '分区（yyyyMMdd）',
    etl_time                    datetime        default CURRENT_TIMESTAMP null comment '数据加工时间',

    constraint uk_macro_gran_metric unique (scenic_id, travel_date, time_granularity, channel)
)
    comment '舆情大盘不同周期粒度KPI指标' charset = utf8mb4 row_format = DYNAMIC;

-- 看板按日期拉全部景区时走这条
create index idx_macro_gran_metric_date
    on ads_trf_social_opinion_macro_gran_metric_di (travel_date, scenic_id);
