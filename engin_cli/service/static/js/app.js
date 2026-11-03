// 入口：外框 + 图标侧栏 + 顶栏 + 极简 hash 路由（#/tasks、#/runs/12 …）。新增页面：在 ROUTES 里加一行即可。
import { createApp, reactive, computed, h, ref, onMounted, onUnmounted } from 'vue'
import { toast, sched, refreshSched, search, dialog } from './api.js'
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
]

const route = reactive({ path: '/' })
const sync = () => { route.path = (location.hash.replace(/^#/, '') || '/').split('?')[0] }
window.addEventListener('hashchange', () => {
  sync(); window.scrollTo(0, 0)
  const m = document.querySelector('.main'); if (m) m.scrollTop = 0      // 内容区自己滚动，换页回到顶部
})
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
    document.title = '标签计算中心'                      // 浏览器标签页标题固定
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
    const kw = ref('')
    // 弹窗键盘：Esc 取消、Enter 确认
    const onKey = e => {
      if (!dialog.cur) return
      if (e.key === 'Escape') { e.preventDefault(); dialog.cur.done(false) }
      // 刚弹出来的 300ms 内不认 Enter：按回车点按钮打开弹窗时，同一下回车别顺手把它确认了
      else if (e.key === 'Enter' && !e.repeat && Date.now() - dialog.cur.at > 300) { e.preventDefault(); dialog.cur.done(true) }
    }
    window.addEventListener('keydown', onKey)
    const doSearch = () => { search.q = kw.value.trim(); search.n++; kw.value = ''; location.hash = '/tasks' }
    return { current, route, NAV, toast, pill, kw, doSearch, dialog }
  },
  template: `
  <div class="frame">
    <header class="topbar">
      <a href="#/" class="brand" title="舆情指标计算服务"><svg viewBox="0 0 40 40" width="38" height="38" aria-hidden="true">
        <g fill="#5a4bc4"><rect v-for="i in 12" :key="i" x="18" y="2" width="4" height="11" rx="2" :transform="'rotate(' + i*30 + ' 20 20)'"/></g></svg></a>
      <div class="hello"><h1>你好，运维！</h1></div>
      <div class="tb-right">
        <label class="tb-search"><input v-model="kw" placeholder="搜索任务 / 景区…" @keyup.enter="doSearch"/>
          <button @click="doSearch" aria-label="搜索"><Icon name="search" :size="16" :sw="2.2"/></button></label>
        <a v-if="pill" href="#/tasks" class="status-pill" :class="pill.cls" :title="pill.text"><i></i><span>{{ pill.text }}</span></a>
        <a href="#/quality" class="tb-round" title="数据质量"><Icon name="shield" :size="18"/></a>
        <a href="#/runs" class="tb-round" title="执行记录"><Icon name="bell" :size="18"/></a>
      </div>
    </header>
    <div class="body">
      <aside class="side">
        <nav>
          <a v-for="n in NAV" :key="n.to" :href="'#'+n.to" :class="{on: current.r.nav===n.to}" :data-tip="n.label" :aria-label="n.label">
            <Icon :name="n.icon" :size="19" /></a>
        </nav>
        <div class="side-foot">
          <a href="#/maint" class="side-mini" title="维护" :class="{on: current.r.nav==='/maint'}"><Icon name="gear" :size="18"/></a>
          <div class="side-me" title="运维">运</div>
        </div>
      </aside>
      <main class="main"><component :is="current.r.comp" v-bind="current.props" :key="route.path" /></main>
    </div>
    <div class="toasts"><TransitionGroup name="toast">
      <div v-for="t in toast.items" :key="t.id" class="toast" :class="t.type">
        <span class="toast-ico"><Icon :name="t.type === 'err' ? 'clear' : 'tickCircle'" :size="18" :sw="2.2"/></span>
        <span class="toast-text">{{ t.text }}</span></div></TransitionGroup></div>
    <Transition name="dlg"><div v-if="dialog.cur" class="dlg-mask" @mousedown.self="dialog.cur.done(false)">
      <div class="dlg" :class="dialog.cur.type" role="alertdialog" aria-modal="true">
        <span class="dlg-ico"><Icon :name="{danger: 'del', warn: 'alert'}[dialog.cur.type] || 'tool'" :size="22" :sw="2"/></span>
        <h3>{{ dialog.cur.title }}</h3>
        <p v-if="dialog.cur.message">{{ dialog.cur.message }}</p>
        <dl v-if="dialog.cur.items.length" class="dlg-items"><template v-for="it in dialog.cur.items" :key="it[0]"><dt>{{ it[0] }}</dt><dd>{{ it[1] }}</dd></template></dl>
        <div class="dlg-btns">
          <button class="btn light" @click="dialog.cur.done(false)">{{ dialog.cur.cancelText }}</button>
          <button ref="okBtn" class="btn" :class="dialog.cur.type === 'danger' ? 'danger-solid' : dialog.cur.type === 'warn' ? 'warn-solid' : 'primary'"
            @click="dialog.cur.done(true)">{{ dialog.cur.okText }}</button></div>
      </div></div></Transition>
  </div>`,
}

const app = createApp(App)
installUI(app)
app.component('router-link', RouterLink)
app.config.globalProperties.$router = { push: p => { location.hash = p } }
app.mount('#app')
