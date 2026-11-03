// 通用 UI 组件：图标、表格（仿 Semi Design Table）、标签、头像、空状态、开关
import { computed, ref, onUnmounted } from 'vue'

// ── 图标（24×24 线性图标，stroke 绘制）────────────────────────────────
const ICONS = {
  home: 'M3 11l9-8 9 8M5 9.5V20h5v-6h4v6h5V9.5',
  list: 'M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01',
  clock: 'M12 7v5l3 2M21 12a9 9 0 11-18 0 9 9 0 0118 0z',
  shield: 'M9 12l2 2 4-4M12 3l8 3v6c0 4.5-3.4 8.2-8 9-4.6-.8-8-4.5-8-9V6l8-3z',
  chart: 'M4 20V10M10 20V4M16 20v-7M22 20H2',
  send: 'M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z',
  tool: 'M14.7 6.3a4 4 0 00-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 005.4-5.4l-2.5 2.5-2.4-.6-.6-2.4 2.5-2.5z',
  tick: 'M20 6L9 17l-5-5',
  tickCircle: 'M22 12a10 10 0 11-20 0 10 10 0 0120 0zM8 12l3 3 5-6',
  clear: 'M22 12a10 10 0 11-20 0 10 10 0 0120 0zM15 9l-6 6M9 9l6 6',
  comment: 'M21 12a8 8 0 01-11.6 7.1L4 20l1-4.6A8 8 0 1121 12z',
  alert: 'M12 9v4M12 17h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z',
  minus: 'M22 12a10 10 0 11-20 0 10 10 0 0120 0zM8 12h8',
  refresh: 'M21 12a9 9 0 01-15.5 6.2M3 12A9 9 0 0118.5 5.8M18.5 2v4h-4M5.5 22v-4h4',
  play: 'M7 4l13 8-13 8V4z',
  stop: 'M6 6h12v12H6z',
  edit: 'M4 20h4L19 9l-4-4L4 16v4zM14 6l4 4',
  del: 'M4 7h16M10 11v6M14 11v6M5 7l1 13h12l1-13M9 7V4h6v3',
  more: 'M5 12h.01M12 12h.01M19 12h.01',
  plus: 'M12 5v14M5 12h14',
  arrowR: 'M5 12h14M13 6l6 6-6 6',
  arrowL: 'M19 12H5M11 6l-6 6 6 6',
  down: 'M6 9l6 6 6-6',
  star: 'M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9L12 3z',
  db: 'M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3',
  bell: 'M18 8a6 6 0 10-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 01-3.4 0',
  search: 'M21 21l-4.3-4.3M17 10.5a6.5 6.5 0 11-13 0 6.5 6.5 0 0113 0z',
  eye: 'M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM15 12a3 3 0 11-6 0 3 3 0 016 0z',
  bolt: 'M13 2L4 14h7l-1 8 9-12h-7l1-8z',
  layers: 'M12 2l10 5-10 5L2 7l10-5zM2 17l10 5 10-5M2 12l10 5 10-5',
  calendar: 'M4 5h16v16H4zM4 9h16M8 3v4M16 3v4',
  file: 'M14 3H6v18h12V7l-4-4zM14 3v4h4',
  gear: 'M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z',
}

export const Icon = {
  props: { name: String, size: { type: [Number, String], default: 16 }, sw: { type: Number, default: 1.9 }, spin: Boolean },
  setup(p) { return { d: computed(() => ICONS[p.name] || '') } },
  template: `<svg class="ico" :class="{spin}" :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    :stroke-width="sw" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="d"/></svg>`,
}

// ── 标签：shape=circle 的浅色标签，带前缀图标（同 Semi Tag）────────────
export const Tag = {
  components: { Icon },
  props: { color: { type: String, default: 'grey' }, icon: String, spin: Boolean },
  template: `<span class="s-tag" :class="'c-' + color"><Icon v-if="icon" :name="icon" :size="12" :sw="2.2" :spin="spin"/><slot/></span>`,
}

// 执行状态 → 标签（同 Semi 例子里的 tagConfig）
export const STATUS_TAG = {
  queued: { color: 'cyan', icon: 'clock', text: '排队中' },
  running: { color: 'blue', icon: 'refresh', text: '执行中', spin: true },
  success: { color: 'green', icon: 'tickCircle', text: '成功' },
  failed: { color: 'pink', icon: 'clear', text: '失败' },
  blocked: { color: 'orange', icon: 'alert', text: '已回退' },
  cancelled: { color: 'grey', icon: 'minus', text: '已取消' },
}
export const StatusTag = {
  components: { Tag },
  props: { status: String },
  setup(p) { return { c: computed(() => STATUS_TAG[p.status] || { color: 'grey', text: p.status }) } },
  template: `<Tag :color="c.color" :icon="c.icon" :spin="c.spin">{{ c.text }}</Tag>`,
}

