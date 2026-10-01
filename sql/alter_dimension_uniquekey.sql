-- 维度表补唯一索引（可选，但强烈建议执行）
--
-- 现状：ads_trf_social_opinion_comment_dimension_score_di 只有 PRIMARY KEY(id)，
-- 没有业务唯一键。直接 INSERT ... ON DUPLICATE KEY UPDATE 等同纯 INSERT，
-- 同一天重跑一次就多一份重复行，看板上维度得分会被重复行拉偏。
--
-- 本脚本执行前 loader 走「先按 (景区, 日期区间) DELETE 再 INSERT」；
-- 执行后把 settings 的 dimension_upsert 设为 True，即可切回 upsert（更快、锁更小）。

-- 1) 先看有没有既存重复（有的话下面的 ALTER 会失败）
select scenic_spot_code, travel_date, dimension_level1, dimension_level2,
       dimension_level3, count(*) cnt
from ads_trf_social_opinion_comment_dimension_score_di
group by 1, 2, 3, 4, 5
having cnt > 1
order by cnt desc
limit 50;

-- 2) 有重复就先去重：同一维度路径同一天只保留 id 最大的那行（最后一次跑批的结果）
-- delete d from ads_trf_social_opinion_comment_dimension_score_di d
-- join (
--     select max(id) keep_id, scenic_spot_code, travel_date,
--            dimension_level1, dimension_level2, dimension_level3
--     from ads_trf_social_opinion_comment_dimension_score_di
--     group by 2, 3, 4, 5, 6
-- ) k
--   on d.scenic_spot_code = k.scenic_spot_code
--  and d.travel_date = k.travel_date
--  and d.dimension_level1 = k.dimension_level1
--  and d.dimension_level2 = k.dimension_level2
--  and d.dimension_level3 = k.dimension_level3
-- where d.id < k.keep_id;

-- 3) 补唯一索引
alter table ads_trf_social_opinion_comment_dimension_score_di
    add constraint uk_dim_path
        unique (scenic_spot_code, travel_date, dimension_level1,
                dimension_level2, dimension_level3);

-- 注意：dimension_level2 / dimension_level3 可能为空串（如「游玩体验|排队时长|」），
-- 写入时统一落空串而不是 NULL —— MySQL 的唯一索引不约束 NULL，
-- 落 NULL 会让「排队时长」这类无三级维度的路径重复插入。
