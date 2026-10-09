# opinion_metric_engine —— 舆情指标引擎

> 景区社媒舆情指标计算引擎。命令行包名 `engin_cli`，跑批入口 `python -m engin_cli.cli`。

从两张采集源表**直接用 Python 算出四张 ADS 指标表 + 一张大盘 KPI 表 + 一张下钻明细表**，
全程在内存里做，不落任何中间表（没有 ODS/DWD/DWS 分层）。

```
   src_opinion_social_work_comment_di   (评论，指标主数据源)
   src_opinion_social_work_di           (作品，仅 5.2 下钻取 work_url)
                    │
                    ▼  normalize（JSON 解析 / 情感归一 / 维度与关键词炸开）
   comment_facts · dimension_facts · keyword_facts     ← DataFrame，进程内存
                    │
                    ▼  builders（日粒度事实 → 多窗口滚动 → 环比/得分）
   core · platform · dimension_score · content · macro ← 列名 = 目标表 DDL
                    │
                    ▼  validate → load（按 景区 + publish_time 先删后插）
   MySQL ADS 表（+ CSV 备份）
```

---

## 快速开始

```bash
pip install -r requirements.txt
cp settings_local.example.py settings_local.py    # 填库连接，该文件已 gitignore

# 跑昨天
python -m engin_cli.cli run

# 跑指定区间，只出 CSV 不写库（第一次建议先这样看一眼）
python -m engin_cli.cli run --start-date 20260801 --end-date 20260901 --skip-db

# 按规则文档打印看板取数结果，肉眼验收
python -m engin_cli.cli report --scenic PFTSCA01002434 --date 20260901 --period week

# 完全离线跑（不连库，直接喂两张源表的 CSV）
python scripts/make_sample_data.py --days 120 --end-date 20260901 --out sample
python -m engin_cli.cli run --date 20260901 \
    --comments-csv sample/comments.csv --works-csv sample/works.csv --skip-db

# 得分口径变更后，重刷历史分数（先 dry-run 看影响面）
python -m engin_cli.cli repair --start-date 20260101 --end-date 20260901 --dry-run --show-preview
python -m engin_cli.cli repair --start-date 20260101 --end-date 20260901

# 单测（98 项，含端到端、口径守卫、历史修正、行存在规则）
python -m pytest -q
```

日常调度：每天凌晨跑 `run`（默认跑昨天），失败退出码为 1，可直接接告警。

---

## 源字段 → 指标 的映射

这是整套代码的核心，先看懂这张表再看代码。

| 源字段 | 用途 | 落到哪 |
|---|---|---|
| `sentiment_score` (1/0/-1) | **好评/中评/差评的唯一判定依据** | core 表全部计数与得分、platform 好评数、content 词的情感归属 |
| `sentiment_label` | `sentiment_score` 为空时的兜底（文本反推） | 同上 |
| `keyword_tags` (词数组) | **关键词云与负面突增词的词源** | content 表 `emotion_word`、下钻明细表 |
| `dimension_tags` (`[{dim1,dim2,dim3,sentiment}]`) | 维度拆解，**每个元素算一条独立记录** | dimension_score 表的三层得分 |
| `channel` | 平台归属 | platform 表 `platform_code/platform_name` |
| `publish_time` | **归属日期**（不是 crawl_time） | 所有表的 `travel_date` |
| `scenic_id / scenic_name` | 景区 | 所有表 |
| `work_id` + 作品表 `work_url/title` | 下钻跳转 | 下钻明细表 |
| `content / likes / commenter_name` | 明细展示 | 下钻明细表 |
| `location` | 地域，归一到省级简称（§12） | 大盘表 `period_comment_heatmap`、下钻表 `region` |
| `entity_tags` | 不参与指标计算，原样带到明细 | 下钻表 `entity_tags` |

`sentiment_score` 与 `keyword_tags` 是三张表核心内容的交汇点：
**评论的情感决定了它贡献给哪一类计数，也决定了它带的每个关键词进哪一组词云。**
所以这两个字段的口径必须在一处定义 —— 见下一节的 `metric_calc_domain.judge_sentiment`
与 `daily_keyword_facts`，四个 builder 一律复用，不允许各算各的。

---

## 口径唯一真源：`engin_cli/metric_calc_domain.py`

**所有指标的计算口径都落在这一个文件里**，其余模块只引用、不重算。改口径改一处即可。

文件内分 10 章，第 5~8、10 章每个函数的 docstring 都写清了 **公式 / 入参 / 出参 / 口径要点**：

| 章节 | 内容 | 典型入口 |
|---|---|---|
| 1 口径常量 | 得分权重与值域、精度、窗口档位、看板周期、情感枚举、维度白名单、渠道字典 | `W_POSITIVE` `SCORE_MAX` `CORE_WINDOWS` |
| 2 原子公式 | 情感得分 v2/v1 / 环比 / 占比 / 星级均分 / 情感判定 | `weighted_score_from_counts` `sentiment_score_from_counts` `growth_rate` |
| 3 日粒度事实口径 | 什么算一条评论、一次维度提及、一次词出现 | `daily_core_facts` `daily_keyword_facts` |
| 4 字段名注册表 | 窗口档 → 各表 DDL 字段名 + 四张表的列清单 | `CORE_SUFFIX` `PLAT_RATE` `CORE_COLUMNS` |
| 5 core 指标 | 好评率/差评率/环比/情感得分/均分 | `core_metrics(w)` |
| 6 platform 指标 | 平台与全平台两套占比、好评率、好评环比 | `platform_metrics(w)` |
| 7 dimension 指标 | 三层维度 × 7 个窗口 = 21 个得分 | `dimension_metrics(w)` |
| 8 content 指标 | 词频、热度占比、排名、突增量 | `content_metrics(w)` `word_surge()` |
| 9 下钻掩码 | `content_snippet` 只保留关键词 | `mask_content()` |
| 10 macro 指标 | 周期 → 日期区间、百分制占比/环比/同比、维度/词云/热力 JSON | `macro_period_ranges()` `macro_metrics(w)` |

各模块的分工（它们只做搬运，不做口径判断）：

```
normalize.py   解析 JSON、把评论炸成明细        → 调 domain.judge_sentiment
windows.py     日粒度事实 → 明细补齐 + 多窗口累计 → 纯机械，上限来自 domain §1.8
builders/*.py  分组 → 滚动 → **调 domain 算指标** → 拼 travel_date 等时间字段
validate.py    自检                             → 断言用的公式也来自 domain
pusher.py      算完后推给下游（HTTP API）        → 纯搬运，格式与开关全来自 settings
analytics.py   看板取数                         → 周期→字段名的映射查 domain 注册表
```

