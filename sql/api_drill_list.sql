-- 看板下钻列表接口（客户 2026-10）：点词云 / 热力图地域 / 渠道 → 列出评论，分页。
--
-- 参数与大盘表接口一致：:scenicId、:qTime（= 大盘表 travel_date，yyyyMMdd）、:timeTag（= 大盘表 time_granularity）。
-- 下钻表按天存，这里把 :timeTag 换算成本期区间 [d_start, d_end] 再圈 travel_date，和大盘表同一套口径：
--   td 今日 [T, T]          latest_1d 近1日 [T-1, T-1]   latest_7d / 30d / 60d / 90d [T-6 / 29 / 59 / 89, T]
--   wtd 本周 [本周一, T]    mtd 本月 [本月1日, T]        qtd 本季度 [本季度首日, T]
-- 这样 total（不带 emotionWord）== 大盘表 comment_total；带 region == 热力图该地域 heat；
-- wordcloud_map 里的每个词带 emotionWord 都查得到。
--
-- 去重按 (景区, 渠道, 作品, 评论, travel_date)：同一条评论命中多个词在同一天有多行，只留一行；
-- 被补数复制到区间里不同天的同一条评论，大盘表每天各算一次，这里也各算一行（publish_time 是补到的那天 0 点）。
-- 只按 (作品, 评论) 去重会把复制的几天合成一条，总数就比大盘表少（近7日 1010 → 887）。
--
-- 用客户数据在 MariaDB 实测：9 个周期 × 7 个渠道 63 格，总数 / 每个地域 / 每个词全部对上。

SELECT
    scenicId, scenicName, channelName, workId, commentId, workUrl, emotionWord, contentSnippet,
    dimensionLevel1, likes, publishTime, sentimentScore, entityTags, region, handleFlag,
    updatedTime, etlTime,
    total                                                   -- 总条数（按评论 × 天去重），每行都带
FROM (
    SELECT t.*,
           ROW_NUMBER() OVER (ORDER BY publishTime DESC, commentId) AS rowNo,
           COUNT(*) OVER ()                                       AS total
    FROM (
        SELECT
            d.scenic_id        AS scenicId,
            d.scenic_name      AS scenicName,
            d.channel_name     AS channelName,
            d.work_id          AS workId,
            d.comment_id       AS commentId,
            d.work_url         AS workUrl,
            d.emotion_word     AS emotionWord,
            d.content_snippet  AS contentSnippet,
            d.dimension_level1 AS dimensionLevel1,
            d.likes            AS likes,
            d.publish_time     AS publishTime,
            d.sentiment_score  AS sentimentScore,
            d.entity_tags      AS entityTags,
            d.region           AS region,
            d.handle_flag      AS handleFlag,         -- 客户表上的字段
            d.updated_time     AS updatedTime,        -- 客户表上的字段
            d.etl_time         AS etlTime,
            -- 同一条评论在同一天有多行（命中多个词）时只留一行；补数复制到不同天的算不同的行，和大盘表一致
            ROW_NUMBER() OVER (
                PARTITION BY d.scenic_id, d.channel, d.work_id, d.comment_id, d.travel_date
                ORDER BY d.publish_time DESC
            ) AS rn
        FROM ads_trf_social_opinion_drill_analysis_di d
        -- 周期 → 本期区间（和大盘表同一套口径）
        JOIN (
            SELECT CAST(DATE_FORMAT(CASE :timeTag
                        WHEN 'latest_1d'  THEN q - INTERVAL 1 DAY
                        WHEN 'latest_7d'  THEN q - INTERVAL 6 DAY
                        WHEN 'latest_30d' THEN q - INTERVAL 29 DAY
                        WHEN 'latest_60d' THEN q - INTERVAL 59 DAY
                        WHEN 'latest_90d' THEN q - INTERVAL 89 DAY
                        WHEN 'wtd'        THEN q - INTERVAL WEEKDAY(q) DAY                  -- 本周一
                        WHEN 'mtd'        THEN q - INTERVAL (DAYOFMONTH(q) - 1) DAY         -- 本月 1 日
                        WHEN 'qtd'        THEN MAKEDATE(YEAR(q), 1) + INTERVAL (QUARTER(q) - 1) QUARTER
                        ELSE q END, '%Y%m%d') AS UNSIGNED) AS d_start,                     -- td
                   CAST(DATE_FORMAT(CASE :timeTag
                        WHEN 'latest_1d' THEN q - INTERVAL 1 DAY
                        ELSE q END, '%Y%m%d') AS UNSIGNED) AS d_end
            FROM (SELECT STR_TO_DATE(:qTime, '%Y%m%d') AS q) x
        ) r ON d.travel_date BETWEEN r.d_start AND r.d_end
        WHERE d.scenic_id = :scenicId                                              -- 必填
        $if(!empty(emotionWord))$     AND d.emotion_word = :emotionWord          $endif$
        $if(!empty(region))$          AND d.region = :region                     $endif$
        $if(!empty(channel))$         AND d.channel = :channel                   $endif$
        $if(!empty(dimensionLevel1))$ AND d.dimension_level1 = :dimensionLevel1  $endif$
        $if(!empty(dimensionLevel2))$ AND d.dimension_level2 = :dimensionLevel2  $endif$
        $if(!empty(dimensionLevel3))$ AND d.dimension_level3 = :dimensionLevel3  $endif$
    ) t
    WHERE rn = 1
) p
WHERE rowNo >    :pageIndex      * :pageSize
  AND rowNo <=  (:pageIndex + 1) * :pageSize
ORDER BY rowNo
;
