import { ref, reactive, onMounted, onUnmounted, computed, watch } from 'vue'
import { api, todayYmd, notify, CHANNELS, confirmBox, STATUS, TABLES, TABLE_LABEL } from '../api.js'
import { Ring } from '../charts.js'

// 表的简写 → 中文：'' = 全部表
const ALL_TABLES = TABLES.map(t => t.key)
export const tablesText = v => { const k = String(v || '').split(',').filter(Boolean)
  return !k.length || k.length === ALL_TABLES.length ? '全部表' : k.map(x => TABLE_LABEL[x] || x).join('、') }

// 检测结果 / 方式的文字，列表和详情共用
export const CHECK_STATUS = {
  pass: { text: '通过', color: 'green', icon: 'tickCircle' },
  fixed: { text: '重算后通过', color: 'violet', icon: 'refresh' },
  blocked: { text: '已回退', color: 'orange', icon: 'alert' },
  fail: { text: '不通过', color: 'pink', icon: 'clear' },
}
const TRIGGER_TEXT = { schedule: '定时跑批', manual: '手动执行', repair: '数据修复', page: '页面检测' }
export const howText = c => c.type === 'run' ? `跑批检测 · ${TRIGGER_TEXT[c.trigger] || c.trigger}` : '页面检测（读库）'
const fmtYmd = d => d ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}` : ''
export const rangeText = c => !c.start ? '—' : c.start === c.end ? fmtYmd(c.start) : `${fmtYmd(c.start)} ~ ${fmtYmd(c.end)}`

export default {
  components: { Ring },
  setup() {
    const days = ref(14), data = ref({ total: 0, counts: {}, items: [] }), err = ref(''), loading = ref(true), kw = ref('')
    const fstatus = ref(''), ftype = ref(''), more = ref(false)
    const scenics = ref([]), today = todayYmd()
    // 选了景区就显示它关联的任务（写哪些表、推哪些表）：修复时按这个任务的表重算、推送
    const linked = ref(undefined)
    const chk = reactive({ scenic: '', start: todayYmd(-30), end: todayYmd(), running: false, result: null,
                           push: true, repair: null })   // repair：{ id, status, start(ms) } 提交的修复执行
    // 检测记录：每一次检测（跑批自动检测、页面检测）一条；先拿 30 条，滚到底 / 点「加载更多」再拿
    const qs = offset => new URLSearchParams({ days: days.value, status: fstatus.value, type: ftype.value,
                                                q: kw.value.trim(), offset, limit: 30 })
    const load = async () => {
      loading.value = true
      try { data.value = await api('/api/quality/checks?' + qs(0)); err.value = '' } catch (e) { err.value = e.message }
      finally { loading.value = false }
    }
    const loadMore = async () => {
      if (more.value || loading.value || data.value.items.length >= data.value.total) return
      more.value = true
      try { const r = await api('/api/quality/checks?' + qs(data.value.items.length))
        data.value = { ...r, items: data.value.items.concat(r.items) } }
      catch (e) { notify(e.message, 'err') } finally { more.value = false }
    }
    onMounted(async () => {
      load()
      try { scenics.value = await api('/api/metrics/scenics')
        if (scenics.value.length && !chk.scenic) chk.scenic = scenics.value[0].scenic_id } catch (e) { /* 库没配 */ }
    })
    // q：不传就按表单上的景区 / 日期；修复跑完自动复检时传修复的那段
    const check = async (q) => {
      q = q && q.scenic ? q : { scenic: chk.scenic, start: chk.start, end: chk.end }
      chk.running = true; chk.result = null; chk.repair = null; stopWatch()
      try { chk.result = await api('/api/quality/check', { method: 'POST', body: q })
        chk.push = chk.result.push_ready; load() }          // 这次检测也记进了检测记录
      catch (e) { notify(e.message, 'err') } finally { chk.running = false }
      return chk.result
    }
    // 提交修复后自己盯着执行状态：每 3 秒查一次，跑完（成功）自动重新检测
    let watchTimer = null, tickTimer = null
    const now = ref(Date.now())
    const stopWatch = () => { clearTimeout(watchTimer); clearInterval(tickTimer); watchTimer = tickTimer = null }
    onUnmounted(stopWatch)
    const watchRepair = (id, q) => {
      chk.repair = { id, status: 'queued', start: Date.now() }
      tickTimer = setInterval(() => { now.value = Date.now() }, 1000)
      const poll = async () => {
        let run = null
        try { run = await api('/api/runs/' + id) } catch (e) { /* 服务暂时连不上：下一轮再查 */ }
        if (!chk.repair || chk.repair.id !== id) return            // 期间点了重新检测 / 换了别的
        if (run) chk.repair.status = run.status
        if (!run || ['queued', 'running'].includes(run.status)) { watchTimer = setTimeout(poll, 3000); return }
        stopWatch()
        if (run.status !== 'success') {
          notify(`修复 #${id} ${(STATUS[run.status] || {}).text || run.status}，没有自动重新检测，点进去看日志`, 'err')
          return
        }
        notify(`修复 #${id} 已完成，正在自动重新检测…`)
        const r = await check(q)
        if (r) notify(r.days && r.days.length ? `重新检测：还有 ${r.days.length} 天有问题` : '重新检测：全部通过 ✓',
                      r.days && r.days.length ? 'err' : 'ok')
      }
      watchTimer = setTimeout(poll, 1500)
    }
    const repairText = computed(() => {
      const r = chk.repair
      if (!r) return ''
      const sec = Math.max(0, Math.round((now.value - r.start) / 1000))
      const t = sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}m${String(sec % 60).padStart(2, '0')}s`
      return r.status === 'queued' ? `修复 #${r.id} 排队中 · ${t}`
        : r.status === 'running' ? `修复 #${r.id} 执行中 · ${t}`
        : `修复 #${r.id} ${(STATUS[r.status] || {}).text || r.status}`
    })
    // 修复 = 把检测的整段一次重算：只重算有问题的那几天，跟相邻没重算的日子用的是不同时刻的源数据
    // （评论会晚到），边上又会冒出新的不一致，要修好几次
    const repair = async () => {
      const r = chk.result
      const fd = d => `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}`
      if (!await confirmBox({ title: '修复数据', okText: '开始修复',
        message: '按当前源数据把检测的这一整段重新计算一遍，覆盖库里的旧数据。近 7 日、本周等周期指标依赖前面每一天，只重算有问题的那几天会在边上产生新的不一致，所以整段一次重算。',
        items: [['景区', r.scenic], ['重算区间', `${fd(r.start)} ~ ${fd(r.end)}（${spanDays.value} 天）`],
                ['有问题', `${r.days.length} 天，共 ${r.days.reduce((a, d) => a + d.count, 0)} 处`],
                ['关联任务', r.task ? r.task.name + (r.task.enabled ? '' : '（已停用）') : '没有包含这个景区的任务'],
                ['重算', (r.task ? tablesText(repairTables.value) : '全部表') + ' · 写库'],
                ['推送', repairPush.value ? tablesText(repairPush.value) : '否' + (chk.push ? '（任务里没勾推送）' : '')]] })) return
      try {
        const x = await api('/api/quality/repair', { method: 'POST',
          body: { scenic: r.scenic, start: r.start, end: r.end, bad_days: r.days.length, push: chk.push } })
        watchRepair(x.run_id, { scenic: r.scenic, start: r.start, end: r.end })
        notify(`已提交修复 #${x.run_id}，跑完会自动重新检测`)
      } catch (e) { notify(e.message, 'err') }
    }
    // 修复按关联任务的表：计算任务里勾的表（大盘表一定算），推送任务里勾的表；没有关联任务 = 全部表、推下钻 + 大盘
    const repairTables = computed(() => {
      const t = chk.result && chk.result.task
      if (!t || !t.tables) return ''
      const k = t.tables.split(',')
      return ALL_TABLES.filter(x => k.includes(x) || x === 'macro').join(',')
    })
    const repairPush = computed(() => {
      const r = chk.result
      if (!r || !chk.push || !r.push_ready) return ''
      return r.task ? r.task.push : 'drill,macro'
    })
    watch(() => chk.scenic, async sc => {
      linked.value = undefined
      if (!sc) return
      try { const r = await api('/api/quality/task?scenic=' + encodeURIComponent(sc)); if (chk.scenic === sc) linked.value = r.task }
      catch (e) { linked.value = undefined }
    }, { immediate: true })
    const spanDays = computed(() => {
      const r = chk.result
      if (!r) return 0
      const t = d => new Date(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6)).getTime()
      return Math.round((t(r.end) - t(r.start)) / 864e5) + 1
    })
    const wk = d => '周' + '日一二三四五六'[new Date(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6)).getDay()]
    const fmtD = d => `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}`
    const stats = computed(() => {
      const c = data.value.counts || {}
      return { pass: c.pass || 0, fixed: c.fixed || 0, blocked: c.blocked || 0, fail: c.fail || 0 }
    })
    const parts = computed(() => [
      { value: stats.value.pass, label: '通过', colors: ['#8fe0d3', '#21b8a6'], icon: 'tickCircle' },
      { value: stats.value.fixed, label: '重算后通过', colors: ['#a194ec', '#5446b8'], icon: 'refresh' },
      { value: stats.value.blocked, label: '已回退', colors: ['#ffd88a', '#f5a524'], icon: 'alert' },
      { value: stats.value.fail, label: '不通过', colors: ['#f6a4b8', '#ee5a7c'], icon: 'clear' }])
    const cols = [
      { key: 'at', title: '检测时间', width: 120 }, { key: 'how', title: '方式 · 触发', width: 210 },
      { key: 'scenic', title: '景区', width: 150 }, { key: 'range', title: '检测日期', width: 200 },
      { key: 'result', title: '结果', width: 120 }, { key: 'summary', title: '检查了 / 结果' }]
    const dayCols = [
      { key: 'date', title: '日期', width: 170 }, { key: 'count', title: '问题', width: 190 },
      { key: 'channels', title: '渠道', width: 110 }, { key: 'samples', title: '明细' }]
    return { days, data, err, loading, kw, load, loadMore, more, fstatus, ftype, scenics, chk, check, repair, repairText, repairPush, tablesText, linked,
             spanDays, wk, fmtD, today, stats, parts, cols, dayCols, CHANNELS, CHECK_STATUS, howText, rangeText }
  },
  template: `
  <div>
    <div class="card sect open">
        <div class="sect-head"><h3>数据检测<Tip wide text="只读检测库里大盘表已有的数据：区间相同的行指标一致（td@T-1 = latest_1d@T …）、各周期评论数 = 每天 td 之和。检测出来哪天不对，点「修复」按当前源数据把检测的整段一次重算（周期指标依赖前面每一天，只重算有问题的几天修不干净）"/></h3></div>
        <div class="sect-body stick">
          <div class="frow">
            <ScenicPicker v-model="chk.scenic" single placeholder="选择景区" width="300px" />
            <DateRange v-model:start="chk.start" v-model:end="chk.end" :clearable="false" :max="today" />
            <button class="btn primary" :disabled="chk.running || !chk.scenic" @click="check"><Icon name="shield" :size="14"/>{{ chk.running ? '检测中…' : '开始检测' }}</button>
          </div>
          <div v-if="chk.scenic && linked !== undefined" class="linked-task" :class="{ none: !linked }">
            <template v-if="linked"><Icon name="list" :size="14" :sw="2.2"/><span>关联任务</span><b>{{ linked.name }}</b>
              <Tag v-if="!linked.enabled" color="grey">已停用</Tag><Tag v-if="linked.all_scenics" color="grey">全部景区的任务</Tag>
              <em>写库 {{ tablesText(linked.tables) }}</em><em>推送 {{ linked.push ? tablesText(linked.push) : '不推送' }}</em>
              <a class="link small" :href="'#/tasks/' + linked.id">查看任务</a></template>
            <template v-else><Icon name="list" :size="14" :sw="2.2"/><span>没有任务包含这个景区：修复时全部表重算，推下钻表 + 大盘表</span></template>
          </div>
        </div>
        <div v-if="chk.result" class="check-result">
          <div class="result-bar" :class="chk.result.days.length ? 'bad' : 'ok'">
            <Icon :name="chk.result.days.length ? 'alert' : 'tickCircle'" :size="18" :sw="2.2"/>
            <b>{{ chk.result.message }}</b>
            <a v-if="chk.result.check_id" class="link small" :href="'#/quality/' + chk.result.check_id">查看检测过程 #{{ chk.result.check_id }}</a><span class="muted small">{{ chk.result.scenic }} · {{ fmtD(chk.result.start) }} ~ {{ fmtD(chk.result.end) }} · {{ chk.result.rows }} 行</span>
            <span v-if="chk.result.task" class="rb-task" :title="'修复时按这个任务的表重算、推送'"><Icon name="list" :size="13" :sw="2.2"/>
              {{ chk.result.task.name }} · 推送 {{ chk.result.task.push ? tablesText(chk.result.task.push) : '不推送' }}</span>
            <span v-else class="rb-task none" title="没有任务包含这个景区：修复时全部表重算，推下钻表 + 大盘表">未关联任务</span>
            <span class="grow"></span>
            <template v-if="chk.result.days.length">
              <label class="check sm"><Switch v-model="chk.push" size="sm" :disabled="!chk.result.push_ready || (chk.result.task && !chk.result.task.push)"/>修复后推送{{ repairPush ? '（' + tablesText(repairPush) + '）' : '' }}
                <Tip v-if="!chk.result.push_ready" text="推送总开关关着或没配推送地址，修复只重算写库、不推送（推送页可以配置）"/></label>
              <a v-if="chk.repair" class="rep-run" :class="chk.repair.status" :href="'#/runs/' + chk.repair.id"
                title="点开看执行日志；跑完会自动重新检测">
                <Icon v-if="['queued', 'running'].includes(chk.repair.status)" name="refresh" :size="14" :sw="2.2" class="spin"/>
                <Icon v-else name="clear" :size="14" :sw="2.2"/>{{ repairText }}</a>
              <button v-if="!chk.repair || !['queued', 'running'].includes(chk.repair.status)" class="btn primary sm" @click="repair" :title="'把检测的整段 ' + spanDays + ' 天一次重算'"><Icon name="tool" :size="14"/>修复（整段重算 {{ spanDays }} 天）</button>
            </template>
            <button class="btn light sm" @click="check"><Icon name="refresh" :size="14"/>重新检测</button>
          </div>
          <STable v-if="chk.result.days.length" :columns="dayCols" :data="chk.result.days" row-key="date">
            <template #date="{ row }"><span class="s-name"><Avatar square icon="calendar" color="red"/>
              <span class="s-ell"><b>{{ fmtD(row.date) }}</b><small>{{ wk(row.date) }}</small></span></span></template>
            <template #count="{ row }"><div class="chips"><Tag color="pink" icon="clear">{{ row.count }} 处</Tag>
              <Tag v-if="row.same_range" color="orange">区间不一致 {{ row.same_range }}</Tag>
              <Tag v-if="row.period_sum" color="violet">周期合计 {{ row.period_sum }}</Tag></div></template>
            <template #channels="{ row }"><div class="chips"><Tag v-for="c in row.channels" :key="c" color="grey">{{ CHANNELS[c] || c }}</Tag></div></template>
            <template #samples="{ row }"><ul class="samples"><li v-for="(x,i) in row.samples" :key="i" :title="x">{{ x }}</li></ul></template>
          </STable>
          <ul v-else class="checklist"><li v-for="c in chk.result.checks" :key="c">{{ c }} ✓</li></ul>
        </div>
      </div>

    <div class="grid one-two">
      <div class="card center">
        <div class="card-head"><h3>近 {{ days }} 天检测<Tip text="每一次检测都算：跑批时每个景区自动检测一次，页面上手动检测一次"/></h3></div>
        <Ring :parts="parts" :size="200" :thickness="20" :value="String(data.total)" label="检测" sub="次" :hole="0.5" />
        <div class="stat4">
          <div v-for="p in parts" :key="p.label"><span class="cap">{{ p.label }}</span><b>{{ p.value }}</b>
            <i class="ubar"><em :style="{width: (data.total ? Math.max(6, p.value / data.total * 100) : 0) + '%', background: p.colors[1]}"></em></i></div>
        </div>
      </div>
      <div class="card">
        <div class="card-head stick"><h3>检测记录<Tip wide text="每一次数据质量检测都在这里：什么时候、怎么触发的（定时跑批 / 手动执行 / 数据修复 / 页面检测）、检测了哪个景区哪几天、结果。点一行看检测过程：查了哪些项、每项的规则、核对了多少、哪里不对、对比的数值"/></h3>
          <div class="toolbar">
            <div class="seg"><button v-for="d in [7,14,30]" :key="d" :class="{on: days===d}" @click="days=d; load()">近 {{ d }} 天</button></div>
            <select v-model="ftype" @change="load"><option value="">全部方式</option><option value="run">跑批自动检测</option><option value="manual">页面检测</option></select>
            <select v-model="fstatus" @change="load"><option value="">全部结果</option>
              <option v-for="(v,k) in CHECK_STATUS" :key="k" :value="k">{{ v.text }}</option></select>
            <span class="search"><Icon name="search" :size="15"/><input v-model="kw" placeholder="景区 / 任务 / 结果" @keyup.enter="load"/></span>
            <button class="btn light" @click="kw=''; fstatus=''; ftype=''; load()">重置</button></div></div>
        <div v-if="err" class="alert red">{{ err }}</div>
        <STable :columns="cols" :data="data.items" :loading="loading" :min-height="260" clickable
          :empty="'近 ' + days + ' 天没有检测记录'" @row-click="r => $router.push('/quality/' + r.id)">
          <template #at="{ row }"><span class="s-ell"><b>{{ row.created_at.slice(5, 16) }}</b><small>#{{ row.id }}</small></span></template>
          <template #how="{ row }"><span class="s-name"><Avatar square :icon="row.type === 'run' ? 'layers' : 'shield'" :color="row.type === 'run' ? 'indigo' : 'cyan'"/>
            <span class="s-ell"><b>{{ howText(row) }}</b><small :title="row.task_name">{{ row.type === 'run' ? (row.task_name || '临时执行') + ' · #' + row.run_id : (row.operator || '—') }}</small></span></span></template>
          <template #scenic="{ row }"><span class="s-ell" :title="row.scenic">{{ row.scenic }}</span></template>
          <template #range="{ row }"><span class="nowrap">{{ rangeText(row) }}</span></template>
          <template #result="{ row }"><Tag :color="CHECK_STATUS[row.status].color" :icon="CHECK_STATUS[row.status].icon">{{ CHECK_STATUS[row.status].text }}</Tag></template>
          <template #summary="{ row }"><span class="s-ell"><b :class="{'t-red': row.status === 'fail' || row.status === 'blocked'}" :title="row.summary">{{ row.summary }}</b>
            <small>检查 {{ row.items }} 项<template v-if="row.attempts > 1"> · 检测 {{ row.attempts }} 次</template><template v-if="row.issues"> · {{ row.issues }} 处问题</template></small></span></template>
          <template #foot><div v-if="data.items.length < data.total" class="s-table-foot">
            已显示 {{ data.items.length }} / 共 {{ data.total }} 条 <a class="link" @click.stop="loadMore">{{ more ? '加载中…' : '加载更多' }}</a></div></template>
        </STable>
      </div>
    </div>
  </div>`,
}