`tests/test_domain_single_source.py` 守着这条规矩：别处再定义一个 `official_score`、
或在 builder 里硬编码 `"_7d"`、或出现 `× 5`、`min(1, n/100)` 这类公式痕迹，测试立刻红。

**入参契约**：第 5~8 章的函数吃的是 builder 滚动后的宽表，列名约定写在各自 docstring 里
（如 core 要 `positive_count_7d` / `positive_count_prev_7d`）。所有 `_{N}d` 列**都已经是
回补后的值**，domain 里不再区分「真实计数」与「回补计数」。
改了列名两边要同步，这是 builder 与 domain 之间唯一的耦合面。

---

## 关键口径

### 1. 情感得分公式（`metric_calc_domain.weighted_score_from_counts`）

**v2 · 现行口径**：加权占比法

```
S = 5 × (好评率 × 1.0 + 中评率 × 0.9 + 差评率 × 0.5)
```

- 好评 1.0、**中评 0.9**（文旅场景的中评多为弱正面）、**差评 0.5**（给底分，
  避免单条差评把分数击穿）
- 三个占比之和恒为 1 ⇒ 值域天然是 **[2.5, 5]**：全差评 2.5，全好评 5，**不会超过 5**
- **没有置信度权重**。样本量小的问题交给缺数回补（§9 先补明细），
  不再在公式里对小样本打折

**游客综合情感得分与维度分数是同一个公式**（同一个函数），区别只在样本范围：
综合得分用该景区的全部评论，维度分数用该维度下的提及。所以维度分数同样落在 [2.5, 5]。

边界：`总数 = 0 → 4.5`（等价于「全部按中评计」）。没有评论不等于评价很差，
返回 0 会让空数据景区在榜单垫底。

**v1 · 历史口径**（净值 + 置信度，值域 [0,10]，中性 = 5）：

```
S = 5 + (好评率 - 差评率) × 5 × min(1, 总数/100)
```

实现保留在 `official_score_from_counts`，仅供对比与回滚
（`settings.score_formula = "confidence_v1"`）。

> ⚠ **两套口径值域不同**（[0,10] vs [2.5,5]），换口径必须同步改看板刻度，
> 否则同一个 4.6 分会被读成「不及格」。同一批数据的实测对比：
>
> | 好/中/差 | v1（0~10） | v2（2.5~5） |
> |---|---|---|
> | 69.7% / 14.7% / 15.6% | 7.71 | 4.54 |
> | 56.0% / 16.0% / 28.0% | 6.40 | 4.22 |
> | 55.6% / 24.1% / 20.4% | 5.95 | 4.37 |
>
> 另外注意 `daily_rating`（统计时刻均分，1~5 星）现在与 `emotional_score`
> 刻度接近但**不是一回事**：前者是星级均分（正5/中3/差1），后者是情感得分
> （权重 1.0/0.9/0.5 再 ×5）。看板上两者并排时要标清楚。

### 2. 存储精度

规则文档写「保留一位小数」，那是**展示**口径。数据层按 `decimal(10,4)` 存 4 位，
由看板 `round(x,1)`。如果在数据层就存 1 位，6~9 分区间只剩 30 个取值，
维度排名会出现大量并列。

### 3. 窗口与环比（口径见 domain 第 1/2 章，滚动实现见 `windows.py`）

- 窗口 N = **[锚点-N+1, 锚点]**，含锚点当天。`_7d` 是「近 7 天」不是「前 7 天」。
- 上一周期 = 再往前推 N 天，两段不重叠、不留空档。
- **滚动前必须把日期补成连续日历并填 0。** 直接对稀疏行 `rolling(7)` 得到的是
  「最近 7 条记录」而不是「最近 7 天」，在有缺采日的真实数据上会算出偏大的窗口值。
  这是这类 ETL 最常见、也最难发现的错误，`tests/test_windows.py` 专门盯它。
- 环比分母为 0 时返回 **0 而不是 inf**：`decimal(10,4)` 存不下 inf，
  「从 0 涨到 100」的正确表达是「新增」，应由展示层根据「上期=0 且 本期>0」标注。

### 4. 主子评论、多维度评论

- 规则文档 §2「主子评论均作为独立个体计数」→ `level_2` 的子评论**不折叠**，
  一条就是一条。
- 规则文档 §4/§5「多维度评论各维度独立计数」→ 一条评论命中 3 个维度就产生 3 行维度事实，
  且每行的情感取**该维度自己的 sentiment**，不跟随整条评论
  （「风景好但停车难」里，交通接驳是负面）。

### 5. 维度表的三层语义（按 DDL）

```
dimension_3_score = 该 dim1/dim2/dim3 路径自身的得分
dimension_2_score = 该 dim1/dim2 下所有提及的得分
dimension_1_score = 该 dim1 下所有提及的得分
```

三层各自独立按公式算，口径自洽：同一个 dim1 下所有行的 `dimension_1_score` 相同，
BI 下钻能对上账。自检项「同一一级维度当日出现多个 dimension_1_score」就是盯这个。

> DDL 里 90d/365d 的注释写的是「维度TOP1/TOP2/TOP3得分」，与 7d~60d 的
> 「维度一/二/三分数」不一致。本实现全部按后者（层级）口径，
> 如果 90d/365d 确实要 Top-N 口径，需要单独开关，请先确认。

### 6. 内容表（关键词云 / 负面突增词）

| 字段 | 口径 |
|---|---|
| `emotion_word` | 来自 `keyword_tags` 的词 |
| `emotion_type` | 该词当天出现次数最多的那一类评论的情感（并列时 负 > 正 > 中）；当天没出现的补零行沿用最近一次的归类 |
| `emotion_value` | 该词**当日**出现次数 |
| `emotion_value_Nd` | 该词**近 N 日累计**出现次数 |
| `emotion_rate_Nd` | 热度占比 = 近 N 日累计次数 / 近 N 日总评论数（分母与 core 表同源） |
| `emotion_rank` | 同日同情感类型内按 `emotion_value` 降序的名次 |

**突增量不落表**，因为它是「本期 - 上期」的两期差值，随所选周期变化：
今日榜用 1d 差、本周榜用 7d 差、本月榜用 30d 差，同一行存不下三套。
取数方式见 `sql/queries.sql` §8 或 `analytics.negative_surge()` ——
数据源就是这张表的 `emotion_value_Nd`，两期自连接相减，性能没有问题。

> 如果你们那边 `emotion_value` 的既定含义就是「相对上一周期的增量」，
> 那 `emotion_rate` 的分子（近 N 日出现次数）与它就对不上了。
> 改动点只有 `metric_calc_domain.content_metrics` 一处，确认后我改。

同一条评论里重复出现的同一个词**只算一次**，否则一条刷屏评论能把词频顶上天。

### 7. platform 表的两套口径：`total_` 前缀 = 全平台

平台表一行里同时存两套数，看的时候极容易搞混：

