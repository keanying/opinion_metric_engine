DATASOURCE = "你的数据源名称或ID"

# 你的完整 SQL，末尾自己写好 LIMIT / OFFSET 占位符。
# 占位符名字要和下面 LIMIT_PARAM / OFFSET_PARAM 对得上。
BASE_SQL = """
select
    t.travelDate, t.inProvince, t.outProvince, t.hmt, t.`foreign`,
    t.elderly, t.children, t.morningExercise, t.specialGroup, t.total,
    t.onlineBooking, t.onsiteBooking, t.inPark, t.advanceBook,
    t.arrivalRate, t.lastMonthRepeatBooking, t.lastMonthTotal
from bi_temp_table t
where t.travelDate >= :startTime and t.travelDate <= :endTime
order by t.travelDate
LIMIT :pageSize OFFSET :startOffsetSize
"""

# SQL 里分页占位符的名字（跟 BASE_SQL 里写的保持一致）
LIMIT_PARAM = "pageSize"          # 每页条数
OFFSET_PARAM = "startOffsetSize"  # 起始偏移量，由插件算出来

# ↑↑↑ 只需要改这两处 ↑↑↑

# 分页上限：防止有人传 pageSize=999999 一次拉全表把数据库拖垮
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 20


def _to_int(value, default, minimum=None, maximum=None):
    """安全转 int：非法值回落到默认值，并夹在 [minimum, maximum] 区间内。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and n < minimum:
        n = minimum
    if maximum is not None and n > maximum:
        n = maximum
    return n


def main(params, ctx=None):
    params = params or {}
    # pageIndex 从 1 开始（pageIndex=1 即第一页）
    page_index = _to_int(params.get("pageIndex"), 1, minimum=1)
    page_size = _to_int(params.get("pageSize"), DEFAULT_PAGE_SIZE, minimum=1, maximum=MAX_PAGE_SIZE)
    # 核心：把页码换算成偏移量，作为整数值传给 SQL
    offset = (page_index - 1) * page_size
    # 组装绑定值：
    #   业务参数（startTime / endTime 等）原样透传，
    #   分页参数用插件算好的值覆盖 —— 即使前端误传了 startOffsetSize，
    #   也以这里算出来的为准，避免翻页错乱。
    binds = {k: v for k, v in params.items() if k != "pageIndex"}
    binds[LIMIT_PARAM] = page_size
    binds[OFFSET_PARAM] = offset
    sql = BASE_SQL.strip().rstrip(";").rstrip()
    ctx.log(f"分页查询 pageIndex={page_index} pageSize={page_size} offset={offset}")
    rows = ctx.query(DATASOURCE, sql, binds)
    return rows


if __name__ == '__main__':
    base = {'startTime':"20260101", "endTime":"20260101", "pageIndex":1, "pageSize":20}
    main(params=base)