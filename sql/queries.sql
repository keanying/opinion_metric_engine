-- 看板取数参考 SQL（与 engin_cli/analytics.py 一一对应）
--
-- 约定：:scenic = 景区编码，:anchor = 锚点日期 yyyyMMdd
-- 周期字段后缀：今日 = 无后缀 / 本周 = _7d / 本月 = _30d
-- 下面一律用「本周」举例，换周期只需要换字段后缀。

-- ============================================================
-- §2 核心数值卡
-- 比率与环比直接读锚点行；评论总数按日粒度累加（core 表没有 comment_count_7d）
-- ============================================================
select
    (select sum(comment_count)
       from ads_trf_social_opinion_comment_core_di
      where scenic_spot_code = :scenic
        and travel_date between date_format(date_sub(str_to_date(:anchor,'%Y%m%d'),
                                                     interval 6 day),'%Y%m%d')
                            and :anchor)          as 评论总数,
    comment_count_wow                             as 评论总数环比,
    positive_rate_7d                              as 好评率,
    positive_growth_rate_7d                       as 好评率环比,
    negative_rate_7d                              as 差评率,
    negative_growth_rate_7d                       as 差评率环比,
    emotional_score_7d                            as 游客总体评分   -- 值域 [2.5, 5]，满分 5
from ads_trf_social_opinion_comment_core_di
where scenic_spot_code = :scenic and travel_date = :anchor;

-- ============================================================
-- §3 评论量趋势图（堆叠：正面/中性/负面）
-- 时间跨度随选择器联动：今日→近7天 / 本周→近14天 / 本月→近60天
-- ============================================================
select travel_date, positive_count 正面, neutral_count 中性, negative_count 负面
from ads_trf_social_opinion_comment_core_di
where scenic_spot_code = :scenic
  and travel_date between date_format(date_sub(str_to_date(:anchor,'%Y%m%d'),
                                               interval 13 day),'%Y%m%d')
                      and :anchor
order by travel_date;

-- ============================================================
-- §4 维度好评 TOP3（一级维度）
-- ============================================================
select distinct dimension_level1, dimension_1_score_7d
from ads_trf_social_opinion_comment_dimension_score_di
where scenic_spot_code = :scenic and travel_date = :anchor
order by dimension_1_score_7d desc
limit 3;

-- ============================================================
-- §5 维度评分变化趋势图（6 个一级维度折线）
-- 维度选择器就是在 dimension_level1 上加 IN 过滤
-- ============================================================
select travel_date, dimension_level1, max(dimension_1_score_7d) score
from ads_trf_social_opinion_comment_dimension_score_di
where scenic_spot_code = :scenic
  and travel_date between date_format(date_sub(str_to_date(:anchor,'%Y%m%d'),
                                               interval 13 day),'%Y%m%d')
                      and :anchor
group by travel_date, dimension_level1
order by travel_date, dimension_level1;

-- ============================================================
-- §6 平台评论分布（占比饼图 + 排行榜）
-- ============================================================
select platform_name,
       comment_cnt_7d                                   as 评论总量,
       comment_cnt_7d / sum(comment_cnt_7d) over ()     as 评论占比,
       week_good_rate                                   as 好评占比,
       week_good_rate_wow                               as 好评环比
from ads_trf_social_opinion_comment_platform_di
where scenic_spot_code = :scenic and travel_date = :anchor
order by 评论总量 desc;

-- ============================================================
-- §7 关键词云（正/中/负切换）
-- ============================================================
select emotion_word, emotion_value_7d 出现次数, emotion_rate_7d 热度占比
from ads_trf_social_opinion_comment_content_di
where scenic_spot_code = :scenic and travel_date = :anchor
  and emotion_type = 'negative'      -- positive / neutral / negative
order by emotion_value_7d desc
limit 50;

-- ============================================================
-- §8 负面突增词 TOP10
-- 突增量 = 本周期出现次数 - 上一周期出现次数。
-- 上一周期的值就在「锚点 - N 天」那一行的同一个字段上，两期自连接相减即可。
-- ============================================================
select cur.emotion_word,
       cur.emotion_value_7d                                  as 本期,
       coalesce(prev.emotion_value_7d, 0)                    as 上期,
       cur.emotion_value_7d - coalesce(prev.emotion_value_7d, 0) as 突增量
from ads_trf_social_opinion_comment_content_di cur
left join ads_trf_social_opinion_comment_content_di prev
       on prev.scenic_spot_code = cur.scenic_spot_code
      and prev.emotion_word = cur.emotion_word
      and prev.travel_date = date_format(date_sub(str_to_date(:anchor,'%Y%m%d'),
                                                  interval 7 day),'%Y%m%d')
where cur.scenic_spot_code = :scenic
  and cur.travel_date = :anchor
  and cur.emotion_type = 'negative'
order by 突增量 desc
limit 10;

-- ============================================================
-- §5.2 点词下钻：列出命中该词的评论，再点评论跳作品
-- 下钻表按天存。看板选的是哪个日期、哪个周期，就按这个周期的本期区间圈 travel_date，
-- 查到的评论正好是大盘表这一格（词云 / 热力图 / comment_total）背后的那些：
--   td 今日 [T, T]          latest_1d 近1日 [T-1, T-1]   latest_7d 近7日 [T-6, T]
--   latest_30d [T-29, T]   latest_60d [T-59, T]        latest_90d [T-89, T]
--   wtd 本周 [本周一, T]    mtd 本月 [本月1日, T]        qtd 本季度 [本季度首日, T]
-- ============================================================
select emotion_word, channel_name, commenter_name, content_snippet,
       likes, sentiment_score, publish_time,
       work_title, work_url            -- 前端跳转就用 work_url
from ads_trf_social_opinion_drill_analysis_di
where scenic_id = :scenic
  and emotion_word = :word
  and travel_date between :period_start and :period_end
  -- and channel = :channel              -- 选了单个渠道时再加
order by likes desc, publish_time desc
limit 100;

-- 点热力图某个地域：评论条数 == 大盘表 period_comment_heatmap 里这个地域的 heat
-- 去掉 region 条件 == comment_total。去重要带 publish_time：同一条评论被补数复制到区间里好几天时，
-- 大盘表每天各算一次，下钻表每天一行
select count(distinct channel, work_id, comment_id, publish_time) as cnt
from ads_trf_social_opinion_drill_analysis_di
where scenic_id = :scenic
  and travel_date between :period_start and :period_end
  and region = :region;

-- 反查：某个作品下命中了哪些词、各多少条
select emotion_word, emotion_type, count(*) cnt
from ads_trf_social_opinion_drill_analysis_di
where scenic_id = :scenic and travel_date between :period_start and :period_end
  and work_id = :work_id
group by emotion_word, emotion_type
order by cnt desc;
