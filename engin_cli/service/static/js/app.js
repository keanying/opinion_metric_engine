// 入口：外框 + 图标侧栏 + 顶栏 + 极简 hash 路由（#/tasks、#/runs/12 …）。新增页面：在 ROUTES 里加一行即可。
import { createApp, reactive, computed, h, ref, onMounted, onUnmounted } from 'vue'
import { toast, sched, refreshSched } from './api.js'
import { installUI } from './ui.js'
import Overview from './pages/overview.js'
import { TaskList, TaskEdit } from './pages/tasks.js'
import { RunList, RunDetail } from './pages/runs.js'
import Quality from './pages/quality.js'
import Metrics from './pages/metrics.js'
import Push from './pages/push.js'
import Maint from './pages/maint.js'

const ROUTES = [
  { path: /^\/$/, comp: Overview, nav: '/', title: '概览', crumb: '指标计算服务 / 概览' },
  { path: /^\/tasks$/, comp: TaskList, nav: '/tasks', title: '任务', crumb: '任务管理 / 定时任务' },
  { path: /^\/tasks\/new$/, comp: TaskEdit, nav: '/tasks', title: '新建任务', crumb: '任务管理 / 新建任务' },
  { path: /^\/tasks\/(\d+)$/, comp: TaskEdit, nav: '/tasks', title: '编辑任务', crumb: '任务管理 / 编辑任务', props: m => ({ id: m[1] }) },
  { path: /^\/runs$/, comp: RunList, nav: '/runs', title: '执行记录', crumb: '任务管理 / 执行记录' },
  { path: /^\/runs\/(\d+)$/, comp: RunDetail, nav: '/runs', title: '执行详情', crumb: '任务管理 / 执行记录 / 详情', props: m => ({ id: m[1] }) },
  { path: /^\/quality$/, comp: Quality, nav: '/quality', title: '数据质量', crumb: '质量 / 检测记录' },
  { path: /^\/metrics$/, comp: Metrics, nav: '/metrics', title: '指标查看', crumb: 'ADS / 大盘指标' },
  { path: /^\/push$/, comp: Push, nav: '/push', title: '推送', crumb: '下游 / 推送设置与记录' },
  { path: /^\/maint$/, comp: Maint, nav: '/maint', title: '维护', crumb: '系统 / 维护' },
]

const NAV = [
  { to: '/', label: '概览', icon: 'home' },
  { to: '/tasks', label: '任务', icon: 'list' },
  { to: '/runs', label: '执行记录', icon: 'clock' },
  { to: '/quality', label: '数据质量', icon: 'shield' },
  { to: '/metrics', label: '指标查看', icon: 'chart' },
  { to: '/push', label: '推送', icon: 'send' },
  { to: '/maint', label: '维护', icon: 'tool' },
]

const route = reactive({ path: '/' })
const sync = () => { route.path = (location.hash.replace(/^#/, '') || '/').split('?')[0] }
window.addEventListener('hashchange', () => { sync(); window.scrollTo(0, 0) })
sync()

const RouterLink = {
  props: { to: String },
  setup(p, { slots }) { return () => h('a', { href: '#' + p.to }, slots.default && slots.default()) },
}

const App = {
  setup() {
    const current = computed(() => {
      for (const r of ROUTES) {
        const m = route.path.match(r.path)
        if (m) return { r, props: r.props ? r.props(m) : {} }
      }
      return { r: ROUTES[0], props: {} }
    })
    let timer
    onMounted(() => { refreshSched(); timer = setInterval(refreshSched, 15000) })
    onUnmounted(() => clearInterval(timer))
    const pill = computed(() => {
      const d = sched.data
      if (!d) return null
      if (!d.available) return { cls: 'grey', text: '定时未启动（--no-scheduler）' }
      if (!d.enabled) return { cls: 'grey', text: '定时已暂停 · 只能手动执行' }
      const run = d.running ? ` · 正在执行 #${d.running}` : ''
      return { cls: 'green', text: d.next ? `调度运行中 · 下次 ${d.next.at.slice(5)}${run}` : `调度运行中 · 暂无定时${run}` }
    })
    return { current, route, NAV, toast, pill }
  },
  template: `
  <div class="frame">
    <div v-if="current.r.nav==='/'" class="deco-tri"></div>
    <aside class="side">
      <a href="#/" class="logo" title="舆情指标计算服务"><svg viewBox="0 0 32 32" width="30" height="30">
        <defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#8b6cff"/><stop offset="1" stop-color="#4f7cff"/></linearGradient></defs>
        <path d="M16 4a12 12 0 00-12 12v12h7V16a5 5 0 0110 0v12h7V16A12 12 0 0016 4z" fill="url(#lg)"/></svg></a>
      <nav>
        <a v-for="n in NAV" :key="n.to" :href="'#'+n.to" :class="{on: current.r.nav===n.to}" :data-tip="n.label" :aria-label="n.label">
          <Icon :name="n.icon" :size="20" /></a>
      </nav>
      <div class="side-me" title="运维">运</div>
    </aside>
    <main class="main">
      <header class="topbar">
        <div class="tb-title"><h1>{{ current.r.title }}</h1><p>{{ current.r.crumb }}</p></div>
        <div class="tb-right">
          <a href="#/tasks/new" class="btn primary pill"><Icon name="plus" :size="15" :sw="2.4"/>新建任务</a>
          <a v-if="pill" href="#/tasks" class="status-pill" :class="pill.cls"><i></i>{{ pill.text }}</a>
          <a href="#/quality" class="tb-bell" title="数据质量"><Icon name="bell" :size="19"/></a>
          <span class="tb-me">OPS</span>
        </div>
      </header>
      <component :is="current.r.comp" v-bind="current.props" :key="route.path" />
    </main>
    <div class="toasts"><div v-for="t in toast.items" :key="t.id" class="toast" :class="t.type">{{ t.text }}</div></div>
  </div>`,
}

const app = createApp(App)
installUI(app)
app.component('router-link', RouterLink)
app.config.globalProperties.$router = { push: p => { location.hash = p } }
app.mount('#app')
