// 入口：外框 + 顶栏（含横向菜单）+ 极简 hash 路由（#/tasks、#/runs/12 …）。新增页面：在 ROUTES 里加一行即可。
import { createApp, reactive, computed, h, ref, watch, onMounted, onUnmounted } from 'vue'
import { toast, sched, refreshSched, search, dialog, health, checkHealth, auth, loadMe, login, logout, confirmBox } from './api.js'
import { installUI } from './ui.js'
import Overview from './pages/overview.js'
import { TaskList, TaskEdit } from './pages/tasks.js'
import { RunList, RunDetail } from './pages/runs.js'
import Quality, { QualityDetail } from './pages/quality.js'
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
  { path: /^\/quality\/(\d+)$/, comp: QualityDetail, nav: '/quality', title: '检测详情', crumb: '质量 / 检测记录 / 详情', props: m => ({ id: m[1] }) },
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

// 登录页：账号是系统用户表 src_opinion_sys_user 里的
const LoginPage = {
  setup() {
    const f = reactive({ username: '', password: '', busy: false, err: '' })
    const submit = async () => {
      if (!f.username.trim() || !f.password) { f.err = '请输入用户名和密码'; return }
      f.busy = true; f.err = ''
      try { await login(f.username.trim(), f.password); f.password = '' }
      catch (e) { f.err = e.message; f.password = '' }
      finally { f.busy = false }
    }
    return { f, submit }
  },
  template: `
  <div class="login-wrap"><form class="login" @submit.prevent="submit">
    <img src="/logo.png" width="60" height="60" alt="" />
    <h1>标签计算中心</h1>
    <p class="muted">请用系统账号登录</p>
    <label><span>用户名</span><input v-model="f.username" autocomplete="username" autofocus /></label>
    <label><span>密码</span><input v-model="f.password" type="password" autocomplete="current-password" /></label>
    <div v-if="f.err" class="login-err"><Icon name="alert" :size="15" :sw="2.2"/>{{ f.err }}</div>
    <button class="btn primary" :disabled="f.busy">{{ f.busy ? '登录中…' : '登录' }}</button>
  </form></div>`,
}

