// 图表组件（纯 SVG，不依赖图表库），风格对齐参考稿：
//   Ring     分段渐变环（每段圆头、段间留缝、段首带图标气泡、中间白色圆盘）
//   Wave     平滑面积曲线（顶部日期刻度、选中点白色圆钮、虚线均值、渐变填充）
//   PillBars 胶囊堆叠柱（每段圆角、段间留缝、悬停 / 最高柱显示数值气泡）
//   DayLine  今天 24 小时时间轴（定时时刻 + 已执行）
import { computed, ref } from 'vue'
import { Icon } from './ui.js'

let _gid = 0
const uid = p => `${p}${++_gid}`

// ══════════════════════════════════════════════════════════════════
// Ring：parts = [{value, label, colors:[起,止], icon}]
// ══════════════════════════════════════════════════════════════════
export const Ring = {
  components: { Icon },
  props: { parts: Array, size: { type: Number, default: 280 }, thickness: { type: Number, default: 26 },
           gap: { type: Number, default: 16 }, label: String, value: String, sub: String,
           hole: { type: Number, default: 0.42 }, icons: { type: Boolean, default: true } },
  setup(p) {
    const id = uid('ring')
    const geo = computed(() => {
      const S = p.size, cx = S / 2, r = (S - p.thickness) / 2 - 4
      const live = (p.parts || []).filter(x => (x.value || 0) > 0)
      const total = live.reduce((a, x) => a + x.value, 0)
      const capDeg = (p.thickness / 2) / r * 180 / Math.PI
      const gapDeg = live.length > 1 ? p.gap + 2 * capDeg : 0
      const avail = 360 - gapDeg * live.length
      const minDeg = Math.min(14, avail / Math.max(live.length, 1))
      // 每段至少 minDeg，剩下的按比例分
      const extra = avail - minDeg * live.length
      let a = gapDeg / 2 + capDeg * (live.length > 1 ? 0 : 0)
      const pt = deg => {
        const t = (deg - 90) * Math.PI / 180
        return [cx + r * Math.cos(t), cx + r * Math.sin(t)]
      }
      const segs = live.map((x, i) => {
        const span = live.length === 1 ? 359.99 : minDeg + extra * x.value / total
        const a0 = a, a1 = a + span
        a = a1 + gapDeg
        const [x0, y0] = pt(a0), [x1, y1] = pt(a1)
        const large = span > 180 ? 1 : 0
        return { key: i, d: `M${x0.toFixed(2)},${y0.toFixed(2)} A${r},${r} 0 ${large} 1 ${x1.toFixed(2)},${y1.toFixed(2)}`,
                 x0, y0, x1, y1, c0: (x.colors || [x.color, x.color])[0], c1: (x.colors || [x.color, x.color])[1],
                 icon: x.icon, label: x.label, value: x.value, gid: `${id}-${i}` }
      })
      return { S, cx, r, segs, empty: !live.length }
    })
    return { geo }
  },
  template: `
  <div class="ring" :style="{width: size+'px', height: size+'px'}">
    <svg :width="size" :height="size" :viewBox="'0 0 '+size+' '+size">
      <defs><linearGradient v-for="s in geo.segs" :key="s.gid" :id="s.gid" gradientUnits="userSpaceOnUse"
          :x1="s.x0" :y1="s.y0" :x2="s.x1" :y2="s.y1">
        <stop offset="0" :stop-color="s.c0"/><stop offset="1" :stop-color="s.c1"/></linearGradient></defs>
      <circle v-if="geo.empty" :cx="geo.cx" :cy="geo.cx" :r="geo.r" fill="none" stroke="var(--track)" :stroke-width="thickness"/>
      <path v-for="s in geo.segs" :key="s.key" :d="s.d" fill="none" :stroke="'url(#'+s.gid+')'" :stroke-width="thickness"
        stroke-linecap="round"><title>{{ s.label }}：{{ s.value }}</title></path>
    </svg>
    <template v-if="icons"><span v-for="s in geo.segs" :key="'i'+s.key" v-show="s.icon" class="ring-ico"
      :style="{left: s.x0+'px', top: s.y0+'px', width: thickness*0.82+'px', height: thickness*0.82+'px'}">
      <Icon :name="s.icon" :size="thickness*0.5" :sw="2.4"/></span></template>
    <div class="ring-hole" :style="{width: size*hole+'px', height: size*hole+'px'}">
      <small v-if="label">{{ label }}</small><b class="grad-text" :style="{fontSize: size*hole*0.3+'px'}">{{ value }}</b>
      <span v-if="sub">{{ sub }}</span></div>
  </div>`,
}