| 口径 | 字段 | 同一天各渠道行之间 |
|---|---|---|
| **全平台**（该景区当期所有渠道合计） | `total_good_rate_*`、`week_total_good_rate*`、`mon_total_good_rate*`、**`positive_count`、`positive_7d_count` … `positive_365d_count`** | **值完全相同**（21 个字段） |
| **该渠道自己** | `good_rate_*`、`week_good_rate*`、`mon_good_rate*`、`plat_positive_*_count`、`comment_cnt*`、`comment_rate`、`positive_rate`、`growth_rate` | 各行不同 |

> **命名陷阱**：`positive_count` / `positive_7d_count` … 这一组**没有 `total_` 前缀，
> 却是全平台口径**（DDL 注释写的是「近1日(全平台)好评数」），它们的平台版叫
> `plat_positive_count` / `plat_positive_7d_count`。光看字段名一定会看反。

所以看到同一天六条渠道记录里 `total_good_rate_wow_1d`、`week_total_good_rate_wow`、
`total_good_rate_wow_14d`、`mon_total_good_rate_wow` 全都一样，**这是设计，不是重复写错**——
排行榜要「携程好评率 84% vs 全景区 12%」这样并排比较，所以每行都带一份全景区的基准值。
这四个字段彼此不同只是因为窗口不同（1 日 / 7 日 / 14 日 / 30 日）。

命名上最容易看错的一对：

```
week_total_good_rate_wow   近7日「全平台」好评数环比   ← 各渠道行相同
week_good_rate_wow         近7日「该平台」好评数环比   ← 各渠道行不同
```

只差一个 `total_`。字段名映射表在 `metric_calc_domain` 第 4 章，别凭记忆写。

自检里有两条硬断言盯着这件事（`validate_platform`）：全平台字段在同一
(景区,日期) 内**只能有一个值**，且**必须等于各平台相加**。
两条一起才能证明它算的确实是全平台，而不是某个平台的数被错抄成了全平台。

> 长窗口的 `*_wow_14d` / `mon_*_wow` 出现 +230% 这种数字，先看历史够不够：
> 算 14 日环比要 28 天历史，采集刚放量时上一周期基数很小，环比自然虚高。
> 见 §10 的说明。

### 8. 行存在规则：当天没有数据，行也要在（domain §1.7）

源表里「某平台/某维度/某个词当天一条都没有」= **这一行根本不存在**。
如果产出跟着一起消失，看板查今天的近 30/60/90 日指标就会扑空 —— 窗口里明明有值，行没了。

所以行存在规则不是「当日有数据」，而是：

> 只要该实体在**任一窗口**内有数据，当天就出一行；
> 当日计数为 0，窗口指标照算。

| 表 | 出行条件 | 说明 |
|---|---|---|
| `core_di` | 景区在近 365 日内有过评论 | 零评论日也出行，`comment_count = 0`，30/60/90 日指标照旧 |
| `platform_di` | **配置的渠道全部出行** | 见下 |
| `dimension_score_di` | 维度路径近 365 日有过提及 | 路径数量有限，全量铺开无压力；折线图不会因为某天没提及而断掉 |
| `content_di` | 词在近 `content_row_window`（默认 30）日出现过 | 只铺 30 天，见下 |

**平台表**：`settings.platform_codes`（默认六个平台）里的渠道，每个景区每天都出一行。
从来没有数据的渠道也出**全 0 行** —— 这样看板的渠道列表稳定，
「这个平台没人讨论」和「这个平台没接入」在数据上是同一种表达：0。
数据里出现了但没写进配置的渠道**也会保留**：采集侧新接一个平台，
不该因为配置忘了改就把真实数据丢掉。

**内容表为什么只铺 30 天**：词的数量没有上限，「一年内出现过的所有词 × 每天一行」
会让这张表比其他三张加起来还大几个量级。30 天正好覆盖看板最长的周期（本月），
60/90/365 日词云不在产品需求里。要放宽就调 `content_row_window`，
代价是行数按 词数 × 天数 线性增长。

补零行的两个细节：

- **情感归属**（`emotion_type`）沿用该词最近一次出现时的归类，不留空 ——
  留空的行既进不了任何一个词云，又占着唯一键的位置。
- **热度占比的分母**同样要铺网格。景区当天一条评论都没有时 core 侧没有这一行，
  不铺的话分母取到 NaN，占比被兜底成 0，30 日词云直接失真。

配套的自检调整：「平台占比合计 = 1」会跳过零评论日（分母为 0 时占比全是 0，
那是刻意保留的行，不是错误）。

### 9. 缺数回补：先补明细，再算指标（需求六.1，`metric_calc_domain` §1.8）

**口径（业务方定义，2.0 版）**：回补补的是**每日明细**，不是窗口结果。
某渠道某一天一条评论都没有 → 往前找最近一个这个渠道有评论的日子，把**那一天的全部评论明细**
原样复制过来，当作这一天的明细，找到就停。

```
快手 09-10 有 3 条，09-11 / 09-12 / 09-13 都没有
  → 09-11、09-12、09-13 各自复制 09-10 的 3 条（每天都是 3 条）
  → 近 7 日（截至 09-13）= 09-10 真实 3 条 + 09-11~09-13 复制 3×3 = 12 条
```

明细补齐之后，**所有指标都在补齐后的明细上照常计算**：窗口合计、好/中/差、得分、占比、
环比、同比、词频、维度、地域……窗口里有就有，没有就没有，不再整窗挪动。
上一周期（环比分母）与去年同期（同比分母）同样用补齐后的明细。

**往前最多找几天，按指标窗口分档**（找太远就失去时效性了）。窗口 N 的指标用
「最多往前找 L_N 天」补出来的那一份明细逐日相加；找满上限还没有，这一天这个渠道就是空的（0 条）：

| 窗口 | 当日 / 近 1 日 | 7 日 | 14 日 | 30 日 | 60 日 | 90 日 | 365 日 |
|---|---|---|---|---|---|---|---|
| 最多往前找 | 5 天 | 10 天 | 15 天 | 20 天 | 30 天 | 60 天 | 90 天* |

\* 365 日档业务方未指定，按递增趋势暂定 90 天。

所以同一天的明细在不同档位里可能不一样：快手只有 09-01 有数据时，09-07 的 1 日值是 0
（往前 5 天找不到），但 7 日档往前能找 10 天，09-07 的近 7 日 = 09-01 真实 1 条 + 09-02~09-07 各复制 1 条 = 7。

取数时会在 `lookback_days` 之前**多取「最大上限」天**（默认 90 天），只当补齐的来源，
不进任何窗口合计 —— 否则取数区间第一天如果恰好没评论，它就补不出来，和逐日跑的结果对不上。

**配置**：

