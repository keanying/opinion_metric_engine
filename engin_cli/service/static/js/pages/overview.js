import { ref, onMounted, onUnmounted, computed } from 'vue'
import { api, TRIGGER, KIND, duration, WEEK } from '../api.js'
import { Ring, Wave, PillBars, DayLine } from '../charts.js'

const C = {
  success: ['#8b6cff', '#4f7cff'], blocked: ['#ffc15e', '#ff8a3d'],
  failed: ['#ff7aa2', '#f2456e'], other: ['#3a4699', '#25306f'],
}
const pct = (a, b) => (b ? Math.round(a / b * 1000) / 10 : 0)

export default {
  components: { Ring, Wave, PillBars, DayLine },
  setup() {
    const d = ref(null), err = ref(''), pick = ref(-1)
    let timer
    const load = async () => {
      try { d.value = await api('/api/overview'); err.value = '' } catch (e) { err.value = e.message }
    }
    onMounted(() => { load(); timer = setInterval(load, 10000) })
    onUnmounted(() => clearInterval(timer))

    const sum = (arr, k) => arr.reduce((a, x) => a + (x[k] || 0), 0)
    const wk = computed(() => {
      const days = d.value?.daily || []
      const cur = days.slice(-7), prev = days.slice(0, 7)
      const s = sum(cur, 'success'), b = sum(cur, 'blocked'), f = sum(cur, 'failed'), o = sum(cur, 'other')
      const total = s + b + f + o
      const ps = sum(prev, 'success'), pt = ps + sum(prev, 'blocked') + sum(prev, 'failed') + sum(prev, 'other')
      const rate = total ? pct(s, total) : null, prate = pt ? pct(ps, pt) : null
      return { s, b, f, o, total, rate, prate, diff: rate !== null && prate !== null ? Math.round((rate - prate) * 10) / 10 : null,
               pf: sum(prev, 'failed') + sum(prev, 'blocked'), ps }
    })
    const ringParts = computed(() => [
      { value: wk.value.s, label: '成功', colors: C.success, icon: 'tick' },
      { value: wk.value.b, label: '已回退', colors: C.blocked, icon: 'alert' },
      { value: wk.value.f, label: '失败', colors: C.failed, icon: 'clear' },
      { value: wk.value.o, label: '取消 / 进行中', colors: C.other, icon: 'clock' }])
    const stats = computed(() => {
      const t = wk.value.total || 1
      return [['成功', wk.value.s, '#6c63ff'], ['回退', wk.value.b, '#ff9a3d'],
              ['失败', wk.value.f, '#f2456e'], ['其他', wk.value.o, '#2f3a8f']]
        .map(([l, v, c]) => ({ l, v, c, p: wk.value.total ? pct(v, t) : 0 }))
    })
    // 近 7 天平均耗时曲线
    const dur7 = computed(() => (d.value?.durations || []).slice(-7))
    const wavePts = computed(() => dur7.value.map(x => ({ label: WEEK[new Date(x.date + 'T00:00').getDay()], value: x.avg })))
    const selIdx = computed(() => pick.value >= 0 ? pick.value : wavePts.value.length - 1)
    const durSel = computed(() => dur7.value[selIdx.value] || null)
    const durDelta = computed(() => {
      const all = d.value?.durations || []
      const a = all.slice(-7).filter(x => x.n), b = all.slice(0, 7).filter(x => x.n)
      if (!a.length || !b.length) return null
      const ma = a.reduce((s, x) => s + x.avg, 0) / a.length, mb = b.reduce((s, x) => s + x.avg, 0) / b.length
      return mb ? Math.round((ma - mb) / mb * 1000) / 10 : null
    })
    const series = [
      { key: 'other', label: '其他', color: 'linear-gradient(180deg,#3a4699,#25306f)' },
      { key: 'failed', label: '失败', color: 'linear-gradient(180deg,#ff7aa2,#f2456e)' },
      { key: 'blocked', label: '回退', color: 'linear-gradient(180deg,#ffc15e,#ff8a3d)' },
      { key: 'success', label: '成功', color: 'linear-gradient(180deg,#8b6cff,#5b6cff)' },
    ]
    const barRows = computed(() => (d.value?.daily || []).map(x => ({ label: x.date.slice(5).replace('-', '/'), values: x })))
    const fmtDur = v => !v ? '0s' : v < 60 ? `${v}s` : `${Math.floor(v / 60)}m${Math.round(v % 60)}s`
    const range7 = computed(() => dur7.value.length ? `${dur7.value[0].date.slice(5)} ~ ${dur7.value[dur7.value.length - 1].date.slice(5)}` : '')
    const cols = [
      { key: 'title', title: '执行', width: 340 }, { key: 'trigger', title: '触发', width: 110 },
      { key: 'status', title: '状态', width: 110 }, { key: 'created_at', title: '时间', width: 150 },
      { key: 'dur', title: '耗时', width: 90 }, { key: 'op', title: '', width: 56, align: 'r' }]
    return { d, err, pick, wk, ringParts, stats, wavePts, selIdx, durSel, durDelta, series, barRows, fmtDur, range7,
             cols, TRIGGER, KIND, duration }
  },
  template: `
  <div>
    <div v-if="err" class="alert red">{{ err }}</div>
    <div v-if="d" class="ov">
      <!-- 左：今日执行 + 分段环 + 四项占比 -->
      <section class="ov-left">
        <div class="card balance">
          <div class="chip-card"><small>{{ d.running ? 'RUNNING' : 'IDLE' }}</small><b>{{ d.running ? '№ ' + d.running : '空闲' }}</b></div>
          <div class="grow"><span class="cap">今日执行</span>
            <div class="big">{{ d.today }}<em> 次</em></div></div>
          <router-link to="/runs" class="round-btn" title="执行记录"><Icon name="down" :size="14" :sw="2.6"/></router-link>
        </div>
        <div class="ring-box">
          <Ring :parts="ringParts" :size="300" label="近 7 天成功率"
            :value="wk.rate === null ? '—' : wk.rate + '%'" :sub="'共 ' + wk.total + ' 次'" />
        </div>
        <div class="stat4">
          <div v-for="s in stats" :key="s.l"><span class="cap">{{ s.l }}</span><b>{{ s.p }}%</b>
            <i class="ubar"><em :style="{width: Math.max(s.p, 4)+'%', background: s.c}"></em></i></div>
        </div>
      </section>

      <!-- 中：执行耗时曲线 + 执行次数柱 -->
      <section class="ov-mid">
        <div class="card week">
          <div class="week-head">
            <span class="icon-sq grad"><Icon name="bolt" :size="18" :sw="2.2"/></span>
            <div class="grow"><h3>执行耗时</h3><span class="cap">近 7 天 · {{ range7 }}</span></div>
            <div class="r"><div class="big sm">{{ fmtDur(durSel ? durSel.avg : 0) }}</div>
              <span v-if="durDelta !== null" class="delta" :class="durDelta <= 0 ? 'up' : 'down'">
                {{ durDelta > 0 ? '+' : '' }}{{ durDelta }}% 较上周</span>
              <span v-else class="cap">平均 / 次</span></div>
          </div>
          <Wave :points="wavePts" :selected="selIdx" :height="130" :fmt="fmtDur" @select="i => pick = i" />
          <router-link v-if="d.attention.length" :to="'/runs/' + d.attention[0].id" class="callout">
            <b>最近一次{{ d.attention[0].status === 'blocked' ? '质量回退' : '失败' }}</b>
            <span>{{ d.attention[0].task_name || '临时执行' }} · {{ d.attention[0].created_at.slice(5,16) }} »</span></router-link>
          <div v-else class="callout calm"><b>运行平稳</b><span>最近没有失败和质量回退</span></div>
        </div>

        <div class="card expenses">
          <div class="exp-head">
            <div class="grow"><h3>执行次数</h3><span class="cap">近 14 天 · 跑批</span></div>
            <div class="kpi-mini"><span class="cap">成功</span><b>{{ wk.s }}</b>
              <em :class="wk.s >= wk.ps ? 'up' : 'down'">{{ wk.s >= wk.ps ? '+' : '' }}{{ wk.s - wk.ps }} 较上周</em></div>
            <div class="kpi-mini"><span class="cap">失败 / 回退</span><b>{{ wk.f + wk.b }}</b>
              <em :class="wk.f + wk.b <= wk.pf ? 'up' : 'down'">{{ wk.f + wk.b - wk.pf > 0 ? '+' : '' }}{{ wk.f + wk.b - wk.pf }} 较上周</em></div>
          </div>
          <PillBars :rows="barRows" :series="series" :height="150" :fmt="v => v + ' 次'" />
        </div>
      </section>

      <!-- 右：下次执行 + 时间轴 + 需要关注 + 小结 + 主按钮 -->
      <section class="card ov-right">
        <div class="next-head">
          <div class="grow"><div class="big">{{ d.upcoming.length ? d.upcoming[0].at.slice(11) : '--:--' }}</div>
            <span class="cap">{{ d.upcoming.length ? '下次执行 · ' + d.upcoming[0].at.slice(5,10) : (d.scheduler.enabled ? '没有启用定时的任务' : '定时已暂停') }}</span></div>
          <span class="icon-sq green"><Icon name="clock" :size="18" :sw="2.2"/></span>
        </div>
        <div class="next-task" v-if="d.upcoming.length">{{ d.upcoming[0].task_name }}</div>
        <DayLine :slots="d.today_slots" :runs="d.today_runs" :now="d.now.slice(11,16)" />
        <div class="sub-head"><span class="cap">需要关注</span><router-link to="/runs" class="btn xs">查看全部</router-link></div>
        <div v-for="r in d.attention" :key="r.id" class="person" @click="$router.push('/runs/'+r.id)">
          <span class="ring-av" :class="r.status"><Avatar :text="r.task_name || '临'" :size="34"/></span>
          <div class="grow"><b>{{ r.task_name || '临时执行' }}</b>
            <span :class="r.status === 'blocked' ? 't-orange' : 't-red'">{{ r.status === 'blocked' ? '已回退' : '失败' }} · {{ r.created_at.slice(5,16) }}</span></div>
        </div>
        <div v-if="!d.attention.length" class="person calm"><span class="ring-av"><Avatar icon="tick" color="green" :size="34"/></span>
          <div class="grow"><b>一切正常</b><span class="muted">近期没有失败 / 回退</span></div></div>
        <div class="info-box">
          近 7 天跑批 <b>{{ wk.total }} 次</b>，成功率 <b>{{ wk.rate === null ? '—' : wk.rate + '%' }}</b><template v-if="wk.diff !== null">，
          比上周<b>{{ wk.diff >= 0 ? '提高' : '降低' }} {{ Math.abs(wk.diff) }}%</b></template>。
          <template v-if="wk.b">质量回退 <b>{{ wk.b }} 次</b>，去「数据质量」看原因。</template>
          <template v-else>推送<b>{{ d.push_enabled ? '已开启' : '已关闭' }}</b>，启用任务 <b>{{ d.tasks.enabled }}/{{ d.tasks.total }}</b>。</template>
        </div>
        <router-link to="/tasks/new" class="cta">新建任务<span><Icon name="arrowR" :size="14" :sw="2.6"/></span></router-link>
      </section>
    </div>

    <div v-if="d" class="card">
      <div class="card-head"><h3>最近执行</h3><router-link to="/runs" class="btn light sm">全部记录</router-link></div>
      <STable :columns="cols" :data="d.recent" clickable empty="还没有执行记录" @row-click="r => $router.push('/runs/'+r.id)">
        <template #title="{ row }"><span class="s-name"><Avatar square :icon="row.kind === 'push' ? 'send' : row.kind === 'notify' ? 'bell' : 'layers'"
          :color="row.kind === 'push' ? 'cyan' : row.kind === 'notify' ? 'amber' : 'indigo'"/>
          <span class="s-ell" :title="(row.task_name || '临时执行') + ' · ' + row.title"><b>{{ row.task_name || '临时执行' }}</b>
            <small>#{{ row.id }} · {{ row.title }}</small></span></span></template>
        <template #trigger="{ row }"><Avatar :text="TRIGGER[row.trigger]" :color="row.trigger==='schedule' ? 'violet' : 'light-blue'"/> {{ TRIGGER[row.trigger] }}</template>
        <template #status="{ row }"><StatusTag :status="row.status"/></template>
        <template #created_at="{ row }">{{ row.created_at.slice(0,16) }}</template>
        <template #dur="{ row }">{{ duration(row.started_at, row.finished_at) || '-' }}</template>
        <template #op="{ row }"><button class="s-btn-icon" title="查看"><Icon name="more" :size="18" :sw="2.6"/></button></template>
      </STable>
    </div>
  </div>`,
}