// ══════════════════════════════════════════════════════════════════
// Wave：points = [{label, value}]；selected 选中下标（默认最后一个）
// ══════════════════════════════════════════════════════════════════
function smoothPath(xs, ys) {
  // 单调三次插值（不会冲出数据范围），转成贝塞尔曲线
  const n = xs.length
  if (n < 2) return `M${xs[0]},${ys[0]}`
  const dx = [], m = [], t = []
  for (let i = 0; i < n - 1; i++) { dx.push(xs[i + 1] - xs[i]); m.push((ys[i + 1] - ys[i]) / dx[i]) }
  t.push(m[0])
  for (let i = 1; i < n - 1; i++) t.push(m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2)
  t.push(m[n - 2])
  for (let i = 0; i < n - 1; i++) {
    if (m[i] === 0) { t[i] = 0; t[i + 1] = 0; continue }
    const a = t[i] / m[i], b = t[i + 1] / m[i], s = a * a + b * b
    if (s > 9) { const k = 3 / Math.sqrt(s); t[i] = k * a * m[i]; t[i + 1] = k * b * m[i] }
  }
  let d = `M${xs[0].toFixed(1)},${ys[0].toFixed(1)}`
  for (let i = 0; i < n - 1; i++) {
    const h = dx[i] / 3
    d += ` C${(xs[i] + h).toFixed(1)},${(ys[i] + t[i] * h).toFixed(1)} ${(xs[i + 1] - h).toFixed(1)},` +
         `${(ys[i + 1] - t[i + 1] * h).toFixed(1)} ${xs[i + 1].toFixed(1)},${ys[i + 1].toFixed(1)}`
  }
  return d
}

export const Wave = {
  props: { points: Array, height: { type: Number, default: 150 }, selected: { type: Number, default: -1 },
           colors: { type: Array, default: () => ['#6e8c78', '#4a6a56'] }, avg: { type: Boolean, default: true },
           fmt: { type: Function, default: v => v }, labels: { type: Boolean, default: true },
           ticks: { type: Number, default: 0 } },
  emits: ['select'],
  setup(p) {
    const id = uid('wave'), W = 640, hover = ref(-1)
    const sel = computed(() => hover.value >= 0 ? hover.value
      : (p.selected >= 0 ? p.selected : (p.points || []).length - 1))
    const geo = computed(() => {
      const vals = (p.points || []).map(x => Number(x.value) || 0)
      if (!vals.length) return null
      const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1
      const top = 14, bot = p.height - (p.ticks ? 22 : 8)
      const n = vals.length
      const pad = 34                              // 两端留白：圆钮 / 数值气泡不被裁掉
      const xs = vals.map((_, i) => n === 1 ? W / 2 : pad + i / (n - 1) * (W - 2 * pad))
      const ys = vals.map(v => hi === lo ? (top + bot) / 2 : top + (1 - (v - lo) / span) * (bot - top - 10) + 6)
      // 曲线两端水平延伸到边缘（参考稿里波浪是铺满卡片的）
      const line = `M0,${ys[0].toFixed(1)} L` + smoothPath(xs, ys).slice(1) + ` L${W},${ys[n - 1].toFixed(1)}`
      const mean = vals.reduce((a, v) => a + v, 0) / n
      const ay = hi === lo ? (top + bot) / 2 : top + (1 - (mean - lo) / span) * (bot - top - 10) + 6
      return { xs, ys, line, area: `${line} L${W},${p.height} L0,${p.height} Z`, ay, n }
    })
    const tickIdx = computed(() => {
      const n = (p.points || []).length
      if (!p.ticks || !n) return []
      const step = Math.max(1, Math.ceil(n / p.ticks))
      // 最后一个刻度总显示；离它太近的普通刻度不显示，免得两个日期挤在一起
      return [...Array(n).keys()].filter(i => i === n - 1 || (i % step === 0 && n - 1 - i >= Math.max(2, step * 0.6)))
    })
    const onMove = e => {
      const g = geo.value
      if (!g) return
      const r = e.currentTarget.getBoundingClientRect()
      const x = (e.clientX - r.left) / r.width * W
      let best = 0
      g.xs.forEach((v, i) => { if (Math.abs(v - x) < Math.abs(g.xs[best] - x)) best = i })
      hover.value = best
    }
    return { id, W, geo, sel, hover, onMove, tickIdx }
  },
  template: `
  <div class="wave">
    <div v-if="labels && points.length" class="wave-labels" :style="{gridTemplateColumns: 'repeat('+points.length+', 1fr)'}">
      <span v-for="(x,i) in points" :key="i" :class="{on: i===sel}" @click="$emit('select', i)">{{ x.label }}</span></div>
    <div class="wave-body" v-if="geo" @mousemove="onMove" @mouseleave="hover=-1">
      <svg :viewBox="'0 0 '+W+' '+height" preserveAspectRatio="none" :style="{height: height+'px'}">
        <defs>
          <linearGradient :id="id+'f'" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0" :stop-color="colors[0]" stop-opacity=".32"/><stop offset="1" :stop-color="colors[1]" stop-opacity=".02"/></linearGradient>
          <linearGradient :id="id+'s'" x1="0" x2="1" y1="0" y2="0">
            <stop offset="0" :stop-color="colors[0]"/><stop offset="1" :stop-color="colors[1]"/></linearGradient>
        </defs>
        <path :d="geo.area" :fill="'url(#'+id+'f)'"/>
        <line v-if="avg" x1="0" :x2="W" :y1="geo.ay" :y2="geo.ay" stroke="#bcbab3" stroke-dasharray="5 6" vector-effect="non-scaling-stroke"/>
        <line :x1="geo.xs[sel]" :x2="geo.xs[sel]" y1="0" :y2="height" stroke="#dcdad3" vector-effect="non-scaling-stroke"/>
        <path :d="geo.line" fill="none" :stroke="'url(#'+id+'s)'" stroke-width="3" vector-effect="non-scaling-stroke"/>
      </svg>
      <span class="wave-knob" :style="{left: (geo.xs[sel]/W*100)+'%', top: geo.ys[sel]+'px'}"></span>
      <span class="wave-tip" :class="{l: geo.xs[sel]/W > .88, f: geo.xs[sel]/W < .12}" :style="{left: (geo.xs[sel]/W*100)+'%', top: geo.ys[sel]+'px'}">{{ fmt(points[sel].value) }}</span>
      <div v-if="ticks" class="wave-ticks"><span v-for="i in tickIdx" :key="i" :style="{left: (geo.xs[i]/W*100)+'%'}">{{ points[i].label }}</span></div>
    </div>
    <div v-else class="chart-empty">暂无数据</div>
  </div>`,
}