// ── 头像：圆形取首字；square = 方形图标位（表格标题列左边那个）──────────
const AV_COLORS = ['indigo', 'light-blue', 'green', 'red', 'amber', 'violet', 'cyan', 'grey']
export function avatarColor(text) {
  let h = 0
  for (const ch of String(text || '')) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  return AV_COLORS[h % AV_COLORS.length]
}
export const Avatar = {
  components: { Icon },
  props: { text: String, color: String, square: Boolean, icon: String, size: { type: Number, default: 24 } },
  setup(p) {
    return { col: computed(() => p.color || avatarColor(p.text)),
             ch: computed(() => (p.text || '?').trim().slice(0, 1).toUpperCase()) }
  },
  template: `<span class="s-avatar" :class="['c-' + col, {square}]" :style="{width: size+'px', height: size+'px', fontSize: (size*0.5)+'px'}">
    <Icon v-if="icon" :name="icon" :size="size*0.58" :sw="2.1"/><template v-else>{{ ch }}</template></span>`,
}

// ── 空状态（仿 Semi IllustrationNoResult）────────────────────────────
export const Empty = {
  props: { text: { type: String, default: '搜索无结果' }, small: Boolean },
  template: `
  <div class="s-empty" :class="{small}">
    <svg viewBox="0 0 200 150" class="s-empty-img" aria-hidden="true">
      <ellipse cx="100" cy="132" rx="70" ry="8" fill="#e7e5f3"/>
      <rect x="46" y="28" width="92" height="86" rx="10" fill="#eeedf6" stroke="#cfcbe6" stroke-width="2"/>
      <rect x="58" y="44" width="50" height="7" rx="3.5" fill="#cfcbe6"/>
      <rect x="58" y="59" width="68" height="7" rx="3.5" fill="#dbd8ec"/>
      <rect x="58" y="74" width="40" height="7" rx="3.5" fill="#dbd8ec"/>
      <circle cx="128" cy="88" r="22" fill="#fff" stroke="#8475de" stroke-width="5"/>
      <path d="M144 104l16 16" stroke="#8475de" stroke-width="7" stroke-linecap="round"/>
      <path d="M121 81l14 14M135 81l-14 14" stroke="#cfc8f7" stroke-width="4" stroke-linecap="round"/>
    </svg>
    <div class="s-empty-desc">{{ text }}</div>
  </div>`,
}

// ── 表格：columns = [{key, title, width, align, ellipsis}]；单元格用同名插槽自定义 ──
export const STable = {
  components: { Empty },
  // scroll：表格自己滚动（外层给定高度，表头固定）；滚到底发 reach-end（「滚动自动翻页」用）
  props: { columns: Array, data: Array, rowKey: { type: String, default: 'id' }, clickable: Boolean,
           empty: { type: String, default: '搜索无结果' }, minHeight: Number, loading: Boolean, scroll: Boolean },
  emits: ['row-click', 'reach-end'],
  methods: {
    onScroll(e) {
      const el = e.target
      if (el.scrollTop + el.clientHeight >= el.scrollHeight - 80) this.$emit('reach-end')
    },
  },
  template: `
  <div class="s-table-wrap" :class="{scroll}" :style="{minHeight: minHeight ? minHeight + 'px' : null}"
    @scroll.passive="scroll && onScroll($event)">
    <table class="s-table">
      <colgroup><col v-for="c in columns" :key="c.key" :style="{width: c.width ? c.width + 'px' : null}"/></colgroup>
      <thead><tr><th v-for="c in columns" :key="c.key" :class="c.align">{{ c.title }}</th></tr></thead>
      <tbody>
        <tr v-for="(r, i) in data" :key="r[rowKey] ?? i" :class="{click: clickable}" @click="$emit('row-click', r)">
          <td v-for="c in columns" :key="c.key" :class="[c.align, {ell: c.ellipsis}]">
            <slot :name="c.key" :row="r" :index="i"><span :title="c.ellipsis ? String(r[c.key] ?? '') : null">{{ r[c.key] ?? '' }}</span></slot></td>
        </tr>
        <tr v-if="!data || !data.length" class="empty-row"><td :colspan="columns.length"><Empty :text="loading ? '加载中…' : empty"/></td></tr>
      </tbody>
    </table>
    <slot name="foot"/>
  </div>`,
}

