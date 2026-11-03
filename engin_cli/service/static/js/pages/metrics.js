import { ref, reactive, onMounted, computed, watch } from 'vue'
import { api, GRANULARITIES, CHANNELS, fmtNum, notify } from '../api.js'
import { Ring, Wave } from '../charts.js'

const WORD_GROUPS = [['positiveWord', '好评', 'var(--green)'], ['neutralWord', '中评', '#8682a0'],
  ['negativeWord', '差评', 'var(--red)']]

export default {
  components: { Ring, Wave },
  setup() {
    const scenics = ref([]), err = ref(''), loading = ref(false)
    const q = reactive({ scenic: '', date: '', granularity: 'td', channel: 'all' })
    const cell = ref(null), trend = ref([])
    const load = async () => {
      if (!q.scenic) return
      loading.value = true
      try {
        const p = new URLSearchParams({ scenic: q.scenic, date: q.date, granularity: q.granularity })
        cell.value = await api('/api/metrics/cell?' + p)
        if (!q.date && cell.value.date) q.date = String(cell.value.date)
        trend.value = await api('/api/metrics/trend?' + new URLSearchParams(
          { scenic: q.scenic, channel: q.channel, granularity: 'td', end: q.date }))
        err.value = ''
      } catch (e) { err.value = e.message } finally { loading.value = false }
    }
    onMounted(async () => {
      try {
        scenics.value = await api('/api/metrics/scenics')
        if (scenics.value.length) { q.scenic = scenics.value[0].scenic_id; load() }
      } catch (e) { err.value = e.message }
    })
    watch(() => [q.scenic, q.granularity], () => load())
    watch(() => q.channel, async () => {
      try { trend.value = await api('/api/metrics/trend?' + new URLSearchParams(
        { scenic: q.scenic, channel: q.channel, granularity: 'td', end: q.date })) } catch (e) { notify(e.message, 'err') }
    })
    const row = computed(() => (cell.value?.rows || []).find(r => r.channel === q.channel) || null)
    const channels = computed(() => (cell.value?.rows || []).map(r => r.channel))
    const words = computed(() => {
      const w = row.value?.wordcloud_map || {}
      const all = WORD_GROUPS.flatMap(([k, , c]) => (w[k] || []).map(x => ({ ...x, color: c })))
      const max = Math.max(1, ...all.map(x => x.rate || 0))
      return all.sort((a, b) => a.word.localeCompare(b.word)).map(x => ({ ...x, size: 12 + (x.rate / max) * 18 }))
    })
    const regions = computed(() => {
      const h = (row.value?.period_comment_heatmap || []).slice(0, 12)
      const max = Math.max(1, ...h.map(x => x.heat))
      return h.map(x => ({ ...x, pct: x.heat / max * 100 }))
    })
    const dims = computed(() => row.value?.dimension_breakdown || [])
    const sentParts = computed(() => row.value ? [
      { value: row.value.positive_comment_cnt, label: '好评', colors: ['#7fe0d4', '#21b8a6'], icon: 'tick' },
      { value: row.value.neutral_comment_cnt, label: '中评', colors: ['#c2bedf', '#9692ad'], icon: 'comment' },
      { value: row.value.negative_comment_cnt, label: '差评', colors: ['#f6a4b8', '#ee5a7c'], icon: 'clear' }] : [])
    const md = d => { const s = String(d); return s.slice(4, 6) + '/' + s.slice(6) }
    const trendPts = computed(() => trend.value.map(x => ({ label: md(x.travel_date), value: x.comment_total })))
    const scorePts = computed(() => trend.value.map(x => ({ label: md(x.travel_date), value: x.overall_sentiment_score })))
    const chCols = [
      { key: 'channel', title: '渠道', width: 160 }, { key: 'comment_total', title: '评论数', align: 'r' },
      { key: 'rate', title: '占比', align: 'r' }, { key: 'score', title: '得分', align: 'r' },
      { key: 'pos', title: '好评', align: 'r' }, { key: 'neu', title: '中评', align: 'r' },
      { key: 'neg', title: '差评', align: 'r' }, { key: 'mom', title: '环比', align: 'r' }]
    const pct = v => v === null || v === undefined ? '-' : (v > 0 ? '+' : '') + fmtNum(v, 2) + '%'
    return { scenics, err, loading, q, cell, row, channels, words, regions, dims, sentParts, trendPts, scorePts,
             load, pct, fmtNum, GRANULARITIES, CHANNELS, WORD_GROUPS, chCols }
  },
  template: `
  <div>
    <div class="card toolbar-card stick">
      <div class="toolbar wrap">
        <ScenicPicker v-model="q.scenic" single placeholder="选择景区" width="300px" />
        <DatePicker v-model="q.date" placeholder="默认最新一天" @change="load" />
        <button class="btn primary sm" @click="load"><Icon name="search" :size="14"/>查询</button>
        <div class="seg"><button v-for="g in GRANULARITIES" :key="g[0]" :class="{on: q.granularity===g[0]}"
          @click="q.granularity=g[0]">{{ g[1] }}</button></div>
      </div>
      <div class="tabs"><button v-for="c in channels" :key="c" :class="{on: q.channel===c}" @click="q.channel=c">
        {{ CHANNELS[c] || c }}</button></div>
    </div>
    <div v-if="err" class="alert red">{{ err }}</div>
    <div v-if="!err && cell && !cell.rows.length" class="card"><Empty text="这一天 / 这个周期没有数据"/></div>
    <div v-if="!err && !scenics.length && !loading" class="card"><Empty text="大盘表里还没有数据（或数据库没配好）"/></div>

    <template v-if="row">
      <div class="grid kpis">
        <div class="card kpi hero"><span>评论数 · {{ cell.date }}</span><b>{{ fmtNum(row.comment_total) }}</b>
          <small>环比 {{ pct(row.comment_total_mom) }} · 同比 {{ pct(row.comment_total_yoy) }}</small></div>
        <div class="card kpi"><span>综合情感得分</span><b>{{ fmtNum(row.overall_sentiment_score, 2) }}</b>
          <small>上期 {{ fmtNum(row.pre_overall_sentiment_score, 2) }}</small></div>
        <div class="card kpi"><span>好评</span><b class="t-green">{{ fmtNum(row.positive_comment_cnt) }}</b>
          <small>{{ fmtNum(row.positive_comment_rate, 2) }}% · 环比 {{ pct(row.positive_comment_mom) }}</small></div>
        <div class="card kpi"><span>差评</span><b class="t-red">{{ fmtNum(row.negative_comment_cnt) }}</b>
          <small>{{ fmtNum(row.negative_comment_rate, 2) }}% · 环比 {{ pct(row.negative_comment_mom) }}</small></div>
      </div>

      <div class="grid two-one">
        <div class="card"><div class="card-head"><h3>每日评论数<small>td · {{ CHANNELS[q.channel] || q.channel }}</small></h3></div>
          <Wave :points="trendPts" :height="190" :labels="false" :ticks="7" :fmt="v => fmtNum(v)" /></div>
        <div class="card center"><div class="card-head"><h3>情感构成</h3></div>
          <Ring :parts="sentParts" :size="200" :thickness="20" :gap="12" :hole="0.52" :value="fmtNum(row.comment_total)" sub="条评论" />
          <div class="mini-legend">
            <span><i class="dot" style="background:var(--green)"></i>好 {{ fmtNum(row.positive_comment_cnt) }}</span>
            <span><i class="dot" style="background:#9692ad"></i>中 {{ fmtNum(row.neutral_comment_cnt) }}</span>
            <span><i class="dot" style="background:var(--red)"></i>差 {{ fmtNum(row.negative_comment_cnt) }}</span></div></div>
      </div>

      <div class="grid two">
        <div class="card"><div class="card-head"><h3>词云</h3>
          <div class="legend"><template v-for="g in WORD_GROUPS" :key="g[0]"><i :style="{background:g[2]}"></i><span>{{ g[1] }}</span></template></div></div>
          <div class="cloud"><span v-for="w in words" :key="w.word+w.color" :style="{fontSize: w.size+'px', color: w.color}"
            :title="w.word + ' ' + w.rate + '%'">{{ w.word }}</span>
            <Empty v-if="!words.length" small text="没有关键词"/></div></div>
        <div class="card"><div class="card-head"><h3>地域分布<small>前 12</small></h3></div>
          <div v-for="r in regions" :key="r.region" class="hbar"><span class="hbar-label">{{ r.region }}</span>
            <div class="hbar-track"><div class="hbar-fill" :style="{width: r.pct+'%'}"></div></div>
            <b>{{ fmtNum(r.heat) }}</b></div>
          <Empty v-if="!regions.length" small text="没有数据"/></div>
      </div>

      <div class="grid two">
        <div class="card"><div class="card-head"><h3>维度得分<Tip text="条 = 本期，橙色竖线 = 上期"/></h3></div>
          <div v-for="d in dims" :key="d.dimension1" class="hbar"><span class="hbar-label">{{ d.dimension1 }}</span>
            <div class="hbar-track"><div class="hbar-fill" :style="{width: (d.dimension1Score/5*100)+'%'}"></div>
              <div class="hbar-prev" :style="{left: (d.preDimension1Score/5*100)+'%'}"></div></div>
            <b>{{ fmtNum(d.dimension1Score, 2) }}</b></div></div>
        <div class="card"><div class="card-head"><h3>每日得分<small>td · 综合情感得分</small></h3></div>
          <Wave :points="scorePts" :height="190" :labels="false" :ticks="7" :colors="['#21b8a6', '#8be3ef']" :fmt="v => fmtNum(v, 2)" /></div>
      </div>

      <div class="card"><div class="card-head"><h3>各渠道对比<Tip text="点一行切换上面图表的渠道"/></h3></div>
        <STable :columns="chCols" :data="cell.rows" row-key="channel" clickable @row-click="r => q.channel = r.channel">
          <template #channel="{ row }"><span class="s-name"><Avatar :text="CHANNELS[row.channel] || row.channel" :color="row.channel==='all' ? 'indigo' : null"/>
            <b :class="{'t-primary': row.channel===q.channel}">{{ CHANNELS[row.channel] || row.channel }}</b></span></template>
          <template #comment_total="{ row }">{{ fmtNum(row.comment_total) }}</template>
          <template #rate="{ row }">{{ row.channel==='all' ? '-' : fmtNum(row.comment_rate, 2)+'%' }}</template>
          <template #score="{ row }">{{ fmtNum(row.overall_sentiment_score, 2) }}</template>
          <template #pos="{ row }">{{ fmtNum(row.positive_comment_cnt) }}</template>
          <template #neu="{ row }">{{ fmtNum(row.neutral_comment_cnt) }}</template>
          <template #neg="{ row }">{{ fmtNum(row.negative_comment_cnt) }}</template>
          <template #mom="{ row }"><Tag :color="(row.comment_total_mom||0) >= 0 ? 'green' : 'pink'">{{ pct(row.comment_total_mom) }}</Tag></template>
        </STable></div>
    </template>
  </div>`,
}
