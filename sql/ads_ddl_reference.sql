-- 四张 ADS 目标表的建表语句（来自业务方，原样保留，不要在这里改口径）
--
-- builders/*.py 的 COLUMNS 必须与本文件逐列一致，
-- tests/test_schema.py 会解析本文件做断言 —— 表结构一变，测试立刻红。

create table ads_trf_social_opinion_comment_core_di
(
    id                        bigint auto_increment comment '自增主键' primary key,

    scenic_spot_name          varchar(255)                             null comment '景区名称',
    scenic_spot_code          varchar(64)                              null comment '景区编号',
    comment_count             bigint                                   null comment '总评论数',
    positive_count            bigint                                   null comment '好评数',
    neutral_count             bigint                                   null comment '中评数',
    negative_count            bigint                                   null comment '差评数',
    
    positive_growth_rate      decimal(10, 4)                           null comment '近1日好评环比增长率',
    positive_growth_rate_7d   decimal(10, 4)                           null comment '近7日好评环比增长率',
    positive_growth_rate_30d  decimal(10, 4)                           null comment '近30日好评环比增长率', 
    positive_growth_rate_60d  decimal(10, 4) default 0.0000            null comment '近60日好评环比增长率',
    positive_growth_rate_90d  decimal(10, 4) default 0.0000            null comment '近90日好评环比增长率',
    positive_growth_rate_365d decimal(10, 4) default 0.0000            null comment '近365日好评环比增长率',
    

    neutral_growth_rate       decimal(10, 4)                           null comment '近1日中评环比增长率',
    neutral_growth_rate_7d    decimal(10, 4)                           null comment '近7日中评环比增长率',
    neutral_growth_rate_30d   decimal(10, 4)                           null comment '近30日中评环比增长率',
    neutral_growth_rate_60d   decimal(10, 4) default 0.0000            null comment '近60日中评环比增长率',
    neutral_growth_rate_90d   decimal(10, 4) default 0.0000            null comment '近90日中评环比增长率',
    neutral_growth_rate_365d  decimal(10, 4) default 0.0000            null comment '近365日中评环比增长率',
    
    negative_growth_rate      decimal(10, 4)                           null comment '近1日差评环比增长率',
    negative_growth_rate_7d   decimal(10, 4)                           null comment '近7日差评环比增长率',
    negative_growth_rate_30d  decimal(10, 4)                           null comment '近30日差评环比增长率',
    negative_growth_rate_60d  decimal(10, 4) default 0.0000            null comment '近60日差评环比增长率',
    negative_growth_rate_90d  decimal(10, 4) default 0.0000            null comment '近90日差评环比增长率',
    negative_growth_rate_365d decimal(10, 4) default 0.0000            null comment '近365日差评环比增长率',
    
    daily_rating              decimal(10, 4)                           null comment '统计时刻均分',
    emotional_score           decimal(10, 4)                           null comment '近1日游客综合情感得分',
    emotional_score_7d        decimal(10, 4) default 0.0000            null comment '近7日游客综合情感得分',
    emotional_score_30d       decimal(10, 4) default 0.0000            null comment '近30日游客综合情感得分',
    emotional_score_60d       decimal(10, 4) default 0.0000            null comment '近60日游客综合情感得分',
    emotional_score_90d       decimal(10, 4) default 0.0000            null comment '近90日游客综合情感得分',
    emotional_score_365d      decimal(10, 4) default 0.0000            null comment '近365日游客综合情感得分',

    comment_count_dod         decimal(10, 4) default 0.0000            null comment '近1日评论数环比',
    comment_count_wow         decimal(10, 4) default 0.0000            null comment '近7日评论数环比',
    comment_count_mom         decimal(10, 4) default 0.0000            null comment '近30日评论数环比',
    comment_count_60dod       decimal(10, 4) default 0.0000            null comment '近60日评论数环比',
    comment_count_90dod       decimal(10, 4) default 0.0000            null comment '近90日评论数环比',
    comment_count_365dod      decimal(10, 4) default 0.0000            null comment '近365日评论数环比',
    
    positive_rate             decimal(10, 4)                           null comment '近1日好评率',
    positive_rate_7d          decimal(10, 4)                           null comment '近7日好评率',
    positive_rate_30d         decimal(10, 4)                           null comment '近30日好评率',
    positive_rate_60d         decimal(10, 4) default 0.0000            null comment '近60日好评率',
    positive_rate_90d         decimal(10, 4) default 0.0000            null comment '近90日好评率',
    positive_rate_365d        decimal(10, 4) default 0.0000            null comment '近365日好评率',
    
    neutral_rate              decimal(10, 4)                           null comment '近1日中评率',
    neutral_rate_7d           decimal(10, 4)                           null comment '近7日中评率',
    neutral_rate_30d          decimal(10, 4)                           null comment '近30日中评率',
    neutral_rate_60d          decimal(10, 4) default 0.0000            null comment '近60日中评率',
    neutral_rate_90d          decimal(10, 4) default 0.0000            null comment '近90日中评率',
    neutral_rate_365d         decimal(10, 4) default 0.0000            null comment '近365日中评率',
    
    negative_rate             decimal(10, 4)                           null comment '近1日差评率',
    negative_rate_7d          decimal(10, 4)                           null comment '近7日差评率',
    negative_rate_30d         decimal(10, 4)                           null comment '近30日差评率',
    negative_rate_60d         decimal(10, 4) default 0.0000            null comment '近60日差评率',
    negative_rate_90d         decimal(10, 4) default 0.0000            null comment '近90日差评率',
    negative_rate_365d        decimal(10, 4) default 0.0000            null comment '近365日差评率',
    
    publish_time              varchar(32)                              null comment '评论发布时间（YYYY-MM-DD HH:mm:ss）',
    etl_time                  datetime       default CURRENT_TIMESTAMP null comment 'ETL写入时间',
    travel_date               int                                      not null comment '分区/评论时间（yyyyMMdd）',
    constraint index_code_date
        unique (scenic_spot_code, travel_date, publish_time)
)
    comment '社媒评论核心指标表' charset = utf8mb4;


create table ads_trf_social_opinion_comment_content_di
(
    id                 bigint auto_increment comment '自增主键' primary key,
    scenic_spot_code   varchar(64)                              null comment '景区编号',
    scenic_spot_name   varchar(255)                             null comment '景区名称',
    emotion_type       varchar(32)                              null comment '内容类型(positive/neutral/negative)',
    emotion_word       varchar(255)                             null comment '内容名称(负面维度/关键词)',
    emotion_rank       int                                      null comment '排名',
    
    emotion_value      bigint                                   null comment '近1日内容值(热度值/数量/出现次数)',
    emotion_value_7d   bigint         default 0                 null comment '近7日内容值(热度值/数量/出现次数)',
    emotion_value_30d  bigint         default 0                 null comment '近30日内容值(热度值/数量/出现次数)',
    emotion_value_60d  bigint         default 0                 null comment '近60日内容值(热度值/数量/出现次数)',
    emotion_value_90d  bigint         default 0                 null comment '近90日内容值(热度值/数量/出现次数)',
    emotion_value_365d bigint         default 0                 null comment '近365日内容值(热度值/数量/出现次数)',
    
    emotion_rate_1d    decimal(18, 6) default 0.000000          null comment '热度占比_近1日出现次数(热度)/当日总评论数',
    emotion_rate_7d    decimal(18, 6) default 0.000000          null comment '热度占比_近7日出现次数(热度)/近7日总评论数',
    emotion_rate_30d   decimal(18, 6) default 0.000000          null comment '热度占比_近30日出现次数(热度)/近30日总评论数',
    emotion_rate_60d   decimal(18, 6) default 0.000000          null comment '热度占比_近60日出现次数(热度)/近60日总评论数',
    emotion_rate_90d   decimal(18, 6) default 0.000000          null comment '热度占比_近90日出现次数(热度)/近90日总评论数',
    emotion_rate_365d  decimal(18, 6) default 0.000000          null comment '热度占比_近365日出现次数(热度)/近365日总评论数',
    
    publish_time       varchar(32)                              null comment '评论发布时间（YYYY-MM-DD HH:mm:ss）',
    etl_time           datetime       default CURRENT_TIMESTAMP null comment 'ETL写入时间',
    travel_date        int                                      not null comment '分区/评论时间（yyyyMMdd）'
)
    comment '内容分析表(负面词+关键词云)' charset = utf8mb4;

create table ads_trf_social_opinion_comment_dimension_score_di
(
    id                     bigint auto_increment comment '自增主键' primary key,
    
    scenic_spot_code       varchar(64)                              null comment '景区编号',
    scenic_spot_name       varchar(255)                             null comment '景区名称',
    
    dimension_level1       varchar(128)                             null comment '一级维度',
    dimension_level2       varchar(128)                             null comment '二级维度',
    dimension_level3       varchar(128)                             null comment '三级维度',
    
    dimension_1_score      decimal(10, 4)                           null comment '近1日一级维度分数',
    dimension_2_score      decimal(10, 4)                           null comment '近1日二级维度分数',
    dimension_3_score      decimal(10, 4)                           null comment '近1日三级维度分数',
    
    dimension_1_score_7d   decimal(10, 4) default 0.0000            null comment '近7日一级维度分数',
    dimension_2_score_7d   decimal(10, 4) default 0.0000            null comment '近7日二级维度分数',
    dimension_3_score_7d   decimal(10, 4) default 0.0000            null comment '近7日三级维度分数',
    
    dimension_1_score_14d  decimal(10, 4) default 0.0000            null comment '近14日一级维度分数',
    dimension_2_score_14d  decimal(10, 4) default 0.0000            null comment '近14日二级维度分数',
    dimension_3_score_14d  decimal(10, 4) default 0.0000            null comment '近14日三级维度分数',
    
    dimension_1_score_30d  decimal(10, 4) default 0.0000            null comment '近30日一级维度分数',
    dimension_2_score_30d  decimal(10, 4) default 0.0000            null comment '近30日二级维度分数',
    dimension_3_score_30d  decimal(10, 4) default 0.0000            null comment '近30日三级维度分数',
    
    dimension_1_score_60d  decimal(10, 4) default 0.0000            null comment '近60日一级维度分数',
    dimension_2_score_60d  decimal(10, 4) default 0.0000            null comment '近60日二级维度分数',
    dimension_3_score_60d  decimal(10, 4) default 0.0000            null comment '近60日三级维度分数',
    
    dimension_1_score_90d  decimal(10, 4) default 0.0000            null comment '近90日一级维度分数',
    dimension_2_score_90d  decimal(10, 4) default 0.0000            null comment '近90日二级维度分数',
    dimension_3_score_90d  decimal(10, 4) default 0.0000            null comment '近90日三级维度分数',
    
    dimension_1_score_365d decimal(10, 4) default 0.0000            null comment '近365日一级维度分数',
    dimension_2_score_365d decimal(10, 4) default 0.0000            null comment '近365日二级维度分数',
    dimension_3_score_365d decimal(10, 4) default 0.0000            null comment '近365日三级维度分数',
    
    publish_time           varchar(32)                              null comment '评论发布时间（YYYY-MM-DD HH:mm:ss）',
    etl_time               datetime       default CURRENT_TIMESTAMP null comment 'ETL写入时间',
    travel_date            int                                      not null comment '分区/评论时间（yyyyMMdd）'
)
    comment '维度标签分数表' charset = utf8mb4;

create table ads_trf_social_opinion_comment_platform_di
(
    id                       bigint auto_increment comment '自增主键' primary key,
    
    scenic_spot_code         varchar(64)                              null comment '景区编号',
    scenic_spot_name         varchar(255)                             null comment '景区名称',
    
    platform_name            varchar(64)                              null comment '平台名称',
    platform_code            varchar(64)                              null comment '平台编码',
    
    comment_rate             decimal(10, 4)                           null comment '平台评论占比',
    positive_rate            decimal(10, 4)                           null comment '平台好评占比',
    growth_rate              decimal(10, 4)                           null comment '平台环比增长率',
    
    comment_cnt              bigint                                   null comment '近1日总评论数',
    comment_cnt_7d           int            default 0                 null comment '近7日总评论数',
    comment_cnt_14d          int            default 0                 null comment '近14日总评论数',
    comment_cnt_30d          int            default 0                 null comment '近30日总评论数',
    comment_cnt_60d          int            default 0                 null comment '近60日总评论数',
    comment_cnt_90d          int            default 0                 null comment '近90日总评论数',
    comment_cnt_365d         int            default 0                 null comment '近365日总评论数',
    
    total_good_rate_wow_1d   decimal(10, 4)                           null comment '近1日(全平台)环比:(周期内好评数-上周期内好评数)/上周期内好评数',
    good_rate_wow_1d         decimal(10, 4)                           null comment '近1日(平台)环比:(周期内好评数-上周期内好评数)/上周期内好评数',
    week_total_good_rate_wow decimal(10, 4) default 0.0000            null comment '近7日(全平台)环比:(周期内好评数-上周期内好评数)/上周期内好评数',
    week_good_rate_wow       decimal(10, 4) default 0.0000            null comment '近7日(平台)环比:(周期内好评数-上周期内好评数)/上周期内好评数',
    total_good_rate_wow_14d  decimal(10, 4)                           null comment '近14日(全平台)环比:(周期内好评数-上周期内好评数)/上周期内好评数',
    good_rate_wow_14d        decimal(10, 4)                           null comment '近14日(平台)环比:(周期内好评数-上周期内好评数)/上周期内好评数',
    mon_total_good_rate_wow  decimal(10, 4) default 0.0000            null comment '近30日(全平台)环比:(周期内好评数-上周期内好评数)/上周期内好评数',
    mon_good_rate_wow        decimal(10, 4) default 0.0000            null comment '近30日(平台)环比:(周期内好评数-上周期内好评数)/上周期内好评数',
    total_good_rate_wow_60d  decimal(10, 4)                           null comment '近60日(全平台)环比:(周期内好评数-上周期内好评数)/上周期内好评数',
    good_rate_wow_60d        decimal(10, 4)                           null comment '近60日(平台)环比:(周期内好评数-上周期内好评数)/上周期内好评数',
    total_good_rate_wow_90d  decimal(10, 4) default 0.0000            null comment '近90日(全平台)环比:(周期内好评数-上周期内好评数)/上周期内好评数',
    good_rate_wow_90d        decimal(10, 4) default 0.0000            null comment '近90日(平台)环比:(周期内好评数-上周期内好评数)/上周期内好评数',
    total_good_rate_wow_365d decimal(10, 4) default 0.0000            null comment '近365日(全平台)环比:(周期内好评数-上周期内好评数)/上周期内好评数',
    good_rate_wow_365d       decimal(10, 4) default 0.0000            null comment '近365日(平台)环比:(周期内好评数-上周期内好评数)/上周期内好评数',

    positive_count           int            default 0                 null comment '近1日(全平台)好评数',
    plat_positive_count      int            default 0                 null comment '近1日(平台)好评数',
    positive_7d_count        int            default 0                 null comment '近7日(全平台)好评数',
    plat_positive_7d_count   int            default 0                 null comment '近7日(平台)好评数',
    positive_14d_count       int            default 0                 null comment '近14日(全平台)好评数',
    plat_positive_14d_count  int            default 0                 null comment '近14日(平台)好评数',
    positive_30d_count       int            default 0                 null comment '近30日(全平台)好评数',
    plat_positive_30d_count  int            default 0                 null comment '近30日(平台)好评数',
    positive_60d_count       int            default 0                 null comment '近60日(全平台)好评数',
    plat_positive_60d_count  int            default 0                 null comment '近60日(平台)好评数',
    positive_90d_count       int            default 0                 null comment '近90日(全平台)好评数',
    plat_positive_90d_count  int            default 0                 null comment '近90日(平台)好评数',
    positive_365d_count      int            default 0                 null comment '近365日(全平台)好评数',
    plat_positive_365d_count int            default 0                 null comment '近365日(平台)好评数',

    total_good_rate_1d       decimal(10, 4)                           null comment '近1日(全平台)好评占比=总评数/好评数',
    good_rate_1d             decimal(10, 4)                           null comment '近1日(平台)好评占比=总评数/好评数',
    week_total_good_rate     decimal(10, 4) default 0.0000            null comment '近7日(全平台)好评占比=总评数/好评数',
    week_good_rate           decimal(10, 4) default 0.0000            null comment '近7日(平台)好评占比=总评数/好评数',
    total_good_rate_14d      decimal(10, 4)                           null comment '近14日(全平台)好评占比=总评数/好评数',
    good_rate_14d            decimal(10, 4)                           null comment '近14日(平台)好评占比=总评数/好评数',
    mon_total_good_rate      decimal(10, 4) default 0.0000            null comment '近30日(全平台)好评占比=总评数/好评数',
    mon_good_rate            decimal(10, 4) default 0.0000            null comment '近30日(平台)好评占比=总评数/好评数',
    total_good_rate_60d      decimal(10, 4)                           null comment '近60日(全平台)好评占比=总评数/好评数',
    good_rate_60d            decimal(10, 4)                           null comment '近60日(平台)好评占比=总评数/好评数',
    total_good_rate_90d      decimal(10, 4) default 0.0000            null comment '近90日(全平台)好评占比=总评数/好评数',
    good_rate_90d            decimal(10, 4) default 0.0000            null comment '近90日(平台)好评占比=总评数/好评数',
    total_good_rate_365d     decimal(10, 4) default 0.0000            null comment '近365日(全平台)好评占比=总评数/好评数',
    good_rate_365d           decimal(10, 4) default 0.0000            null comment '近365日(平台)好评占比=总评数/好评数',
    
    
    etl_time                 datetime       default CURRENT_TIMESTAMP null comment 'ETL写入时间',
    publish_time             varchar(32)                              null comment '评论发布时间（YYYY-MM-DD HH:mm:ss）',
    travel_date              int                                      not null comment '分区/评论时间（yyyyMMdd）',
    
    constraint index_code_platform_date
        unique (scenic_spot_code, platform_code, travel_date, publish_time)
)
    comment '平台评论分布及排行表' charset = utf8mb4;
