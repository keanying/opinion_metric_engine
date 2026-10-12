-- 看板下钻列表接口：客户原写法 + 最小改动版（客户 2026-10）
--
-- 参数：qTime = 本期首日、endTime = 本期末日（都是 yyyyMMdd，由前端按周期算好传进来）
--   td 今日 [T, T]          latest_1d 近1日 [T-1, T-1]   latest_7d / 30d / 60d / 90d [T-6 / 29 / 59 / 89, T]
--   wtd 本周 [本周一, T]    mtd 本月 [本月1日, T]        qtd 本季度 [本季度首日, T]
-- 和客户原写法相比只改三处：
--   1. 去重分区加 travel_date：补数复制到不同天的同一条评论各算一行（大盘表每天各算一次）
--   2. time_granularity = 'td'：只取按天存的行，挡掉旧版本按周期存的行
--   3. 加 endTime 上限：不传的话会把区间之后的日子也算进来（近1日、查历史日期时必错）
-- MariaDB 实测（客户数据，1012 / 1010 两个日期 × 9 周期 × 7 渠道）：传 endTime 后
-- 总数 / 每个地域 / 每个词全部和大盘表一致；原写法分别有 32 / 53 格对不上。

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
            scenic_id        AS scenicId,
            scenic_name      AS scenicName,
            channel_name     AS channelName,
            work_id          AS workId,
            comment_id       AS commentId,
            work_url         AS workUrl,
            emotion_word     AS emotionWord,
            content_snippet  AS contentSnippet,
            dimension_level1 AS dimensionLevel1,
            likes            AS likes,
            publish_time     AS publishTime,
            sentiment_score  AS sentimentScore,
            entity_tags      AS entityTags,
            region           AS region,
            handle_flag      AS handleFlag,
            updated_time     AS updatedTime,
            etl_time         AS etlTime,
            -- 同一条评论同一天有多行（命中多个词）只留一行；复制到不同天的各算一行，和大盘表一致
            ROW_NUMBER() OVER (PARTITION BY scenic_id, channel_name, work_id, comment_id, travel_date ORDER BY publish_time DESC) AS rn
        FROM ads_trf_social_opinion_drill_analysis_di
        WHERE scenic_id = :scenicId                                              -- 必填
          AND time_granularity = 'td'                                         -- 只取按天存的行
        $if(!empty(qTime))$           AND travel_date >= :qTime                $endif$
        $if(!empty(endTime))$         AND travel_date <= :endTime              $endif$
        $if(!empty(emotionWord))$     AND emotion_word = :emotionWord          $endif$
        $if(!empty(region))$          AND region = :region                     $endif$
        $if(!empty(channel))$         AND channel = :channel                   $endif$
        $if(!empty(dimensionLevel1))$ AND dimension_level1 = :dimensionLevel1  $endif$
        $if(!empty(dimensionLevel2))$ AND dimension_level2 = :dimensionLevel2  $endif$
        $if(!empty(dimensionLevel3))$ AND dimension_level3 = :dimensionLevel3  $endif$
    ) t
    WHERE rn = 1
) p
-- 分页：pageIndex 从 0 开始，每页 20 条
WHERE rowNo >    :pageIndex      * 20
  AND rowNo <=  (:pageIndex + 1) * 20
ORDER BY rowNo
;
