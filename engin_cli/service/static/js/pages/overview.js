// 概览：版式对齐参考稿（第一行 4 个小卡、第二行曲线 + 半圆仪表 + 下次执行、第三行执行次数 + 需要关注 + 数据检测）
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { api, TRIGGER, KIND, duration } from '../api.js'
import { Wave, PillBars, Gauge, MiniBars, Spark } from '../charts.js'

const pct = (a, b) => (b ? Math.round(a / b * 1000) / 10 : 0)
const sum = (arr, k) => arr.reduce((a, x) => a + (x[k] || 0), 0)

export default {
  components: { Wave, PillBars, Gauge, MiniBars, Spark },
  setup() {
    const d = ref(null), err = ref('')
    let timer
    const load = async () => {
      try { d.value = await api('/api/overview'); err.value = '' } catch (e) { err.value = e.message }
    }
    onMounted(() => { load(); timer = setInterval(load, 10000) })
    onUnmounted(() => clearInterval(timer))

    const wk = computed(() => {
      const days = d.value?.daily || []
      const cur = days.slice(-7), prev = days.slice(0, 7)
      const t = x => sum(x, 'success') + sum(x, 'blocked') + sum(x, 'failed') + sum(x, 'other')
      const s = sum(cur, 'success'), total = t(cur), ps = sum(prev, 'success'), pt = t(prev)
      const rate = total ? pct(s, total) : null, prate = pt ? pct(ps, pt) : null
      return { s, b: sum(cur, 'blocked'), f: sum(cur, 'failed'), total, rate, ps,
               diff: rate !== null && prate !== null ? Math.round((rate - prate) * 10) / 10 : null,
               bad: sum(cur, 'blocked') + sum(cur, 'failed'), pbad: sum(prev, 'blocked') + sum(prev, 'failed') }
    })
    const dayTotal = x => (x.success || 0) + (x.blocked || 0) + (x.failed || 0) + (x.other || 0)
    const last7 = computed(() => (d.value?.daily || []).slice(-7))
    const rateSeries = computed(() => last7.value.map(x => dayTotal(x) ? x.success / dayTotal(x) * 100 : 0))
    // 执行耗时（近 14 天平均）
    const durPts = computed(() => (d.value?.durations || []).map(x => ({ label: x.date.slice(5).replace('-', '/'), value: x.avg })))
    const avgDur = computed(() => {
      const a = (d.value?.durations || []).slice(-7).filter(x => x.n)
      return a.length ? Math.round(a.reduce((s, x) => s + x.avg, 0) / a.length * 10) / 10 : 0
    })
    const durDelta = computed(() => {
      const all = d.value?.durations || []
      const a = all.slice(-7).filter(x => x.n), b = all.slice(0, 7).filter(x => x.n)
      if (!a.length || !b.length) return null
      const ma = a.reduce((s, x) => s + x.avg, 0) / a.length, mb = b.reduce((s, x) => s + x.avg, 0) / b.length
      return mb ? Math.round((ma - mb) / mb * 1000) / 10 : null
    })
    const series = [
      { key: 'other', label: '其他', color: '#bdbbce' },
      { key: 'failed', label: '失败', color: '#ee5a7c' },
      { key: 'blocked', label: '回退', color: '#ffc04d' },
      { key: 'success', label: '成功', color: '#5a4bc4' },
    ]
    const barRows = computed(() => (d.value?.daily || []).map(x => ({ label: x.date.slice(5).replace('-', '/'), values: x })))
    const fmtDur = v => !v ? '0s' : v < 60 ? `${v}s` : `${Math.floor(v / 60)}m${Math.round(v % 60)}s`
    const next = computed(() => d.value?.upcoming?.[0] || null)
    return { d, err, wk, last7, rateSeries, durPts, avgDur, durDelta, series, barRows, fmtDur, next, dayTotal,
             TRIGGER, KIND, duration }
  },
  template: `
  <div>
    <div v-if="err" class="alert red">{{ err }}</div>
    <div v-if="d" class="ov2">
      <!-- 第一行：4 个小卡 -->
      <div class="card mini">
        <div><span class="lbl">今日执行</span><b class="num">{{ d.today }} <em>次</em></b></div>
        <MiniBars :values="last7.map(dayTotal)" />
      </div>
      <div class="card mini">
        <span class="circ green"><Icon name="list" :size="20"/></span>
        <div class="grow"><span class="lbl">启用任务</span><b class="num">{{ d.tasks.enabled }}<em> / {{ d.tasks.total }}</em></b></div>
        <Spark :values="last7.map(x => x.success)" />
      </div>
      <div class="card mini">
        <span class="circ sand"><Icon name="clock" :size="20"/></span>
        <div class="grow"><span class="lbl">下次执行</span><b class="num">{{ next ? next.at.slice(11) : '--:--' }}</b></div>
      </div>
      <div class="card mini hero">
        <div class="grow"><span class="lbl">近 7 天成功率</span><b class="num">{{ wk.rate === null ? '—' : wk.rate + '%' }}</b></div>
        <Spark :values="rateSeries" color="#ffffff" :width="80" :height="40" />
      </div>

      <!-- 第二行 -->
      <div class="card span2 bal">
        <div class="bal-head"><h3>执行耗时</h3>
          <span class="ontrack" :class="{bad: durDelta !== null && durDelta > 20}"><i></i>{{ durDelta !== null && durDelta > 20 ? '变慢了' : '运行平稳' }}</span>
          <span class="grow"></span><span class="muted small">近 14 天 · 每天平均</span></div>
        <div class="bubbles">
          <div class="bubble"><span>成功（近 7 天）</span><b>{{ wk.s }} 次</b>
            <em :class="wk.s >= wk.ps ? 'up' : 'down'">{{ wk.s >= wk.ps ? '+' : '' }}{{ wk.s - wk.ps }}</em></div>
          <div class="bubble"><span>平均耗时</span><b>{{ fmtDur(avgDur) }}</b>
            <em v-if="durDelta !== null" :class="durDelta <= 0 ? 'up' : 'down'">{{ durDelta > 0 ? '+' : '' }}{{ durDelta }}%</em></div>
        </div>
        <Wave :points="durPts" :height="150" :labels="false" :ticks="7" :avg="false" :colors="['#9b8fe6', '#5446b8']" :fmt="fmtDur" />
      </div>

      <div class="card gcard">
        <h3>执行结果</h3>
        <span class="lbl">近 7 天跑批</span>
        <b class="big-green">{{ wk.total }} 次</b>
        <p class="muted small">成功率<template v-if="wk.diff !== null">比上周{{ wk.diff >= 0 ? '高' : '低' }} {{ Math.abs(wk.diff) }}%</template><template v-else>（上周没有数据）</template></p>
        <Gauge :value="wk.rate || 0" :text="wk.rate === null ? '—' : Math.round(wk.rate) + '%'" :size="210" />
      </div>

      <div class="card pcard">
        <span class="p-avatar"><Icon name="clock" :size="34" :sw="1.8"/></span>
        <b class="p-name">{{ next ? next.task_name : (d.scheduler.enabled ? '暂无定时任务' : '定时已暂停') }}</b>
        <span class="muted small">{{ next ? '下次执行 ' + next.at.slice(5) : '在任务页设置定时' }}</span>
        <div class="p-stats">
          <div><span>任务</span><b>{{ d.tasks.total }}</b></div>
          <div><span>今日执行</span><b>{{ d.today }}</b></div>
          <div><span>需关注</span><b :class="{'t-red': d.attention.length}">{{ d.attention.length }}</b></div>
        </div>
      </div>

      <!-- 第三行 -->
      <div class="card span2 wallet">
        <div class="w-text">
          <h3>近 14 天执行次数</h3>
          <p>成功 <b>{{ wk.s }}</b> 次，失败 / 回退 <b>{{ wk.bad }}</b> 次（近 7 天），
            比上周{{ wk.bad <= wk.pbad ? '少' : '多' }} {{ Math.abs(wk.bad - wk.pbad) }} 次。</p>
          <div class="legend"><span v-for="s in series.slice().reverse()" :key="s.key" class="lg-item"><i :style="{background: s.color}"></i>{{ s.label }}</span></div>
          <router-link to="/runs" class="btn primary sm">执行记录 <Icon name="arrowR" :size="13" :sw="2.4"/></router-link>
        </div>
        <div class="w-chart"><PillBars :rows="barRows" :series="series" :height="150" :bar-width="10" :fmt="v => v + ' 次'" /></div>
      </div>

      <div class="card transfers">
        <div class="t-head"><h3>需要关注</h3><router-link to="/runs" class="link small">全部</router-link></div>
        <a v-for="r in d.attention" :key="r.id" class="t-item" :href="'#/runs/' + r.id">
          <i :class="r.status"></i>
          <div class="grow"><b>{{ r.task_name || '临时执行' }}</b><span>{{ r.created_at.slice(5,16) }}</span></div>
          <StatusTag :status="r.status"/></a>
        <div v-if="!d.attention.length" class="t-empty"><Icon name="tickCircle" :size="28" :sw="1.6"/><span>近期没有失败和回退</span></div>
      </div>

      <div class="card safe">
        <span class="safe-ico"><Icon name="shield" :size="48" :sw="1.3"/></span>
        <b>数据质量检测</b>
        <span class="muted small">{{ wk.b ? '近 7 天质量回退 ' + wk.b + ' 次' : '近 7 天没有质量回退' }}</span>
        <router-link to="/quality" class="btn primary sm">去检测</router-link>
      </div>
    </div>

  </div>`,
}