// ══════════════════════════════════════════════════════════════════
// PillBars：rows = [{label, values:{k:n}}]，series = [{key,label,color}]（自下而上）
// ══════════════════════════════════════════════════════════════════
export const PillBars = {
  props: { rows: Array, series: Array, height: { type: Number, default: 150 },
           fmt: { type: Function, default: v => v }, barWidth: { type: Number, default: 12 } },
  setup(p) {
    const hover = ref(-1)
    const tot = r => p.series.reduce((a, s) => a + (r.values[s.key] || 0), 0)
    const max = computed(() => Math.max(1, ...p.rows.map(tot)))
    const peak = computed(() => {
      let b = -1, v = 0
      p.rows.forEach((r, i) => { if (tot(r) > v) { v = tot(r); b = i } })
      return b
    })
    const tip = computed(() => hover.value >= 0 ? hover.value : peak.value)
    const every = computed(() => Math.max(1, Math.ceil(p.rows.length / 8)))
    return { hover, tot, max, tip, every }
  },
  template: `
  <div class="pbars">
    <div class="pbars-grid" :style="{height: height+'px'}"><i></i><i></i><i></i><i></i></div>
    <div class="pbars-cols" :style="{height: height+'px'}">
      <div v-for="(r,i) in rows" :key="r.label" class="pbar" @mouseenter="hover=i" @mouseleave="hover=-1">
        <span v-if="i===tip && tot(r)" class="pbar-tip" :style="{bottom: (tot(r)/max*100)+'%'}">{{ fmt(tot(r)) }}</span>
        <div class="pbar-stack" :style="{height: (tot(r)/max*100)+'%', width: barWidth+'px'}">
          <template v-for="s in series" :key="s.key"><i v-if="r.values[s.key]" :style="{flexGrow: r.values[s.key], background: s.color}"
            :title="s.label + ' ' + r.values[s.key]"></i></template>
        </div>
      </div>
    </div>
    <div class="pbars-labels"><span v-for="(r,i) in rows" :key="r.label">{{ (rows.length - 1 - i) % every === 0 ? r.label : '' }}</span></div>
  </div>`,
}

