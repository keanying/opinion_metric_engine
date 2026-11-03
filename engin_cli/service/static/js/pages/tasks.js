import { ref, reactive, onMounted, onUnmounted, computed, watch } from 'vue'
import { api, TABLES, TABLE_LABEL, notify, sched, refreshSched } from '../api.js'

const DATE_MODES = [['today', '当天'], ['yesterday', '昨天'], ['recent', '近 N 天'], ['range', '固定区间']]
const KEYS = TABLES.map(t => t.key)

export function blankConfig() {
  return { scenics: '', tables: '', date_mode: 'today', recent_days: 3, start_date: '', end_date: '',
           rerun_days: '', push: 'drill,macro', skip_db: false, drill_keyword_filter: false,
           quality_gate: true, quality_retries: '', backfill_lookback: '', verbose: true }
}
const split = s => String(s || '').split(',').filter(Boolean)
const modeText = c => ({ today: '当天', yesterday: '昨天', recent: `近 ${c.recent_days} 天`,
  range: `${c.start_date}~${c.end_date}` })[c.date_mode] || ''

// ── 调度日志（深色面板）──────────────────────────────────────────────
export const EventLog = {
  props: { events: Array, title: { type: String, default: '调度日志' }, sub: { type: String, default: '定时触发了谁、跳过了谁、为什么' } },
  template: `
  <div class="card dark-card">
    <div class="card-head"><h3>{{ title }}<Tip :text="sub"/></h3><slot/></div>
    <div class="evlog">
      <div v-for="e in events" :key="e.id" class="ev" :class="e.level">
        <span class="ev-ts">{{ e.ts.slice(5,16) }}</span>
        <span v-if="e.task_name" class="ev-task">{{ e.task_name }}</span>
        <span class="ev-text">{{ e.text }}</span></div>
      <div v-if="!events || !events.length" class="ev muted">暂无</div>
    </div>
  </div>`,
}