// ── 说明图标：灰色「!」，鼠标移上去 / 键盘聚焦时浮出说明（挂到 body，不会被卡片裁掉）──────
// 整页缩放（app.css 里桌面按 80% 显示）：getBoundingClientRect / innerWidth 是屏幕像素，
// 弹层的 position: fixed 坐标要用缩放前的 CSS 像素，这里统一换算
export const box = el => {
  const de = document.documentElement, z = parseFloat(getComputedStyle(de).zoom) || 1
  // 新版浏览器（标准 zoom）矩形是缩放后的屏幕像素，要除；老版 Chrome 已经是缩放前的，不除
  const rz = Math.abs(de.getBoundingClientRect().width - window.innerWidth) < 2 ? z : 1
  const r = el.getBoundingClientRect()
  return { left: r.left / rz, top: r.top / rz, bottom: r.bottom / rz, width: r.width / rz,
           vw: window.innerWidth / z, vh: window.innerHeight / z }
}

export const Tip = {
  props: { text: String, wide: Boolean },
  setup(p) {
    const el = ref(null), pos = ref(null)
    const show = () => {
      const r = box(el.value), w = p.wide ? 420 : 300
      const left = Math.min(Math.max(r.left + r.width / 2, w / 2 + 12), r.vw - w / 2 - 12)
      pos.value = r.top < 120 ? { left, top: r.bottom + 8, below: true } : { left, top: r.top - 8, below: false }
    }
    const hide = () => { pos.value = null }
    return { el, pos, show, hide }
  },
  template: `<span ref="el" class="tip" tabindex="0" role="note" :aria-label="text"
      @mouseenter="show" @mouseleave="hide" @focus="show" @blur="hide">
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><circle cx="8" cy="8" r="8" fill="currentColor"/>
      <path d="M8 4.3v4.9" stroke="#fff" stroke-width="1.9" stroke-linecap="round"/><circle cx="8" cy="11.6" r="1.05" fill="#fff"/></svg>
    <Teleport to="body"><div v-if="pos" class="tip-pop" :class="{below: pos.below, wide}" :style="{left: pos.left+'px', top: pos.top+'px'}">{{ text }}</div></Teleport>
  </span>`,
}