// ══════════════════════════════════════════════════════════════════
// DayLine：今天 0~24 点，slots = ['08:10', …]，runs = [{at:'HH:MM', status}]
// ══════════════════════════════════════════════════════════════════
export const DayLine = {
  props: { slots: Array, runs: Array, now: String },
  setup(p) {
    const pos = t => { const [h, m] = String(t).split(':').map(Number); return (h * 60 + m) / 1440 * 100 }
    return { pos }
  },
  template: `
  <div class="dayline">
    <div class="dayline-track">
      <div class="dayline-past" :style="{width: now ? pos(now)+'%' : 0}"></div>
      <span v-for="r in runs" :key="'r'+r.at+r.status" class="dl-run" :class="r.status" :style="{left: pos(r.at)+'%'}" :title="r.at + ' 执行'"></span>
      <span v-for="(s,i) in slots" :key="'s'+s" class="dl-slot" :class="{up: i % 2}" :style="{left: pos(s)+'%'}" :title="'定时 ' + s"><em>{{ s }}</em></span>
      <span v-if="now" class="dl-now" :style="{left: pos(now)+'%'}"></span>
    </div>
    <div class="dayline-axis"><span>0</span><span>6</span><span>12</span><span>18</span><span>24</span></div>
  </div>`,
}

// 小号环（指标页情感构成等）
export const MiniRing = {
  components: { Ring },
  props: { parts: Array, size: { type: Number, default: 170 }, value: String, sub: String },
  template: `<Ring :parts="parts" :size="size" :thickness="16" :gap="10" :value="value" :sub="sub" :hole="0.56" :icons="false"/>`,
}

// ══════════════════════════════════════════════════════════════════
// Gauge：半圆仪表（参考稿 Earnings 卡），value 0~100
// ══════════════════════════════════════════════════════════════════
export const Gauge = {
  props: { value: { type: Number, default: 0 }, text: String, size: { type: Number, default: 220 },
           color: { type: String, default: '#5a7a65' }, track: { type: String, default: '#e3ebe5' } },
  setup(p) {
    const geo = computed(() => {
      const v = Math.max(0, Math.min(100, Number(p.value) || 0)) / 100
      const cx = 110, cy = 104, r = 78, th = Math.PI * (1 - v)
      const x = cx + r * Math.cos(th), y = cy - r * Math.sin(th)
      const arc = v <= 0 ? '' : `M${cx - r},${cy} A${r},${r} 0 0 1 ${x.toFixed(2)},${y.toFixed(2)}`
      // 指针：从弧外侧往外伸一截
      const n1 = [cx + (r + 4) * Math.cos(th), cy - (r + 4) * Math.sin(th)]
      const n2 = [cx + (r + 26) * Math.cos(th), cy - (r + 26) * Math.sin(th)]
      return { cx, cy, r, arc, n1, n2, track: `M${cx - r},${cy} A${r},${r} 0 0 1 ${cx + r},${cy}` }
    })
    return { geo }
  },
  template: `
  <div class="gauge" :style="{width: size + 'px'}">
    <svg viewBox="0 0 220 120" :width="size" :height="size * 120 / 220">
      <path :d="geo.track" fill="none" :stroke="track" stroke-width="30"/>
      <path v-if="geo.arc" :d="geo.arc" fill="none" :stroke="color" stroke-width="30"/>
      <line :x1="geo.n1[0]" :y1="geo.n1[1]" :x2="geo.n2[0]" :y2="geo.n2[1]" stroke="#2b302d" stroke-width="3" stroke-linecap="round"/>
    </svg>
    <b class="gauge-val">{{ text ?? (Math.round(value) + '%') }}</b>
  </div>`,
}

// 迷你竖条（参考稿 Spent this month）：values 数组，最后一根深色
export const MiniBars = {
  props: { values: Array, height: { type: Number, default: 44 } },
  setup(p) { return { max: computed(() => Math.max(1, ...(p.values || []).map(v => v || 0))) } },
  template: `<div class="minibars" :style="{height: height + 'px'}">
    <i v-for="(v,i) in values" :key="i" :class="{on: i === values.length - 1}" :style="{height: Math.max(8, (v||0)/max*100) + '%'}"
      :title="v"></i></div>`,
}

// 迷你折线（参考稿 New clients / Activity 右边那条线）
export const Spark = {
  props: { values: Array, color: { type: String, default: '#8fa898' }, width: { type: Number, default: 90 },
           height: { type: Number, default: 34 } },
  setup(p) {
    const d = computed(() => {
      const v = (p.values || []).map(x => Number(x) || 0)
      if (v.length < 2) return ''
      const lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1
      const xs = v.map((_, i) => 2 + i / (v.length - 1) * (p.width - 4))
      const ys = v.map(x => p.height - 4 - (x - lo) / span * (p.height - 8))
      return smoothPath(xs, ys)
    })
    return { d }
  },
  template: `<svg class="spark" :width="width" :height="height" :viewBox="'0 0 ' + width + ' ' + height">
    <path v-if="d" :d="d" fill="none" :stroke="color" stroke-width="2.4" stroke-linecap="round"/></svg>`,
}