// ── 任务参数（日期 / 计算哪些表 / 推送 / 选项），任务编辑和维护页「临时执行」共用 ──
export const TaskBody = {
  props: { cfg: Object, pushOff: Boolean },
  setup(p) {
    const calc = computed(() => new Set(p.cfg.tables ? split(p.cfg.tables) : KEYS))
    const push = computed(() => new Set(split(p.cfg.push)))
    const setCalc = (k, on) => {
      const s = new Set(calc.value)
      on ? s.add(k) : s.delete(k)
      if (!s.size) return notify('至少算一张表', 'err')
      p.cfg.tables = s.size === KEYS.length ? '' : KEYS.filter(x => s.has(x)).join(',')
      p.cfg.push = split(p.cfg.push).filter(x => s.has(x)).join(',')
    }
    const setPush = (k, on) => {
      const s = new Set(push.value)
      on ? s.add(k) : s.delete(k)
      p.cfg.push = KEYS.filter(x => s.has(x) && calc.value.has(x)).join(',')
    }
    const preset = keys => { p.cfg.tables = keys.length === KEYS.length ? '' : keys.join(',');
      p.cfg.push = split(p.cfg.push).filter(x => keys.includes(x)).join(',') }
    return { calc, push, setCalc, setPush, preset, TABLES, DATE_MODES, KEYS }
  },
  template: `
  <div class="tb">
    <div class="card sect">
      <div class="sect-head"><h3>{{ $slots.schedule ? '定时与日期' : '日期范围' }}<Tip text="目标日期按服务器日期算；「当天」= 执行那天"/></h3></div>
      <div class="sect-body">
        <slot name="schedule"/>
        <div class="frow"><label>目标日期</label>
          <div class="seg"><button v-for="m in DATE_MODES" :key="m[0]" :class="{on: cfg.date_mode===m[0]}"
            @click="cfg.date_mode=m[0]">{{ m[1] }}</button></div></div>
        <div class="frow" v-if="cfg.date_mode==='recent'"><label>近几天</label>
          <input type="number" min="1" v-model.number="cfg.recent_days" class="w100" /><span class="hint">天，含当天</span></div>
        <div class="frow" v-if="cfg.date_mode==='range'"><label>区间</label>
          <DateRange v-model:start="cfg.start_date" v-model:end="cfg.end_date" :clearable="false" /></div>
        <div class="frow"><label>景区</label>
          <input v-model="cfg.scenics" class="grow" placeholder="景区编号，逗号分隔；不填 = 有评论的全部景区" /></div>
      </div>
    </div>

    <div class="card sect">
      <div class="sect-head"><h3>计算哪些表<Tip text="没选的下钻表 / 大盘表不计算；基础四表照算（质量检测要用），但不写库、不推送"/></h3>
        <div class="btn-group"><button class="btn xs" @click="preset(KEYS)">全选</button>
          <button class="btn xs" @click="preset(['drill','macro'])">只要下钻 + 大盘</button></div></div>
      <div class="tiles">
        <div v-for="t in TABLES" :key="t.key" class="tile" :class="{on: calc.has(t.key)}" @click="setCalc(t.key, !calc.has(t.key))">
          <Avatar square :icon="t.icon" :color="t.color" :size="34"/>
          <div class="grow"><b>{{ t.label }}<Tip :text="t.desc + '（' + t.table + '）'"/></b></div>
          <Switch :model-value="calc.has(t.key)" size="sm" @change="v => setCalc(t.key, v)"/>
        </div>
      </div>
    </div>

    <div class="card sect">
      <div class="sect-head"><h3>推送<Tip text="算完推给下游；推送地址、批大小、试推在「推送」页设置；都不选 = 不推送"/></h3>
        <a href="#/push" class="btn xs light">推送设置 →</a></div>
      <div v-if="pushOff" class="alert orange">推送总开关已关闭：这个任务现在只算不推（推送页可以打开）</div>
      <div class="push-row">
        <label v-for="t in TABLES" :key="t.key" class="pchk" :class="{off: !calc.has(t.key), on: push.has(t.key)}">
          <Switch :model-value="push.has(t.key)" :disabled="!calc.has(t.key)" size="sm" @change="v => setPush(t.key, v)"/>
          {{ t.label }}</label>
      </div>
    </div>

    <div class="card sect">
      <div class="sect-head"><h3>数据质量与选项</h3></div>
      <div class="opts">
        <div class="opt"><div><b>质量检测不通过就回退</b><Tip text="先重算（次数不填 = 用 settings），还不通过：不写库、不推送，发飞书通知"/></div>
          <div class="opt-r"><span class="hint">重算</span><input v-model="cfg.quality_retries" class="w64" placeholder="默认"/><span class="hint">次</span>
            <Switch v-model="cfg.quality_gate"/></div></div>
        <div class="opt"><div><b>下钻表按采集关键字过滤</b><Tip text="下钻表只收评论包含本景区采集关键字（及词云里的词）的评论"/></div>
          <Switch v-model="cfg.drill_keyword_filter"/></div>
        <div class="opt"><div><b>只算不写库</b><Tip text="不写 ADS 表（只算 + 推送），一般不用"/></div><Switch v-model="cfg.skip_db"/></div>
        <div class="opt"><div><b>往前重跑 / 补数上限</b><Tip text="往前多重跑几天，覆盖延迟产出的数据；不填 = 用 settings 里的"/></div>
          <div class="opt-r"><input v-model="cfg.rerun_days" class="w64" placeholder="默认"/><span class="hint">天</span>
            <input v-model="cfg.backfill_lookback" class="w100" placeholder="补数上限"/></div></div>
        <div class="opt"><div><b>详细日志</b><Tip text="执行日志里打印每一步的明细（-v）"/></div><Switch v-model="cfg.verbose"/></div>
      </div>
    </div>
  </div>`,
}