| 配置 | 默认 | 说明 |
|---|---|---|
| `backfill_mode` | `"fill"` | `fill` 先补明细再算指标 / `off` 不回补（当天没评论即为 0）。旧写法 `shift` 等同 `fill` |
| `backfill_lookback` | `None` | 逐档覆盖上限，**只写要改的档**，例如 `{1: 3, 7: 7}`，其余仍用上表 |
| `platform_codes` | None（六个平台） | 配置的渠道全集，见 §7 行存在规则 |
| `content_row_window` | 30 | 内容表铺行窗口，见 §7 |

命令行也能临时覆盖：

```bash
python -m engin_cli.cli run --date 20260911 --backfill-lookback 1:3,7:7
python -m engin_cli.cli run --date 20260911 --no-backfill      # backfill_mode=off
```

**代价要知道**：`comment_count` 的含义从「当天真的收到多少条」变成了
「**各渠道补齐当天明细后的条数之和**」。趋势图上会看到平台期而不是断崖，
这是刻意的 —— 采集缺一天不该让看板显示「舆情归零」。

#### 补齐的原子粒度是**渠道**，不是景区

「当天有没有数据」看的是**这个渠道当天有没有评论**，缺的是「快手」，就只复制快手的明细：

```
09-10  抖音15 小红书22 微博2 携程4 快手3      合计 46
09-11  抖音10 小红书20 微博5 携程3 （无快手）  原始合计 38
       ↓ 快手复制 09-10 的 3 条
09-11  抖音10 小红书20 微博5 携程3 快手3      合计 41
```

`core.comment_count(09-11)` = **41**，不是 38。景区粒度的数 = 各渠道补齐后的明细相加，
core / platform / macro 用**同一份渠道全集、同一段日历、同一套上限**，于是这几条恒等式硬成立，自检里卡死：

```
core.comment_count           == Σ 各渠道 comment_cnt
platform 的 total_ 全平台字段 == Σ 各渠道对应字段      （占比合计因此也恒为 1）
macro 的 all 行              == Σ 各渠道行
```

复制的是那一天那个渠道的**整条评论明细**，连同它的情感、维度标签、关键词、地域，
所以维度表、内容表、大盘表的词云 / 维度 / 热力、下钻表都和评论数同源。
当天有评论但一个维度都没提到，维度就是真的没有，不会去复制前一天的维度。
行存在规则（§7）仍然看**真实明细**：补出来的数据不会让「景区接入之前」凭空多出行。

**另一个连带影响**：修正历史（`repair`）必须回源表，且同样按渠道粒度补齐再重算 ——
core 表存的计数已经是补齐后的值，再滚一次窗口口径就错了。详见 §「历史数据修正」。

**回补是可见的**：跑批报告会打印输出日期上的补齐情况，例如

```
· 缺数回补=先补明细再算指标（渠道当天没评论 → 复制往前最近一天的明细），各窗口最多往前找 1日→5天 / 7日→10天 / ...
· 输出日期上有 3 个「景区·渠道·日」当天没评论：3 个已复制往前最近一天的明细（1 日档最多找 5 天），0 个往前也没找到、保持为 0
```

悄悄回补的数据比缺数更难排查，所以它必须是可见的。
`tests/test_fill_backfill.py` 把上面每一条都钉成了断言。

### 10. 取数区间（需求六.2）

评论**按 `publish_time` 拉取**，不是 `crawl_time`——用采集时间会让补采的历史评论
全都堆到补采当天，趋势图直接失真。默认回溯 `lookback_days=180`（近半年）。

> **长窗口环比在历史不足时会虚高**：算 `comment_count_90dod` 需要 180 天历史
> （本期 90 天 + 上期 90 天），只有 120 天时上期窗口只装了 30 天的数据，
> 分母偏小，环比就会算出 +300% 这种数字。这不是计算错，是历史不够。
> 上线初期建议只看 dod/wow/mom，等历史攒够一年再放开长窗口环比。

> **各周期得分相同 ≠ bug**：窗口一旦都超过「已有数据总天数」，
> `_90d` 和 `_365d` 就都退化成「全部历史的均值」。半年历史下两者必然相等。
> 要让 365d 真正分离，历史至少要有 365 天。跑批报告里会打印这条提示。

### 11. 数据推送（`pusher.py`）

跑批算完、**落库之后**，把结果以 HTTP API 推给下游。默认关闭，不配就不推。

```
POST https://www.demo.com/data/sync
Authorization: Bearer sk_deduehdueh
Content-Type: application/json

{
  "tableName": "ads_trf_social_opinion_comment_core_di",
  "pkId": ["scenic_spot_code", "travel_date", "publish_time"],
  "data": [ {...一行一个对象...} ]
}
```

全部可配（`settings_local.py` 的 `ETL` 段）：

| 配置 | 默认 | 说明 |
|---|---|---|
| `push_enabled` | `False` | 总开关 |
| `push_url` | `""` | 推送地址 |
| `push_headers` | `None` | 请求头，例 `{"Authorization": "Bearer sk_xxx"}`（`Content-Type` 自动带） |
| `push_body_fields` | `{"table": "tableName", "pk": "pkId", "data": "data"}` | **报文字段名**，下游叫别的名字就改这里 |
| `push_extra_fields` | `None` | 额外顶层字段，原样带上，例 `{"source": "opinion_metric_engine"}` |
| `push_table_names` | `None` | 本地表名 → 下游的 `tableName` |
| `push_pk` | `None` | 各表的 `pkId`，不配用 `pusher.DEFAULT_PUSH_PK`（已对齐 DDL 唯一索引） |
| `push_tables` | `None` | 只推其中几张表，不填=全推 |
| `push_batch_size` | 500 | 每个 POST 带多少行 |
| `push_timeout` | 30 | 单次请求超时（秒） |
| `push_retries` / `push_retry_backoff` | 2 / 1.0 | 重试次数与退避基数 |
| `push_dry_run` | `False` | 只组报文不真发，验证配置用 |
| `push_strict` | `False` | `True` 时推送失败也让跑批退出码非 0 |
| `push_cleanup_local` | `True` | 推送成功后删掉本地 CSV（推失败的表留着补推；`push` 补推只删推过的行）。命令行 `--keep-local` 临时保留 |

鉴权令牌**不要写进文件**，用环境变量：`OPINION_PUSH_TOKEN=sk_xxx` 会自动拼成
`Authorization: Bearer sk_xxx`。`OPINION_PUSH_ENABLED` / `OPINION_PUSH_URL` 同理。

命令行：

```bash
python -m engin_cli.cli run --date 20260911 --push                 # 跑完就推（全部表）
python -m engin_cli.cli run --date 20260911 --push all             # 同上，写法二
python -m engin_cli.cli run --date 20260911 --push core,platform   # 只推这两张
python -m engin_cli.cli run --date 20260911 --push-dry-run         # 只组报文，验证配置
python -m engin_cli.cli run --date 20260911 --push --push-strict   # 推失败就算跑批失败
python -m engin_cli.cli push --tables core                         # 从 CSV 补推
```