// ── 日期选择：点开日历选一天，值是 yyyyMMdd 字符串（和后端一致）──────────────────────
const ymd = d => `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`
const parseYmd = s => /^\d{8}$/.test(s || '') ? new Date(+s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6, 8)) : null
export const DatePicker = {
  components: { Icon },
  props: { modelValue: String, placeholder: { type: String, default: '选择日期' }, min: String, max: String,
           clearable: { type: Boolean, default: true }, width: { type: Number, default: 150 } },
  emits: ['update:modelValue', 'change'],
  setup(p, { emit }) {
    const el = ref(null), pos = ref(null), view = ref(new Date())
    const text = computed(() => p.modelValue && /^\d{8}$/.test(p.modelValue)
      ? `${p.modelValue.slice(0, 4)}-${p.modelValue.slice(4, 6)}-${p.modelValue.slice(6)}` : '')
    const close = () => { pos.value = null; window.removeEventListener('mousedown', outside, true); window.removeEventListener('scroll', close, true) }
    const outside = e => { if (!e.target.closest('.dp-pop') && !el.value.contains(e.target)) close() }
    const open = () => {
      if (pos.value) return close()
      const r = box(el.value)
      const below = r.vh - r.bottom > 330
      pos.value = { left: Math.min(r.left, r.vw - 300), top: below ? r.bottom + 6 : r.top - 326 }
      const d = parseYmd(p.modelValue) || parseYmd(p.max) || new Date()
      view.value = new Date(d.getFullYear(), d.getMonth(), 1)
      window.addEventListener('mousedown', outside, true)
      window.addEventListener('scroll', close, true)
    }
    onUnmounted(close)
    const cells = computed(() => {
      const y = view.value.getFullYear(), m = view.value.getMonth()
      const first = new Date(y, m, 1), lead = (first.getDay() + 6) % 7       // 周一开头
      const out = []
      for (let i = 0; i < 42; i++) {
        const d = new Date(y, m, 1 - lead + i), v = ymd(d)
        out.push({ v, day: d.getDate(), other: d.getMonth() !== m, today: v === ymd(new Date()),
                   on: v === p.modelValue, off: (p.min && v < p.min) || (p.max && v > p.max) })
      }
      return out
    })
    const move = n => { view.value = new Date(view.value.getFullYear(), view.value.getMonth() + n, 1) }
    const pick = c => { if (c.off) return; emit('update:modelValue', c.v); emit('change', c.v); close() }
    const clear = () => { emit('update:modelValue', ''); emit('change', ''); close() }
    const today = () => pick({ v: ymd(new Date()), off: (p.min && ymd(new Date()) < p.min) || (p.max && ymd(new Date()) > p.max) })
    const title = computed(() => `${view.value.getFullYear()} 年 ${view.value.getMonth() + 1} 月`)
    return { el, pos, text, open, cells, move, pick, clear, today, title }
  },
  template: `
  <span ref="el" class="dp" :class="{on: !!pos}" :style="{width: width + 'px'}" tabindex="0" @click="open" @keydown.enter.prevent="open">
    <span :class="text ? 'dp-val' : 'dp-ph'">{{ text || placeholder }}</span>
    <a v-if="clearable && text" class="dp-x" @click.stop="clear">×</a><Icon v-else name="calendar" :size="15"/>
    <Teleport to="body"><div v-if="pos" class="dp-pop" :style="{left: pos.left + 'px', top: pos.top + 'px'}" @click.stop>
      <div class="dp-head"><button @click="move(-12)">«</button><button @click="move(-1)">‹</button>
        <b>{{ title }}</b><button @click="move(1)">›</button><button @click="move(12)">»</button></div>
      <div class="dp-week"><span v-for="w in ['一','二','三','四','五','六','日']" :key="w">{{ w }}</span></div>
      <div class="dp-days"><button v-for="c in cells" :key="c.v" :class="{other: c.other, today: c.today, on: c.on, off: c.off}"
        :disabled="c.off" @click="pick(c)">{{ c.day }}</button></div>
      <div class="dp-foot"><a @click="today">今天</a><a v-if="clearable" @click="clear">清空</a></div>
    </div></Teleport>
  </span>`,
}

// 起止日期：两个日历，结束不能早于开始
export const DateRange = {
  components: { DatePicker },
  props: { start: String, end: String, placeholder: { type: Array, default: () => ['开始日期', '结束日期'] },
           max: String, clearable: { type: Boolean, default: true } },
  emits: ['update:start', 'update:end', 'change'],
  template: `<span class="dr">
    <DatePicker :model-value="start" :placeholder="placeholder[0]" :max="end || max" :clearable="clearable"
      @update:model-value="v => { $emit('update:start', v); $emit('change') }"/>
    <span class="dr-sep">至</span>
    <DatePicker :model-value="end" :placeholder="placeholder[1]" :min="start" :max="max" :clearable="clearable"
      @update:model-value="v => { $emit('update:end', v); $emit('change') }"/></span>`,
}