// ══════════════════════════════════════════════════════════════════
// 任务列表
// ══════════════════════════════════════════════════════════════════
export const TaskList = {
  components: { EventLog },
  setup() {
    const tasks = ref([]), err = ref(''), kw = ref(''), events = ref([]), loading = ref(true)
    const load = async () => {
      try {
        tasks.value = await api('/api/tasks'); err.value = ''
        events.value = (await api('/api/scheduler')).events
      } catch (e) { err.value = e.message } finally { loading.value = false }
      refreshSched()
    }
    let timer
    onMounted(() => { load(); timer = setInterval(load, 15000) })
    onUnmounted(() => clearInterval(timer))
    const shown = computed(() => tasks.value.filter(t => !kw.value ||
      (t.name + (t.config.scenics || '')).toLowerCase().includes(kw.value.toLowerCase())))
    const setMaster = async v => {
      try { await api('/api/scheduler', { method: 'PUT', body: { enabled: v } }); notify(v ? '定时已开启' : '定时已暂停'); load() }
      catch (e) { notify(e.message, 'err') }
    }
    const runNow = async t => {
      try { const r = await api(`/api/tasks/${t.id}/run`, { method: 'POST', body: {} })
        notify(`已提交执行 #${r.run_id}`); location.hash = '/runs/' + r.run_id } catch (e) { notify(e.message, 'err') }
    }
    const toggle = async (t, v) => {
      try { await api(`/api/tasks/${t.id}/enabled`, { method: 'POST', body: { enabled: v } }); load() }
      catch (e) { notify(e.message, 'err') }
    }
    const remove = async t => {
      if (!confirm(`删除任务「${t.name}」？执行记录会保留。`)) return
      try { await api(`/api/tasks/${t.id}`, { method: 'DELETE' }); notify('已删除'); load() }
      catch (e) { notify(e.message, 'err') }
    }
    const cols = [
      { key: 'name', title: '任务', width: 300 }, { key: 'times', title: '定时', width: 150 },
      { key: 'tables', title: '计算 / 推送', width: 230 }, { key: 'last', title: '上次执行', width: 170 },
      { key: 'next', title: '下次执行', width: 140 }, { key: 'enabled', title: '启用', width: 70 },
      { key: 'op', title: '', width: 130, align: 'r' }]
    return { tasks, shown, err, kw, events, loading, cols, sched, setMaster, runNow, toggle, remove, modeText, split, TABLE_LABEL, load }
  },
  template: `
  <div>
    <div class="card banner">
      <Switch :model-value="!!(sched.data && sched.data.enabled)" :disabled="sched.data && !sched.data.available" @change="setMaster"/>
      <div class="grow"><b>启用定时执行</b><Tip wide :text="'到点按任务配置跑批：计算 → 质量检测 → 写入 ADS → 推送下游。同一时间只跑一个，后到的排队；服务停机错过超过 ' + (sched.data ? sched.data.grace_minutes : 30) + ' 分钟的不补跑。关掉后只能手动执行'"/></div>
      <Tag v-if="sched.data" :color="sched.data.enabled ? 'green' : 'grey'" :icon="sched.data.enabled ? 'tickCircle' : 'minus'">
        {{ !sched.data.available ? '未启动' : sched.data.enabled ? '已启用' : '已暂停' }}</Tag>
    </div>
    <div v-if="err" class="alert red">{{ err }}</div>

    <div class="card">
      <div class="card-head"><h3>定时任务<small>{{ tasks.length }} 个</small></h3>
        <div class="toolbar">
          <span class="search"><Icon name="search" :size="15"/><input v-model="kw" placeholder="搜索任务 / 景区" /></span>
          <button class="btn light" @click="kw=''; load()">重置</button>
          <a href="#/tasks/new" class="btn primary"><Icon name="plus" :size="14" :sw="2.4"/>新建任务</a></div></div>
      <STable :columns="cols" :data="shown" :loading="loading" :min-height="260"
        :empty="tasks.length ? '搜索无结果' : '还没有任务，点「新建任务」'">
        <template #name="{ row }"><span class="s-name"><Avatar square :text="row.name" :size="24"/>
          <span class="s-ell"><a :href="'#/tasks/'+row.id" :title="row.name"><b>{{ row.name }}</b></a>
            <small :title="row.config.scenics">{{ row.config.scenics || '全部景区' }} · {{ modeText(row.config) }}</small></span></span></template>
        <template #times="{ row }"><div class="chips">
          <span v-for="x in (row.schedule.times||[])" :key="x" class="chip">{{ x }}</span>
          <span v-if="!(row.schedule.times||[]).length" class="muted">仅手动</span></div></template>
        <template #tables="{ row }"><div class="chips">
          <Tag v-if="!row.config.tables" color="indigo">全部表</Tag>
          <Tag v-for="k in split(row.config.tables)" :key="k" color="indigo">{{ TABLE_LABEL[k] }}</Tag>
          <Tag v-if="row.config.push" color="cyan" icon="send">{{ split(row.config.push).map(k => TABLE_LABEL[k]).join('、') }}</Tag>
          <Tag v-else color="grey">不推送</Tag></div></template>
        <template #last="{ row }"><template v-if="row.last_run"><StatusTag :status="row.last_run.status"/>
          <a class="link small" :href="'#/runs/'+row.last_run.id"> {{ row.last_run.created_at.slice(5,16) }}</a></template>
          <span v-else class="muted">未执行</span></template>
        <template #next="{ row }"><span :class="row.next_fire ? '' : 'muted'">{{ row.next_fire ? row.next_fire.slice(5) : '-' }}</span></template>
        <template #enabled="{ row }"><Switch :model-value="row.enabled" size="sm" @change="v => toggle(row, v)"/></template>
        <template #op="{ row }">
          <button class="s-btn-icon play" title="立即执行" @click="runNow(row)"><Icon name="play" :size="16"/></button>
          <a class="s-btn-icon" title="编辑" :href="'#/tasks/'+row.id"><Icon name="edit" :size="16"/></a>
          <button class="s-btn-icon danger" title="删除" @click="remove(row)"><Icon name="del" :size="16"/></button></template>
      </STable>
    </div>
    <EventLog :events="events" />
  </div>`,
}