// ══════════════════════════════════════════════════════════════════
// 检测详情：什么时候、怎么触发的、检测了什么数据；每次检测的每一项：查了什么、核对多少、哪里不对、对比的数值
// ══════════════════════════════════════════════════════════════════
export const QualityDetail = {
  props: { id: [String, Number] },
  setup(p) {
    const c = ref(null), err = ref(''), open = reactive({})
    onMounted(async () => {
      try {
        c.value = await api('/api/quality/checks/' + p.id)
        const att = c.value.detail?.attempts || []
        att.forEach((a, i) => (a.steps || []).forEach(s => {            // 默认展开：最后一次检测里不通过的项
          if (i === att.length - 1 && !s.ok) open[`${a.attempt}.${s.key}`] = true
        }))
      } catch (e) { err.value = e.message }
    })
    const toggle = k => { open[k] = !open[k] }
    const fmtNum = n => (n ?? 0).toLocaleString('zh-CN')
    const KEY_TEXT = { same_range: '区间不一致', period_sum: '周期合计不对' }
    return { c, err, open, toggle, fmtNum, CHECK_STATUS, howText, rangeText, KEY_TEXT, CHANNELS, tablesText }
  },
  template: `
  <div>
    <div class="detail-head card">
      <a class="dh-back" href="#/quality" title="返回数据质量" aria-label="返回数据质量"><Icon name="arrowL" :size="18" :sw="2.2"/></a>
      <div class="dh-title">
        <div class="dh-crumb"><a href="#/quality">数据质量</a><span>/</span>检测记录<span>/</span>详情</div>
        <h2>检测 #{{ id }}<Tag v-if="c" :color="CHECK_STATUS[c.status].color" :icon="CHECK_STATUS[c.status].icon">{{ CHECK_STATUS[c.status].text }}</Tag></h2>
        <span v-if="c" class="muted ell1" :title="c.summary">{{ c.summary }}</span>
      </div>
    </div>
    <div v-if="err" class="alert red">{{ err }}</div>
    <template v-if="c">
      <div class="card run-sum">
        <div class="rs-grid">
          <div><span>检测时间</span><b>{{ c.created_at }}</b></div>
          <div><span>方式 · 触发</span><b>{{ howText(c) }}</b></div>
          <div><span>景区</span><b>{{ c.scenic || '-' }}</b></div>
          <div><span>检测日期</span><b>{{ rangeText(c) }}</b></div>
          <div><span>检测次数 · 项数</span><b>{{ c.attempts }} 次 · {{ c.items }} 项</b></div>
          <div v-if="c.type === 'run'"><span>关联执行</span><b><a :href="'#/runs/' + c.run_id">#{{ c.run_id }}</a> <small class="muted">{{ c.task_name || '临时执行' }}</small></b></div>
          <div v-else><span>操作人</span><b>{{ c.operator || '-' }}</b></div>
        </div>
        <div class="qd-source">
          <template v-if="c.type === 'run'">检测的是这次跑批<b>刚算出来、还没写库</b>的结果；不通过会重新取数重算，还不通过就回退（不写库、不推送）。
            <span v-if="c.detail.run_title" class="muted">（{{ c.detail.run_title }}）</span></template>
          <template v-else>检测的是 {{ c.detail.source }}。
            <template v-if="c.detail.task">关联任务 <b>{{ c.detail.task.name }}</b>：修复时重算 {{ tablesText(c.detail.task.tables) }}，推送 {{ c.detail.task.push ? tablesText(c.detail.task.push) : '不推送' }}。</template></template>
          <span v-if="c.detail.exception" class="t-red">异常：{{ c.detail.exception }}</span>
        </div>
      </div>

      <div v-if="!(c.detail.attempts || []).length" class="card"><Empty text="这次没有走到检测（见上面的说明）"/></div>
      <div v-for="a in c.detail.attempts" :key="a.attempt" class="card qd-attempt">
        <div class="card-head"><h3>第 {{ a.attempt }} 次检测<small>{{ a.at }} · {{ (a.steps || []).length }} 项</small></h3>
          <Tag :color="a.ok ? 'green' : 'pink'" :icon="a.ok ? 'tickCircle' : 'clear'">{{ a.ok ? '全部通过' : (a.errors || []).length + ' 项不通过' }}</Tag></div>
        <div v-if="!a.ok && a.attempt < c.detail.attempts.length" class="alert orange small">这次不通过，结果作废，重新取数重算后做了第 {{ a.attempt + 1 }} 次检测</div>
        <div class="qd-steps">
          <div v-for="s in a.steps" :key="s.key" class="qd-step" :class="{bad: !s.ok, on: open[a.attempt + '.' + s.key]}">
            <button class="qd-row" @click="toggle(a.attempt + '.' + s.key)">
              <span class="qd-ico"><Icon :name="s.ok ? 'tickCircle' : 'clear'" :size="18" :sw="2.2"/></span>
              <b>{{ s.title }}</b>
              <span class="qd-stats">核对 {{ fmtNum(s.rows) }} 行 · 断言 {{ s.asserts }} 次<template v-if="s.compared != null"> · 比对 {{ fmtNum(s.compared) }} {{ s.key === 'period_sum' ? '格' : '对' }}</template> · {{ s.ms }} ms</span>
              <span class="grow"></span>
              <Tag v-if="!s.ok" color="pink">{{ s.issue_total ? s.issue_total + ' 处' : (s.errors || []).length + ' 项' }}</Tag>
              <span class="chev" :class="{on: open[a.attempt + '.' + s.key]}"><Icon name="down" :size="16"/></span>
            </button>
            <div v-if="open[a.attempt + '.' + s.key]" class="qd-body">
              <div class="qd-sec"><span class="ng-lab">查了什么</span><ul><li v-for="(r,i) in s.rules" :key="i">{{ r }}</li></ul></div>
              <div v-if="(s.errors || []).length" class="qd-sec"><span class="ng-lab bad">哪里不对</span>
                <ul class="errs"><li v-for="(e,i) in s.errors" :key="i">{{ e }}</li></ul></div>
              <div v-if="(s.issues || []).length" class="qd-sec"><span class="ng-lab bad">对比的数值</span>
                <div class="s-table-wrap"><table class="mini-table qd-issues">
                  <thead><tr><th>日期</th><th>渠道</th><th>字段</th><th>对比的行</th><th>数值</th></tr></thead>
                  <tbody><tr v-for="(it, i) in s.issues" :key="i">
                    <td>{{ it.date }}</td><td>{{ CHANNELS[it.channel] || it.channel }}</td><td>{{ it.field }}</td>
                    <td><template v-if="s.key === 'period_sum'">{{ (it.rows || [])[0] }} 的评论数 vs 区间内每天 td 合计</template>
                      <template v-else>{{ (it.rows || []).join(' vs ') }}</template></td>
                    <td><b class="t-red">{{ (it.values || []).join(' vs ') }}</b></td></tr></tbody></table></div>
                <div v-if="s.issue_total > s.issues.length" class="muted small">共 {{ s.issue_total }} 处，这里只列前 {{ s.issues.length }} 处</div></div>
              <div v-if="s.ok" class="muted small">这一项全部通过。</div>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>`,
}
