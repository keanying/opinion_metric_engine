-- 下钻表 ↔ 大盘表对不上时的排查 SQL（客户 2026-10）
-- 例子：景区 PFTSCA01016184、跑数日期 20261012、近7日（下钻表 travel_date 20261006~20261012）。
-- 换别的周期时，把 ③④ 里的区间换成这个周期的本期区间（见 sql/api_drill_list.sql 顶部说明）。
--
-- ① 不止 td：库里还有旧版本「按周期各存一份」的行 → 用 repair 把这些日期重跑（按 travel_date 先删后写会删掉它们）
-- ② 不为 0：有旧口径地域为空的行（热力图记「未知」）→ repair 重跑那几天
-- ③ 不相等：补数上限没统一（settings_local.py 的 backfill_lookback 不是一个数），或前几天的 td 是旧数据 → 改配置后 repair 重跑区间
-- ④ diff 不为 0 的地域就是对不上的地域

-- ① 下钻表里是否还有 #15 版本「按周期各存一份」的旧行（应该只有 td）
SELECT time_granularity, COUNT(*) AS cnt, MIN(travel_date), MAX(travel_date)
FROM ads_trf_social_opinion_drill_analysis_di
WHERE scenic_id = 'PFTSCA01016184'
GROUP BY time_granularity;

-- ② 地域为空的旧行（热力图里没地域的记「未知」）
SELECT COUNT(*) AS empty_region_rows
FROM ads_trf_social_opinion_drill_analysis_di
WHERE scenic_id = 'PFTSCA01016184' AND (region IS NULL OR region = '');

-- ③ 大盘表近7日 是否 == 区间内每天 td 之和（不等 = 补数上限没统一，或前几天的 td 是旧的）
SELECT m7.channel, m7.comment_total AS latest_7d, SUM(td.comment_total) AS td_sum
FROM ads_trf_social_opinion_macro_gran_metric_di m7
JOIN ads_trf_social_opinion_macro_gran_metric_di td
  ON td.scenic_id = m7.scenic_id AND td.channel = m7.channel AND td.time_granularity = 'td'
 AND td.travel_date BETWEEN 20261006 AND 20261012
WHERE m7.scenic_id = 'PFTSCA01016184' AND m7.travel_date = 20261012 AND m7.time_granularity = 'latest_7d'
GROUP BY m7.channel, m7.comment_total;

-- ④ 逐个地域对比：热力图 heat vs 下钻表（近7日 = travel_date 1006~1012，全渠道）
SELECT COALESCE(h.region, dd.region) AS region, h.heat, dd.cnt AS drill_cnt,
       COALESCE(h.heat, 0) - COALESCE(dd.cnt, 0) AS diff
FROM (
    SELECT j.region, j.heat
    FROM ads_trf_social_opinion_macro_gran_metric_di m,
         JSON_TABLE(m.period_comment_heatmap, '$[*]'
                    COLUMNS (region VARCHAR(64) PATH '$.region', heat INT PATH '$.heat')) j
    WHERE m.scenic_id = 'PFTSCA01016184' AND m.travel_date = 20261012
      AND m.time_granularity = 'latest_7d' AND m.channel = 'all'
) h
LEFT JOIN (
    SELECT region, COUNT(DISTINCT channel, work_id, comment_id, travel_date) AS cnt
    FROM ads_trf_social_opinion_drill_analysis_di
    WHERE scenic_id = 'PFTSCA01016184' AND time_granularity = 'td'
      AND travel_date BETWEEN 20261006 AND 20261012
    GROUP BY region
) dd ON dd.region = h.region
ORDER BY ABS(COALESCE(h.heat, 0) - COALESCE(dd.cnt, 0)) DESC, h.heat DESC
LIMIT 15;