const App = {
  components: { LoginPage },
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
    // 系统状态：正常 10 秒测一次，连不上时 5 秒一次
    let htimer
    const loopHealth = async () => {
      await checkHealth()
      if (auth.user) htimer = setTimeout(loopHealth, health.state === 'down' ? 5000 : 10000)
    }
    const hp = ref(false), me = ref(false)       // 系统状态面板 / 账号卡片开没开
    // 鼠标移上去展开、移开稍等一下再收（留时间把鼠标移进面板里点「重新检测」「退出登录」）；点一下也能开关（触屏）
    const hoverOf = flag => { let t
      return { enter: () => { clearTimeout(t); flag.value = true }, leave: () => { clearTimeout(t); t = setTimeout(() => { flag.value = false }, 220) } } }
    const hpHover = hoverOf(hp), meHover = hoverOf(me)
    const closeHp = e => {
      if (hp.value && !e.target.closest('.hp-wrap')) hp.value = false
      if (me.value && !e.target.closest('.me-wrap')) me.value = false
    }
    // 登录后才开始轮询调度状态 / 系统状态，退出登录就停
    const startLoops = () => { if (timer) return; refreshSched(); timer = setInterval(refreshSched, 15000); loopHealth() }
    const stopLoops = () => { clearInterval(timer); clearTimeout(htimer); timer = htimer = null }
    watch(() => !!auth.user, on => (on ? startLoops() : stopLoops()), { immediate: true })
    onMounted(() => { loadMe(); window.addEventListener('mousedown', closeHp) })
    onUnmounted(() => { stopLoops(); window.removeEventListener('mousedown', closeHp) })
    const doLogout = async () => { me.value = false; await logout() }      // 账号卡片里点「退出登录」，意图明确，不再二次确认
    const recheck = () => checkHealth(true).then(() => setTimeout(checkHealth, 1500))
    const checks = computed(() => health.state === 'down'
      ? [{ key: 'service', label: '服务', ok: false, detail: `没有响应（已停止或网络中断），${health.since} 起` }]
      : (health.data?.checks || []))
    // 顶栏齿轮：正常绿色、有问题红色（服务连不上 / 某个库连不上），正在执行时慢慢转
    const pill = computed(() => {
      if (health.state === 'down') return { cls: 'red', text: '服务连不上 · 可能已停止' }
      const bad = (health.data?.checks || []).filter(c => c.ok === false)
      if (bad.length) return { cls: 'red', text: bad.map(c => c.label).join('、') + '连不上' }
      const d = sched.data
      if (!d) return { cls: 'grey', text: '检测中…' }
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
    return { current, route, NAV, toast, pill, kw, doSearch, dialog, health, hp, me, hpHover, meHover, checks, recheck, auth, doLogout, sched }
  },
  template: `
  <div v-if="auth.user === undefined" class="boot">加载中…</div>
  <LoginPage v-else-if="!auth.user" />
  <div v-else class="frame">
    <header class="topbar">
      <div class="tb-left">
      <a href="#/" class="brand" title="标签计算中心"><img src="/logo.png" width="40" height="40" alt="标签计算中心" /></a>
      <div class="hello"><h1>你好，{{ auth.user.display_name }}！<span v-if="auth.user.readonly" class="ro-badge" title="只读账号：只能查看，不能执行、修改">只读</span></h1></div>
      </div>
      <!-- 菜单：横向紫色胶囊，在顶栏中间；当前页显示名称，其他的鼠标移上去显示 -->
      <nav class="topnav" aria-label="菜单">
        <a v-for="n in NAV" :key="n.to" :href="'#'+n.to" :class="{on: current.r.nav===n.to}" :data-tip="n.label" :aria-label="n.label">
          <Icon :name="n.icon" :size="18" /><span>{{ n.label }}</span></a>
        <i class="tn-sep"></i>
        <a href="#/maint" :class="{on: current.r.nav==='/maint'}" data-tip="维护" aria-label="维护"><Icon name="gear" :size="18"/><span>维护</span></a>
      </nav>
      <div class="tb-right">
        <label class="tb-search"><input v-model="kw" placeholder="搜索任务 / 景区…" @keyup.enter="doSearch"/>
          <button @click="doSearch" aria-label="搜索"><Icon name="search" :size="16" :sw="2.2"/></button></label>
        <div class="hp-wrap" @mouseenter="hpHover.enter" @mouseleave="hpHover.leave">
          <button class="tb-gear" :class="[pill.cls === 'red' ? 'red' : 'green', { busy: sched.data && sched.data.running }]"
            :aria-label="'系统状态：' + pill.text" @click="hp = !hp"><Icon name="gear" :size="20" :sw="2"/><i></i></button>
          <Transition name="pop"><div v-if="hp" class="hp-pop">
            <div class="hp-head"><b>系统状态</b>
              <span class="hp-state" :class="health.state">{{ {ok: '全部正常', warn: '部分不可用', down: '服务连不上', checking: '检测中'}[health.state] }}</span></div>
            <div class="hp-now" :class="pill.cls"><i></i>{{ pill.text }}</div>
            <p v-if="health.state === 'down'" class="hp-tip">页面连不上服务：服务可能已经停止，或网络中断。每 5 秒自动重试，恢复后页面会自动刷新。
              服务器上重新启动：<code>python -m engin_cli.cli serve</code></p>
            <ul class="hp-list"><li v-for="c in checks" :key="c.key">
              <i :class="c.ok === true ? 'ok' : c.ok === false ? 'bad' : 'warn'"></i>
              <b>{{ c.label }}</b><span>{{ c.detail }}<small v-if="c.sub">{{ c.sub }}</small></span></li></ul>
            <div class="hp-foot"><span>{{ health.state === 'down' ? '上次正常 ' + (health.lastOk || '—') : '检测于 ' + (health.lastOk || '—') }}</span>
              <a @click="recheck">重新检测</a></div>
          </div></Transition>
        </div>
        <div class="me-wrap" @mouseenter="meHover.enter" @mouseleave="meHover.leave">
          <button class="side-me" :aria-label="auth.user.display_name" @click="me = !me">{{ auth.user.display_name.slice(0, 1) }}</button>
          <Transition name="pop"><div v-if="me" class="me-pop">
            <div class="me-head"><span class="side-me big">{{ auth.user.display_name.slice(0, 1) }}</span>
              <div><b>{{ auth.user.display_name }}</b><small>{{ auth.user.username }}</small></div>
              <span class="hp-state" :class="auth.user.readonly ? '' : 'ok'">{{ auth.user.role_text }}</span></div>
            <dl class="me-kv"><dt>登录时间</dt><dd>{{ auth.user.login_at || '—' }}</dd>
              <dt>权限</dt><dd>{{ auth.user.readonly ? '只能查看' : auth.user.role === 'admin' ? '全部（含系统设置）' : '执行、编辑任务（系统设置除外）' }}</dd></dl>
            <button class="btn light danger me-out" @click="doLogout"><Icon name="logout" :size="15" :sw="2.2"/>退出登录</button>
          </div></Transition>
        </div>
      </div>
    </header>
    <div class="body">
      <main class="main"><component :is="current.r.comp" v-bind="current.props" :key="route.path + '#' + health.epoch" /></main>
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