表名可以用简写，也可以写完整表名，`all` 或不填 = 全部：

| 简写 | 表 |
|---|---|
| `core` | `ads_trf_social_opinion_comment_core_di` |
| `platform` | `ads_trf_social_opinion_comment_platform_di` |
| `dimension`（或 `dim`） | `ads_trf_social_opinion_comment_dimension_score_di` |
| `content`（或 `word`） | `ads_trf_social_opinion_comment_content_di` |
| `drill` | `ads_trf_social_opinion_drill_analysis_di` |
| `macro`（或 `kpi`） | `ads_trf_social_opinion_macro_gran_metric_di` |

打错表名会**立刻报错并列出可选值**，不会静默推 0 张表。

几个刻意的设计：

- **只用标准库 `urllib`**，不引入 requests —— 这套引擎跑在客户机器上，少一个依赖少一份装不上的风险。
- **推送排在落库之后**。推失败了数据还在库里，`push` 子命令补推一次就行；
  反过来推成功但落库失败，下游拿到的就是查无对证的数据。
- **推送失败默认不影响跑批成败**（数据已经落定，推不出去是下游的事），
  但一定会打进跑批报告。要让它影响就开 `push_strict`。
- **4xx 不重试**：报文格式错、鉴权失败，重试一百次还是错，只会白打下游。
  网络错误 / 5xx / 429 才重试，指数退避。
- **一批失败不影响其余批次**，失败批次数会如实报出来。
- **日志不打请求头** —— `Authorization` 里是密钥，落进日志文件就等于泄露。
- **HTTP 200 不等于成功**：网关把业务失败写在响应体里（`{"status": false, "code", "msg", "trace_id"}`）。
  响应体是 JSON 且带 `push_success_field`（默认 `status`）时，它为假就算该批失败、不重试，
  报告带上 code / msg / trace_id。字段名可配（`push_code_field` / `push_message_field` /
  `push_trace_field`），`push_success_field = ""` 关闭检查。
- `np.int64` / `NaN` / `Timestamp` 在发出去之前统一转成原生类型，
  `NaN` 转 `null`：裸 `NaN` 是非法 JSON，下游解析必挂。

### 12. 大盘 KPI 表（需求 2.0，`builders/macro.py`）

新表 `ads_trf_social_opinion_macro_gran_metric_di`，建表语句见 `sql/ads_macro_gran_metric_ddl.sql`。

**一行 = 景区 × 日期 × 周期粒度 × 渠道**（6 个渠道 + `all`），每个组合恰好一行，没数据也出全 0 行。
看板「总览」页的周期页签 + 平台下拉选中的就是这一行：

```sql
select * from ads_trf_social_opinion_macro_gran_metric_di
 where scenic_id = 'PFTSCA01002434' and travel_date = 20260917
   and time_granularity = 'latest_7d' and channel = 'all';
```

> ⚠ 这张表的**占比 / 环比 / 同比是百分数**（× 100，6 位小数，需求原文写法），
> 其余四张表是 [0,1] 的比率。景区列叫 `scenic_id / scenic_name`（同下钻表）。

#### 周期粒度（可配置，`settings.macro_granularities`，domain §1.10）

| `time_granularity` | 中文名 | 类型 | 本期 | 上期（环比分母） |
|---|---|---|---|---|
| `td` | 今日 | rolling 1 | travel_date 当天 | 前一天 |
| `latest_1d` | 近一日 | rolling 1，offset 1 | travel_date 前一天（最近一个完整日） | 再前一天 |
| `latest_7d` / `latest_30d` / `latest_60d` / `latest_90d` | 近7日 / 近30日 / 近60日 / 近90日 | rolling N | [T-N+1, T] | [T-2N+1, T-N] |
| `wtd` | 本周 | week | 周一 ~ T | 上周一起的**同样几天** |
| `mtd` | 本月 | month | 1 号 ~ T | 上月同样几天（上月短就截到月底） |
| `qtd` | 本季度 | quarter | 季初 ~ T | 上季度同样几天 |

- **同比**的对比期 = 本期日期整体减一年（2026-09-01~09-17 → 2025-09-01~09-17）。
  引擎会额外按 `publish_time` 取「去年同期」那一段，不会把一年半的数据全读进来；
  上期早于 `lookback_days` 时（如 `qtd` 的上期）也会补取。
- 加周期：在配置里加一项，如 `{"name": "latest_14d", "label": "近14日", "type": "rolling", "days": 14}`，
  还支持 `year`（本年）。`name` 原样写进 `time_granularity`，`label` 只是中文名，不落表。

#### 缺数回补：先补明细，再算指标

规则同 §9：每个渠道每一天，当天有评论就用当天的明细，没有就复制往前最近一个有评论那天的明细，
然后**本期、上期（环比）、去年同期（同比）三段区间都在补齐后的明细上求和**，区间本身不挪。
all 行 = 各渠道补齐后相加。往前最多找几天按周期查 §9 的表（`backfill_lookback` 改了也跟着生效）：

| 周期 | 借用的档位 | 默认最多往前找 |
|---|---|---|
| `td` / `latest_1d` | 1 日 | 5 天 |
| `latest_7d` / `wtd` | 7 日 | 10 天 |
| `latest_30d` / `mtd` | 30 日 | 20 天 |
| `latest_60d` | 60 日 | 30 天 |
| `latest_90d` / `qtd` | 90 日 | 60 天 |

例：同程本周（周一 9/14 ~ 周四 9/17）一条都没有 → 这 4 天各自复制 9/13 的明细。

因为与 core/platform 是同一套补齐规则，近 N 日的计数逐行对得上（`tests/test_macro.py` 在触发了补齐的日子上对账）：
```
macro latest_7d 某渠道 comment_total == platform.comment_cnt_7d
macro td 的 all 行 comment_total   == core.comment_count
```
词云 / 维度 / 热力也来自同一份补齐后的明细 —— 同一行的数字同源。
取数时每段区间都会往前多取「这个周期最多往前找几天」，区间第一天没评论也补得出来。

> 源表如果只保留了半年，去年同期根本没有数据，补也补不出来，同比仍是 0。
> 自定义的近 N 日如果 N 不在 §9 的表里（如 `latest_15d`），又没在 `backfill_lookback` 里配上限，就不回补。

#### 字段口径