// ══════════════════════════════════════════════════════════════════
// 任务编辑（新建 / 编辑）
// ══════════════════════════════════════════════════════════════════
export const TaskEdit = {
  components: { TaskBody, EventLog },
  props: { id: String },
  setup(p) {
    const f = reactive({ name: '', enabled: true, times: ['08:10'], config: blankConfig() })
    const newTime = ref(''), saving = ref(false), preview = ref(null), events = ref([]), loaded = ref(!p.id)
    onMounted(async () => {
      refreshSched()
      if (!p.id) return
      try {
        const t = (await api('/api/tasks')).find(x => String(x.id) === String(p.id))
        if (!t) { notify('任务不存在', 'err'); location.hash = '/tasks'; return }
        f.name = t.name; f.enabled = t.enabled; f.times = [...((t.schedule || {}).times || [])]
        Object.assign(f.config, t.config)
        for (const k of ['rerun_days', 'quality_retries']) if (f.config[k] === null) f.config[k] = ''
        events.value = await api(`/api/tasks/${p.id}/events?limit=40`)
      } catch (e) { notify(e.message, 'err') } finally { loaded.value = true }
    })
    let t0
    watch(() => JSON.stringify([f.config, f.times]), () => {
      clearTimeout(t0)
      t0 = setTimeout(async () => {
        try { preview.value = await api('/api/tasks/preview', { method: 'POST',
          body: { config: f.config, schedule: { times: f.times } } }) } catch (e) { /* 忽略 */ }
      }, 250)
    }, { immediate: true })
    const addTime = () => {
      const t = newTime.value.trim()
      if (!/^([01]?\d|2[0-3]):[0-5]\d$/.test(t)) return notify('时间格式是 HH:MM，例如 08:10', 'err')
      const v = t.padStart(5, '0')
      if (!f.times.includes(v)) f.times.push(v)
      f.times.sort(); newTime.value = ''
    }
    const save = async () => {
      saving.value = true
      try {
        const body = { name: f.name, enabled: f.enabled, config: f.config, schedule: { times: f.times } }
        const t = await api(p.id ? `/api/tasks/${p.id}` : '/api/tasks', { method: p.id ? 'PUT' : 'POST', body })
        notify('已保存'); refreshSched(); location.hash = '/tasks'
        return t
      } catch (e) { notify(e.message, 'err') } finally { saving.value = false }
    }
    return { f, newTime, saving, preview, events, loaded, addTime, save, sched, TABLE_LABEL }
  },
  template: `
  <div v-if="loaded" class="te">
    <div class="card banner">
      <Switch v-model="f.enabled"/><Tip text="启用后按下面的定时自动执行；停用也能在列表里手动执行"/>
      <div class="grow"><input v-model="f.name" class="name-input" placeholder="任务名称，如：九嶷山每日跑批" />
        </div>
      <Tag :color="f.enabled ? 'green' : 'grey'" :icon="f.enabled ? 'tickCircle' : 'minus'">{{ f.enabled ? '已启用' : '已停用' }}</Tag>
    </div>

    <div class="te-layout">
      <div class="te-main">
        <TaskBody :cfg="f.config" :push-off="!!(preview && preview.push_off)">
          <template #schedule>
            <div class="frow"><label>定时<Tip text="每天几点自动执行（服务器时间）。不加时间 = 只手动执行；多个时间各跑一次，例如 08:10 跑完、20:10 再补一遍晚到的评论"/></label>
              <div class="chips">
                <span v-for="(t,i) in f.times" :key="t" class="time-chip">{{ t }}<a @click="f.times.splice(i,1)">×</a></span>
                <span class="time-add"><input v-model="newTime" type="time" @keyup.enter="addTime"/><Icon name="clock" :size="14"/></span>
                <button class="btn light sm" @click="addTime">添加</button></div></div>
            <div v-if="sched.data && !sched.data.enabled" class="alert orange small">定时总开关已暂停，到点不会执行</div>
          </template>
        </TaskBody>
        <div class="savebar">
          <span class="hint">下次运行：{{ preview && preview.ok && preview.next_fires.length && f.enabled ? preview.next_fires[0] : '—' }}</span>
          <a href="#/tasks" class="btn light">取消</a>
          <button class="btn primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存任务' }}</button>
        </div>
      </div>
      <aside class="te-side">
        <div class="card sect">
          <div class="sect-head"><h3>预览<Tip text="按当前设置，这次定时会跑什么（目标日期按今天算）"/></h3></div>
          <div v-if="preview && preview.ok" class="preview">
            <div class="pv-row"><span>下次运行</span><div class="pv-list"><b v-for="x in preview.next_fires" :key="x">{{ x }}</b>
              <b v-if="!preview.next_fires.length">仅手动</b></div></div>
            <div class="pv-row"><span>目标日期</span><b>{{ preview.start === preview.end ? preview.start : preview.start + ' ~ ' + preview.end }}</b></div>
            <div class="pv-row"><span>景区</span><b>{{ preview.scenics.length ? preview.scenics.join('、') : '有评论的全部景区' }}</b></div>
            <div class="pv-row"><span>计算</span><div class="chips">
              <Tag v-if="!preview.tables.length" color="indigo">全部 6 张表</Tag>
              <Tag v-for="k in preview.tables" :key="k" color="indigo">{{ TABLE_LABEL[k] }}</Tag></div></div>
            <div class="pv-row"><span>推送</span><div class="chips">
              <Tag v-for="k in preview.push" :key="k" color="cyan" icon="send">{{ TABLE_LABEL[k] }}</Tag>
              <Tag v-if="!preview.push.length" color="grey">{{ preview.push_off ? '推送总开关关闭' : '不推送' }}</Tag></div></div>
            <code class="cmd block">{{ preview.command }}</code>
          </div>
          <div v-else-if="preview" class="alert red">{{ preview.error }}</div>
        </div>
        <EventLog v-if="id" :events="events" sub="这个任务的触发 / 跳过记录" />
      </aside>
    </div>
  </div>`,
}