// ── 景区选择：可搜索（编号 / 名称），多选或单选；值是逗号分隔的景区编号 ─────────────────────
let _scenicCache = null
export function loadScenics(force = false) {
  if (!_scenicCache || force) {
    _scenicCache = fetch('/api/scenics', { headers: { 'X-Requested-With': 'omx' } })
      .then(r => r.ok ? r.json() : { items: [] }).then(d => d.items || []).catch(() => [])
  }
  return _scenicCache
}
export const ScenicPicker = {
  components: { Icon },
  props: { modelValue: String, single: Boolean, placeholder: { type: String, default: '全部景区' },
           width: { type: String, default: '100%' } },
  emits: ['update:modelValue', 'change'],
  setup(p, { emit }) {
    const el = ref(null), inp = ref(null), pos = ref(null), kw = ref(''), opts = ref([])
    loadScenics().then(x => { opts.value = x })
    const picked = computed(() => String(p.modelValue || '').split(',').map(x => x.trim()).filter(Boolean))
    const nameOf = id => (opts.value.find(o => o.scenic_id === id) || {}).scenic_name || ''
    const shown = computed(() => {
      const k = kw.value.trim().toLowerCase()
      return opts.value.filter(o =>
        (!k || o.scenic_id.toLowerCase().includes(k) || (o.scenic_name || '').toLowerCase().includes(k))).slice(0, 200)
    })
    const set = ids => { const v = ids.join(','); emit('update:modelValue', v); emit('change', v) }
    const close = () => { pos.value = null; kw.value = ''; window.removeEventListener('mousedown', outside, true); window.removeEventListener('scroll', onScroll, true) }
    const outside = e => { if (!e.target.closest('.sp-pop') && !el.value.contains(e.target)) close() }
    const onScroll = e => { if (!e.target.closest || !e.target.closest('.sp-pop')) close() }
    const open = () => {
      if (pos.value) return
      const r = box(el.value), below = r.vh - r.bottom > 300
      pos.value = { left: r.left, width: Math.max(r.width, 300), top: below ? r.bottom + 6 : null, bottom: below ? null : r.vh - r.top + 6 }
      window.addEventListener('mousedown', outside, true); window.addEventListener('scroll', onScroll, true)
      setTimeout(() => inp.value && inp.value.focus())
    }
    onUnmounted(close)
    const toggle = id => {
      if (p.single) { set([id]); close(); return }
      set(picked.value.includes(id) ? picked.value.filter(x => x !== id) : [...picked.value, id])
    }
    const remove = id => set(picked.value.filter(x => x !== id))
    const enter = () => {                       // 列表里没有的编号也能直接回车加上
      const k = kw.value.trim()
      if (!k) return
      const hit = shown.value.find(o => o.scenic_id === k) || (shown.value.length === 1 ? shown.value[0] : null)
      toggle(hit ? hit.scenic_id : k); kw.value = ''
    }
    const backspace = () => { if (!kw.value && picked.value.length && !p.single) remove(picked.value[picked.value.length - 1]) }
    return { el, inp, pos, kw, picked, shown, nameOf, open, close, toggle, remove, enter, backspace, set }
  },
  template: `
  <div ref="el" class="sp" :class="{on: !!pos, single}" :style="{width, flex: width === '100%' ? null : 'none'}" @click="open">
    <span v-for="id in picked" :key="id" class="sp-chip" :title="(nameOf(id) ? nameOf(id) + ' ' : '') + id"><span class="sp-name">{{ nameOf(id) || id }}</span><small v-if="nameOf(id)">{{ id }}</small>
      <a v-if="!single" @click.stop="remove(id)">×</a></span>
    <span v-if="!picked.length" class="sp-ph">{{ placeholder }}</span>
    <span class="grow"></span>
    <a v-if="picked.length" class="sp-x" title="清空" @click.stop="set([])">×</a>
    <Icon v-else name="down" :size="15"/>
    <Teleport to="body"><div v-if="pos" class="sp-pop" @click.stop
        :style="{left: pos.left + 'px', width: pos.width + 'px', top: pos.top != null ? pos.top + 'px' : null, bottom: pos.bottom != null ? pos.bottom + 'px' : null}">
      <div class="sp-search"><Icon name="search" :size="15"/><input ref="inp" v-model="kw" placeholder="搜索景区名称 / 编号，回车添加"
        @keydown.enter.prevent="enter" @keydown.backspace="backspace" @keydown.esc="close"/></div>
      <div class="sp-list">
        <button v-for="o in shown" :key="o.scenic_id" :class="{on: picked.includes(o.scenic_id)}" @click="toggle(o.scenic_id)">
          <i class="sp-check"><Icon v-if="picked.includes(o.scenic_id)" name="tick" :size="12" :sw="3"/></i>
          <span class="grow"><b>{{ o.scenic_name || o.scenic_id }}</b><small>{{ o.scenic_id }}</small></span></button>
        <div v-if="!shown.length" class="sp-empty">{{ kw ? '没有匹配的景区，回车直接按编号添加' : '没有景区' }}</div>
      </div>
      <div v-if="!single" class="sp-foot"><span>已选 {{ picked.length }} 个</span><a @click="set([])">清空 = {{ placeholder }}</a><a @click="close">完成</a></div>
    </div></Teleport>
  </div>`,
}

// ── 开关 ──────────────────────────────────────────────────────────────
export const Switch = {
  props: { modelValue: Boolean, disabled: Boolean, size: String },
  emits: ['update:modelValue', 'change'],
  template: `<button type="button" class="switch" :class="[{on: modelValue, disabled}, size]" :disabled="disabled"
    @click.stop="$emit('update:modelValue', !modelValue); $emit('change', !modelValue)"><i></i></button>`,
}

// 全局注册（app.js 调用）
export function installUI(app) {
  for (const [n, c] of Object.entries({ Icon, Tag, StatusTag, Avatar, Empty, STable, Switch, Tip, DatePicker, DateRange, ScenicPicker })) app.component(n, c)
}