| 字段 | 口径 |
|---|---|
| `overall_sentiment_score` / `pre_` | §1 的得分公式，权重可配（`score_weights`）；本期 / 上期 |
| `comment_total` | all = 各渠道相加；渠道行 = 该渠道 |
| `comment_rate` | all = 0；渠道行 = 渠道总评数 × 100 / 全渠道总评数（各渠道合计 = 100） |
| `*_mom` / `*_yoy` | (本期 - 上期或去年同期) × 100 / 上期或去年同期，**分母为 0 取 0** |
| `*_comment_rate` | 该类计数 × 100 / 本期总评数 |
| `dimension_breakdown` | 6 个一级维度**全量、固定顺序**，本期与上期得分；没有提及的维度取 5 × 中评权重（默认 4.5，同综合得分的约定） |
| `wordcloud_map` | 好/中/差各 Top 30（`macro_wordcloud_top_n`），rate = 该词次数 × 100 / **周期内全部关键词次数**（三组合计）；同一个词可同时出现在两组；同一条评论里同词只算一次。组内按本期次数降序；**次数并列时按近期热度**（该词在本景区近 90 天、截至当天的真实出现次数，不分渠道和情感）降序，再并列按词排序 —— 单日样本少、大量词只出现 1 次时，前 30 挑的是常见说法，不是按字符顺序随机截断 |
| `period_comment_heatmap` | 本期评论按地域计数，按 heat 降序；heat 合计 = 同行 `comment_total` |

**地域**来自评论表 `location`，归一到省级简称（`IP属地：广东` → `广东`、`四川成都` → `四川`、
`北京市` → `北京`），认不出省份的保留原文（`美国`），**没写地域的记 `未知`**（domain §1.11）。
热力图和下钻表 `region` 用同一个值，所以对得上：

- 热力图 heat 合计 = 同口径 `comment_total` = 下钻表当天 `comment_id` 去重数（全渠道、分渠道都成立）；
- 按地域逐个对：某地域 heat = 下钻表 `region` = 该地域的 `comment_id` 去重数，`未知` 也能搜到。

核对 SQL（下钻表一定要带 `travel_date`，否则会把所有日期的评论都算进去）：

```sql
SELECT region, COUNT(DISTINCT channel, work_id, comment_id) AS cnt
FROM ads_trf_social_opinion_drill_analysis_di
WHERE scenic_id = 'PFTSCE01001721' AND travel_date = 20261009   -- 分渠道再加 AND channel = 'weibo'
GROUP BY region ORDER BY cnt DESC;
```

⚠ **不要写 `publish_time = '20261009'`**：下钻表 `publish_time` 是 datetime，MySQL 会把它当成
`2026-10-09 00:00:00` 精确匹配，只能查到补进来的评论（补数统一记零点），当天真实采集的评论
（带具体时分秒）全部漏掉，数就比大盘表少。按天查用 `travel_date = 20261009`，
或者 `publish_time >= '2026-10-09' AND publish_time < '2026-10-10'`。

老数据里 `region` 为空的行要用 `repair` 重跑后才会变成 `未知`。

#### 得分权重可配置

`settings.score_weights = {"positive": 1.0, "neutral": 0.9, "negative": 0.5}`，只写要改的档，
每档须在 [0, 1]。**全引擎一套**：core / dimension / macro 三张表同时生效，改完用 `repair` 重刷历史。

#### 下钻表新增 `region` / `entity_tags`

`region` 同上面的地域归一；`entity_tags` 原样存 JSON 数组（没有就是 `[]`）。
**已有的库先执行 `sql/alter_drill_analysis_region_entity.sql` 再上线**，否则写库报 Unknown column；
下游推送目标表也要同步加这两列。

---

## 幂等与写入策略：按「景区 + publish_time」先删除、再写入

每次运行，六张表一律**先删除本次范围内的旧行，再写入新算出来的行**，不再依赖唯一索引、也不再用 upsert：

| 表 | 删除条件 |
|---|---|
| `core_di` / `platform_di` / `dimension_score_di` / `content_di` | `scenic_spot_code IN (本次景区) AND publish_time IN (本次日期)` |
| `macro_gran_metric_di` | `scenic_id IN (本次景区) AND publish_time IN (本次日期)` |
| `drill_analysis_di` | `scenic_id IN (本次景区) AND publish_time` 落在本次日期那几天内（`>= 当天 0 点 且 < 次日 0 点`） |

- **删除范围 = 本次跑的景区 × 本次跑的日期**，不是「新数据里出现过的 publish_time」。
  重算后某天某张表没有行了（比如某个词不再出现），旧行照样删掉，不会残留；这次某张表一行都没算出来也会执行删除。
- 下钻表的 `publish_time` 是**评论自己的发布时间**（datetime），按值相等删只能删到时间一模一样的行，
  所以按「落在那一天内」删。其余五张表的 `publish_time` 是 `yyyyMMdd` 字符串，与日期一一对应。
- **删除和写入在同一个事务里**（`db.replace_rows`）：写入失败整体回滚，不会出现「旧数据删了、新数据没写进去」的空窗。
- 景区 = 命令行 `--scenic` 指定的；不指定时 `run` 本来就是逐个景区跑，每次只删当前景区。
- `settings.dimension_upsert` 与 `sql/alter_dimension_uniquekey.sql` 已不再需要（执行过也没有影响）。
- 只影响写库；推送仍按 pkId 由下游 upsert，不受影响。

`publish_time` 默认写 `yyyyMMdd`，与现有数据保持一致 —— 它进了三张表的唯一索引，
格式一变就会跟历史数据产生重复行。要按 DDL 注释改成 `YYYY-MM-DD HH:mm:ss`，
把 `settings.publish_time_format` 改掉并同步清洗历史数据。

---

## 历史数据修正（得分口径变更后）

得分口径从 v1 换到 v2 之后，历史数据里的分数还是旧值，必须重刷一遍：

```bash
# 先看影响面，不写库
python -m engin_cli.cli repair --start-date 20260101 --end-date 20260901 --dry-run --show-preview

# 确认后执行
python -m engin_cli.cli repair --start-date 20260101 --end-date 20260901

# 只修 core（不需要源表，最快）
python -m engin_cli.cli repair --tables core --start-date 20260101 --end-date 20260901
```

**只更新这 27 个字段，别的一列不碰**：

| 表 | 字段 | 数量 |
|---|---|---|
| `core_di` | `emotional_score` + `_7d/_30d/_60d/_90d/_365d` | 6 |
| `dimension_score_di` | `dimension_{1,2,3}_score` + `_7d/_14d/_30d/_60d/_90d/_365d` | 21 |

用 `UPDATE ... SET <这几列>` 而不是整行 upsert：评论数、好评率、环比、词频都不受
口径变更影响，重刷整行既慢又会把 `etl_time` 冲掉，出问题时也分不清是口径改的还是重算错的。

**两张表的数据来源不同**（这是刻意的）：

- `core` —— 直接从 **core 表自己的计数列**重算。好/中/差/总数都在表里存着，
  窗口累计滚一遍就有了，**不需要回源表**。所以哪怕源表只留了半年，
  一年前的 `emotional_score_365d` 也能修对。
