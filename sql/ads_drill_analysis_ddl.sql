-- 需求 5.2：下钻分析表 —— 词 → 评论 → 作品
--
-- 用途：看板点击关键词云/负面突增词里的某个 emotion_word，
--       列出命中该词的评论明细，再点评论跳转到原作品（work_url）。
--
-- 幂等：detail_uk 唯一，跑批走 INSERT ... ON DUPLICATE KEY UPDATE，
--       同一天重复跑不会产生重复行。

create table if not exists ads_trf_social_opinion_drill_analysis_di
(
    id               bigint auto_increment comment '自增主键' primary key,

    scenic_id        varchar(64)                              null comment '景区编号',
    scenic_name      varchar(255)                             null comment '景区名称',

    emotion_word     varchar(255)                             not null comment '关键词（对应内容表 emotion_word）',
    emotion_type     varchar(32)                              null comment '词的情感归属(positive/neutral/negative)，取自所属评论的 sentiment_score',
    word_source      varchar(32)     default 'keyword'        null comment '词来源：keyword=关键词数组',

    platform_code    varchar(64)                              null comment '平台编码(=src.channel)',
    platform_name    varchar(64)                              null comment '平台名称',

    work_id          varchar(600)                             null comment '作品/帖子/笔记ID',
    work_url         varchar(600)    default ''               null comment '作品链接，前端跳转用',
    work_title       varchar(255)    default ''               null comment '作品标题/正文摘要',
    author_name      varchar(600)    default ''               null comment '作者名称',

    comment_id       varchar(300)                             null comment '评论ID',
    root_comment_id  varchar(300)    default ''               null comment '所属一级评论ID',
    comment_level    varchar(20)     default ''               null comment '评论层级 level_1/level_2...',
    commenter_name   varchar(600)    default ''               null comment '评论用户昵称',

    content_snippet  varchar(1000)                            null comment '评论内容摘要（截断）',
    likes            bigint          default 0                null comment '评论点赞数',
    sentiment_score  int                                      null comment '评论整体情感得分 1/0/-1',

    dimension_level1 varchar(128)    default ''               null comment '该评论首个维度标签-一级',
    dimension_level2 varchar(128)    default ''               null comment '该评论首个维度标签-二级',
    dimension_level3 varchar(128)    default ''               null comment '该评论首个维度标签-三级',

    publish_time     datetime                                 null comment '评论发布时间',
    travel_date      int                                      not null comment '分区/评论时间（yyyyMMdd）',
    etl_time         datetime        default CURRENT_TIMESTAMP null comment 'ETL写入时间',

    detail_uk        char(32)                                 not null comment 'md5(景区+词+平台+作品+评论)，幂等唯一键',

    constraint uk_drill_analysis unique (detail_uk)
)
    comment '舆情下钻分析表(关键词-评论-作品)' charset = utf8mb4 row_format = DYNAMIC;

-- 下钻主查询走这条索引：先定景区+日期，再按词过滤，最后按点赞排序取前 N 条
create index idx_drill_analysis_query
    on ads_trf_social_opinion_drill_analysis_di (scenic_id, travel_date, emotion_word);

-- 「这个作品下所有命中词的评论」反查
create index idx_drill_analysis_work
    on ads_trf_social_opinion_drill_analysis_di (scenic_id, work_id);
