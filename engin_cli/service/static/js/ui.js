// 通用 UI 组件：图标、表格（仿 Semi Design Table）、标签、头像、空状态、开关
import { computed } from 'vue'

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
      <ellipse cx="100" cy="132" rx="70" ry="8" fill="#eef0f6"/>
      <rect x="46" y="28" width="92" height="86" rx="10" fill="#f4f5fb" stroke="#dfe2ef" stroke-width="2"/>
      <rect x="58" y="44" width="50" height="7" rx="3.5" fill="#dfe2ef"/>
      <rect x="58" y="59" width="68" height="7" rx="3.5" fill="#e8eaf3"/>
      <rect x="58" y="74" width="40" height="7" rx="3.5" fill="#e8eaf3"/>
      <circle cx="128" cy="88" r="22" fill="#fff" stroke="#7b6cff" stroke-width="5"/>
      <path d="M144 104l16 16" stroke="#7b6cff" stroke-width="7" stroke-linecap="round"/>
      <path d="M121 81l14 14M135 81l-14 14" stroke="#c6c0ff" stroke-width="4" stroke-linecap="round"/>
    </svg>
    <div class="s-empty-desc">{{ text }}</div>
  </div>`,
}

// ── 表格：columns = [{key, title, width, align, ellipsis}]；单元格用同名插槽自定义 ──
export const STable = {
  components: { Empty },
  props: { columns: Array, data: Array, rowKey: { type: String, default: 'id' }, clickable: Boolean,
           empty: { type: String, default: '搜索无结果' }, minHeight: Number, loading: Boolean },
  emits: ['row-click'],
  template: `
  <div class="s-table-wrap" :style="{minHeight: minHeight ? minHeight + 'px' : null}">
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
  for (const [n, c] of Object.entries({ Icon, Tag, StatusTag, Avatar, Empty, STable, Switch })) app.component(n, c)
}