- `dimension` —— 维度提及数没有落表（表里只有分数），**必须回源表重算**，
  可修正范围因此受源表保留期限制。源表已清理的日期改不到，跑批报告会说明。

**修正结果 == 用新口径重跑一遍的结果**，这是硬性不变量。两边的 `lookback_days`
必须相同 —— 跑批只看了 180 天、修正却读了 365 天的话，`emotional_score_365d`
必然对不上，然后两边来回改。想让 365d 真的是一年，把 `lookback_days` 调大，两边一起生效。

修正时同样铺网格、同样先补明细再滚动（同样往前多读补齐来源），动作与 builders 逐步对齐。

⚠ **`backfill_mode="fill"`（默认）下两张表都必须回源表重算。** core 表里存的 `comment_count`
已经是 1 日档补齐后的值（§9），而 7 日、30 日档各用各的上限补齐，不是 1 日值简单相加 ——
拿表里的计数再滚一次窗口，补出来的天会被当成「真实有数据」再往后补一遍，口径就错了。
`backfill_mode="off"` 时 core 表存的就是真实计数，可以直接用表内计数重算，不需要源表。
不变量破了的话，`repair` 和 `run` 会互相把对方的值改回去，来回打架；
`tests/test_repair.py` 的 `test_repair_after_run_changes_nothing_*` 盯着它。

跑完会打印每张表「扫了多少行 / 多少行分数变了 / 最大变化多少分 / 实际写回多少行」，
`--show-preview` 打出前 10 行的 **旧值 / 新值 / 差值** 三列对照（含维度路径等定位列）。

---

## 需求 5.2：下钻分析表（词 → 评论 → 作品）

新表 `ads_trf_social_opinion_drill_analysis_di`，建表语句见 `sql/ads_drill_analysis_ddl.sql`。

一行 = 一个词 × 一条评论。命中 3 个词的评论就有 3 行。带 `work_url`，前端直接跳转。
查询见 `sql/queries.sql` 最后两段（点词看评论 / 点作品反查词）。

> ⚠ **这张表的景区列名、渠道列名跟另外四张不一样**：
>
> | | 景区编号 | 景区名称 | 渠道编码 | 渠道名称 |
> |---|---|---|---|---|
> | 下钻表 | `scenic_id` | `scenic_name` | `channel` | `channel_name` |
> | 其余四张 ADS 表 | `scenic_spot_code` | `scenic_spot_name` | `platform_code` | `platform_name` |
>
> 下钻表跟源表 `src_opinion_social_work_di` 同名，关联作品表时少一次改名。
> 渠道列是客户 2026-10 改的表：已经建好的表执行一次 `sql/alter_drill_analysis_channel.sql`
> （只改列名，数据保留），下游推送目标表同样要改；`push_pk` 里下钻表写了 `platform_code` 的要换成 `channel`。
> 引擎内部（`comment_facts` 等）统一用 `scenic_spot_code`，**只在这张表的输出边界改名**，
> 免得为了一张表把整条链路都动一遍。两张表关联时记得对齐这两个列名。
> `tests/test_schema.py` 把这个差异钉死了：下钻表里不许出现 `scenic_spot_*` / `platform_*`，
> 另外四张里不许出现裸的 `scenic_id`。

缺数回补（业务确认「包含」）：某渠道在输出日期当天没评论 → 复制往前最近一天（当日档，最多找 5 天）
那个渠道的全部评论，作为这一天的下钻明细。复制行的 `travel_date` = 这一天，`publish_time` =
这一天 00:00:00（客户要求用跑数日期；不保留原评论的时分秒，否则上午跑批时 23:25 这种时刻还在「未来」，
下游按「当天到现在」查会查不到；固定值也保证重跑一致，`publish_time` 在推送 pkId 里），
`detail_uk` 带上这一天，不和原评论撞键。

与大盘表对账（客户要求，自检里卡死）：某天某渠道，下钻表按评论去重的条数 == 大盘表当日周期（`td`）
该渠道的 `comment_total`，各渠道合计 == `all` 行。所以**没有关键词的评论也出一行**
（`emotion_word` 为空、`word_source = none`），看板点词查询按词过滤，碰不到这些行。看板点内容表里补出来的词，也能列出对应的评论。按 `travel_date` 幂等重写当期即可。词表大时可以开 `content_top_words`
只保留每日 TopN 的词，明细表跟着一起瘦身。

### `content_snippet` 掩码（domain 第 9 章）

默认**只保留命中的关键词，其余内容一律隐藏**：

```
原文      我是体力一般，来回两个半小时左右
关键词    体力一般 / 来回两个半小时
掩码后    **体力一般**来回两个半小时**
```

被隐藏的每一段换成**一个固定掩码串**（默认 `**`），不是按字数出星号 ——
「我是」2 字、「，」1 字、「左右」2 字，掩码后都是同样的 `**`。
按字数出星号会泄露原文长度，拼上关键词位置能还原出不少信息。要长度就调 `mode="char"`。

| 配置 | 默认 | 说明 |
|---|---|---|
| `drill_mask_content` | `True` | 掩码开关，关掉就存原文 |
| `drill_mask_token` | `"**"` | 每段隐藏内容换成什么 |
| `drill_mask_mode` | `"run"` | `run` 一段一个掩码串 / `char` 一字一个掩码字符（泄露长度） |
| `drill_mask_scope` | `"comment"` | `comment` 保留这条评论的全部关键词 / `word` 只保留当前行那个词 |
| `drill_mask_on_no_match` | `"mask_all"` | 关键词一个都没对上时：整条隐藏 / `keep_raw` 保留原文 |
| `drill_mask_ignore_case` | `True` | 匹配忽略大小写，输出仍用原文大小写 |

几个决定：

- **默认按「这条评论的全部关键词」掩码**，不是只按当前行那一个词。一行 = 一个词 × 一条评论，
  按单词掩码的话同一条评论在不同行里会忽长忽短，读的人以为是两条不同的评论。
  要每行只高亮自己那个词就把 `drill_mask_scope` 设成 `"word"`。
- **同一个词出现多次，每一次都保留**。只留第一次会让人以为只提过一次。
- **区间重叠先合并**：关键词可能互相包含（「体力」「体力一般」）或交叉，不合并会切出空片段。
- **先截断再掩码**。掩码串长度跟原文无关，反过来做 `drill_content_limit` 就失去意义了；
  被截断切掉一半的关键词自然匹配不上，跟着进掩码段，不会露出半个词。
- ⚠ **关键词对不上原文的情况真实存在**。实测八大处 2 万条评论：98.1% 的关键词能在原文里
  原样找到，1.9% 找不到 —— 大模型把词抽象过了（原文「每次感受都不错」，关键词「感受不错」；
  原文「景色很漂亮，人也很多」，关键词「人很多」）。这类评论一个词都对不上，掩码后就是
  光秃秃一个 `**`。默认就这么存（隐藏优先）；觉得这样的行没信息量，就把
  `drill_mask_on_no_match` 设成 `"keep_raw"`。
  **每次跑批都会在报告里打印掩码了多少条、其中多少条一个词都没对上。**

---

## 自检

`validate.py` 每次跑批都跑，全部是**恒等关系**断言，与具体数值无关：

- core：好+中+差 = 总数 / 三个率之和 = 1 / 得分在 [0,10] / 均分在 [1,5] / (景区,日期) 不重复
- platform：各平台之和 = core 总数 / 平台占比合计 = 1 / 各类占比在 [0,1] / 唯一键不重复
- dimension：得分在 [0,10] / 同一级维度当日得分唯一（层级自洽）/ 维度路径不重复
- content：(景区,日期,词) 不重复（撞唯一索引）/ emotion_type 合法 /
  词频不超过当日评论数 / 每组都有 rank=1
- macro：(景区,日期,周期,渠道) 不重复 / 好+中+差 = 总数 / all 行 = 各渠道之和 /
  各渠道 comment_rate 合计 = 100 / 每个周期都有 all + 全部渠道 / 三个 JSON 列可解析

任一项不通过说明**计算逻辑**有问题，不是数据问题。退出码 1，可直接接 CI。

---

## 目录与文件职责

```
opinion_metric_engine/          项目根
├── engin_cli/                  引擎包（跑批入口 python -m engin_cli.cli）
│   ├── metric_calc_domain.py   ★ 口径唯一真源（公式/阈值/字段命名/计数规则）
│   └── builders/               四张 ADS 表各自的计算（只搬运，不算口径）
├── sql/                        建表语句 + 看板取数参考 SQL
├── scripts/                    样例数据生成
├── tests/                      单测（98 项，含端到端、口径守卫、历史修正、行存在规则）
├── settings_local.example.py   连接配置模板（复制为 settings_local.py）
└── requirements.txt
```

| 文件 | 作用 |
|---|---|
| **`engin_cli/metric_calc_domain.py`** | **全部指标口径的唯一落地处**：公式、阈值、字段命名、计数规则。改口径只改这里 |
| `engin_cli/settings.py` | 运行期配置（库连接、回溯天数、回补上限、写入策略） |
| `engin_cli/windows.py` | **唯一一份**多窗口滚动 + 明细补齐实现，各 builder 全部复用 |
| `engin_cli/normalize.py` | JSON 解析、情感归一、维度与关键词炸开 |
| `engin_cli/source.py` | 源表抽取（MySQL / CSV 两种源，同构） |
| `engin_cli/builders/*.py` | 四张 ADS 表各自的计算，列名严格等于 DDL |
| `engin_cli/drill_analysis.py` | 需求 5.2 下钻分析表 |
| `engin_cli/builders/macro.py` | 需求 2.0 大盘 KPI 表（周期粒度 × 渠道） |
| `engin_cli/repair.py` | 历史数据修正：按新口径重刷得分字段（只 UPDATE 得分列） |
| `engin_cli/validate.py` | 口径自检 |
| `engin_cli/pusher.py` | 数据推送：算完后以 HTTP API 推给下游（可配置，默认关闭） |
| `engin_cli/analytics.py` | 看板取数参考实现（BI 照抄这里） |
| `engin_cli/loader.py` | 幂等写入 |
| `engin_cli/pipeline.py` | 编排 |
| `engin_cli/cli.py` | 命令行 |
| `sql/ads_ddl_reference.sql` | 四张目标表的建表语句（原样保留，测试据此断言） |
| `sql/ads_drill_analysis_ddl.sql` | 5.2 下钻分析表建表语句（含 2.0 新增的 region / entity_tags） |
| `sql/alter_drill_analysis_region_entity.sql` | 已有下钻表补 region / entity_tags 两列 |
| `sql/ads_macro_gran_metric_ddl.sql` | 2.0 大盘 KPI 表建表语句 |
| `sql/alter_dimension_uniquekey.sql` | 给维度表补唯一索引（可选） |
| `sql/queries.sql` | 看板各组件的取数 SQL |

---

## 已知取舍 / 待确认

1. **`emotion_value` 的含义**：本实现按 DDL 注释「近 N 日出现次数」落表，
   突增量由两期相减现算。若你们的既定口径是「增量」，改 `builders/content.py` 一处即可。
2. **`daily_rating`（统计时刻均分）**：DDL 只写了「均分」没给公式，
   本实现按 正 5 星 / 中 3 星 / 负 1 星 加权，值域 [1,5]，与 0~10 的
   `emotional_score` 是两个刻度，看板上不要互相校验。口径要改在 `metric_calc_domain.star_rating`。
3. **platform 表 DDL 的 `好评占比=总评数/好评数`** 注释写反了，
   实现按数学上正确的 `好评数/总评数`。
4. **维度表 90d/365d 的 DDL 注释**是「TOP1/TOP2/TOP3 得分」，与短窗口的层级口径不一致，
   本实现统一按层级口径。
5. **综合得分不一定落在各维度得分之间。** v2 去掉置信度权重后两者更接近了，
   但仍不保证落在区间内：综合得分按「全部评论」的好中差占比算，维度得分按
   「该维度提及」的占比算，两个样本的构成本来就不同（一条评论可能命中 0 个或 3 个维度）。
   如果看板要求区间关系成立，需要在一级维度层做占比对齐，是明确的口径变更，先确认再做。
6. **`entity_tags`** 目前不参与任何指标。如果要做「景点项目维度的口碑排行」，
   它是现成的数据源，可以再加一张表。
7. 超大数据量（单景区日均 10 万条以上）时，`normalize` 的逐行 Python 循环会成为瓶颈，
   届时把 `build_dimension_facts` / `build_keyword_facts` 换成
   `pd.json_normalize` + `explode` 的向量化写法即可，接口不变。
8. **大盘表（需求 2.0）的几处口径是本实现的理解，需要业务方确认**：
   - 需求第 4 条「每个景区/周期/渠道 我预想的是只有一条数据，因为我设计的是」原文没写完，
     本实现按「景区 × 日期 × 周期 × 渠道 唯一」落表。
   - 词云 rate 的分母取「周期内全部关键词次数（好中差三组合计）」，不是各组自己的合计。
   - 没有提及的维度、没有评论的渠道，得分取 5 × 中评权重（默认 4.5），沿用 §1 的约定；
     看板如果要显示「暂无数据」，以 `comment_total = 0` 判断。
   - 同比的去年同期 = 本期日期整体减一年（不是「去年第 N 周」）。
