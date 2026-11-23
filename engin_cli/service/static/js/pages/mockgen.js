// 演示数据：给演示景区（默认 PFTSCM00000001 A级景区）按日期区间生成数据（engin_cli mock-gen）
// 评论是按「景区 + 日期」凭空生成的，再走正常的跑批计算；同一天每次生成结果都一样，分几次生成、重复生成都没关系
import { ref, reactive, onMounted, computed, watch } from 'vue'
import { api, notify, todayYmd, TABLES, TABLE_LABEL, fmtNum, confirmBox } from '../api.js'
import { loadScenics } from '../ui.js'

const ymdAdd = (s, n) => {
  const d = new Date(+s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6) + n)
  return `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`
}
const ymd = s => s ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}` : ''
const segText = g => g.start === g.end ? ymd(g.start) : `${ymd(g.start)} ~ ${ymd(g.end)}`

export default {
  setup() {
    const f = reactive({ scenic: 'PFTSCM00000001', name: 'A级景区', start: todayYmd(-29), end: todayYmd(),
                         tables: 'drill,macro,core,platform,dimension,content', write_db: true, push: true })
    const cov = ref(null), loading = ref(false), busy = ref(false), pushOff = ref(false)
    const picked = computed(() => f.tables.split(',').filter(Boolean))
    const load = async () => {
      loading.value = true
      try { cov.value = await api('/api/mockgen/coverage?' + new URLSearchParams({ scenic: f.scenic, start: f.start, end: f.end })) }
      catch (e) { notify(e.message, 'err') } finally { loading.value = false }
    }
    onMounted(async () => {
      try { pushOff.value = !(await api('/api/push/settings')).settings.enabled } catch (e) { /* 忽略 */ }
      await load()
      if (cov.value) { f.tables = cov.value.default_tables.join(','); if (f.scenic === cov.value.default_scenic) f.name = cov.value.default_name }
    })
    // 换景区：名称跟着景区表里的名字走
    watch(() => f.scenic, async v => {
      const o = (await loadScenics()).find(x => x.scenic_id === v)
      if (o && o.scenic_name) f.name = o.scenic_name
      load()
    })
    watch(() => [f.start, f.end], load)
    const toggle = k => {
      const s = new Set(picked.value)
      s.has(k) ? s.delete(k) : s.add(k)
      f.tables = TABLES.map(t => t.key).filter(x => s.has(x)).join(',')
    }
    const rows = computed(() => (cov.value ? cov.value.tables : []).filter(t => picked.value.includes(t.key)))
    const days = computed(() => f.start && f.end ? Math.round((new Date(ymd(f.end)) - new Date(ymd(f.start))) / 86400000) + 1 : 0)
    // 选中的表里最早「最后一天」：从它的下一天补到今天
    const lastDay = computed(() => {
      const ls = rows.value.map(t => t.last)
      return ls.length && ls.every(Boolean) ? ls.sort()[0] : ''
    })
    const fillToToday = () => {
      if (!lastDay.value) { notify('选中的表还有没生成过的，请直接选日期'); return }
      if (lastDay.value >= todayYmd()) { notify('已经生成到今天了'); return }
      f.start = ymdAdd(lastDay.value, 1); f.end = todayYmd()
    }
    const quick = n => { f.start = todayYmd(-(n - 1)); f.end = todayYmd() }
    const missDays = t => (t.missing || []).reduce((a, g) => a + g.days, 0)
    const submit = async () => {
      if (!picked.value.length) { notify('至少选一张表', 'err'); return }
      busy.value = true
      try {
        const r = await api('/api/mockgen/run', { method: 'POST', body: {
          scenic: f.scenic, name: f.name, start: f.start, end: f.end, tables: f.tables,
          push: f.push ? f.tables : '', skip_db: !f.write_db } })
        notify(`已提交执行 #${r.run_id}`); location.hash = '/runs/' + r.run_id
      } catch (e) { notify(e.message, 'err') } finally { busy.value = false }
    }
    // ── 每日自动生成：时间 / 近几天在这里设，景区 / 表 / 写库 / 推送 保存时取上面「生成数据」的设置
    const sc = ref(null), sf = reactive({ enabled: false, times: [], recent_days: 3 }), newTime = ref(''), saving = ref(false)
    const loadSc = async () => {
      try { sc.value = await api('/api/mockgen/schedule')
        Object.assign(sf, { enabled: sc.value.config.enabled, times: [...sc.value.config.times], recent_days: sc.value.config.recent_days })
      } catch (e) { notify(e.message, 'err') }
    }
    onMounted(loadSc)
    const addTime = () => {
      const t = newTime.value.trim()
      if (!/^([01]?\d|2[0-3]):[0-5]\d$/.test(t)) return notify('时间格式是 HH:MM，例如 01:20', 'err')
      const v = t.padStart(5, '0')
      if (!sf.times.includes(v)) sf.times.push(v)
      sf.times.sort(); newTime.value = ''
    }
    const tabText = v => { const k = String(v || '').split(',').filter(Boolean)
      return !k.length ? '不推送' : k.length === TABLES.length ? '全部 6 张' : k.map(x => TABLE_LABEL[x] || x).join('、') }
    const saveSc = async () => {
      if (sf.enabled && !picked.value.length) { notify('至少选一张表', 'err'); return }
      const body = { enabled: sf.enabled, times: sf.times, recent_days: sf.recent_days, scenic: f.scenic, name: f.name,
                     tables: f.tables, push: f.push ? f.tables : '', skip_db: !f.write_db }
      if (sf.enabled && !await confirmBox({ title: '保存每日自动生成', okText: '保存', items: [
        ['时间', '每天 ' + sf.times.join('、')], ['日期', `每次生成近 ${sf.recent_days} 天（到当天）`],
        ['景区', `${f.name}（${f.scenic}）`], ['表', tabText(f.tables)], ['写库', f.write_db ? '写入 ADS 库' : '不写库'],
        ['推送', f.push ? (pushOff.value ? '推送（推送总开关已关闭，暂不推）' : '推送') : '不推送']] })) return
      saving.value = true
      try { sc.value = await api('/api/mockgen/schedule', { method: 'PUT', body }); notify('已保存'); }
      catch (e) { notify(e.message, 'err') } finally { saving.value = false }
    }
    // 把保存过的定时设置填回上面的表单（看 / 改的时候用）
    const useSaved = () => {
      const c = sc.value && sc.value.config; if (!c) return
      Object.assign(f, { scenic: c.scenic, name: c.name, tables: c.tables, push: !!c.push, write_db: !c.skip_db })
    }
    return { f, cov, loading, busy, pushOff, picked, toggle, rows, days, lastDay, fillToToday, quick, missDays, submit,
             load, TABLES, TABLE_LABEL, fmtNum, todayYmd, ymd, segText,
             sc, sf, newTime, saving, addTime, saveSc, useSaved, tabText }
  },
  template: `
  <div>
    <div class="card banner">
      <span class="icon-sq grad"><Icon name="file" :size="18" :sw="2.2"/></span>
      <div class="grow"><b>演示数据</b><Tip wide text="给演示景区按日期生成数据：按「景区 + 日期」造出当天的评论（只在内存里，不写源表），再按正常口径算出各张表，写进 ADS 库 / 推给下游。每天的评论量、好中差比例、渠道、维度、关键词都不一样；同一天不管生成几次结果都一样，所以分几次生成、重复生成都没关系。景区在源表里有真实评论时会拒绝生成，免得覆盖真实数据"/>
        <p>{{ f.name || '-' }}（{{ f.scenic }}） · 不影响其他景区的跑批</p></div>
    </div>

    <div class="grid two">
      <div class="card sect">
        <div class="sect-head"><h3>生成数据</h3></div>
        <div class="sect-body">
          <div class="frow"><label>景区</label><ScenicPicker v-model="f.scenic" single placeholder="演示景区" /></div>
          <div class="frow"><label>名称</label><input v-model="f.name" class="grow" placeholder="A级景区" /></div>
          <div class="frow"><label>日期</label><DateRange v-model:start="f.start" v-model:end="f.end" :clearable="false" :max="todayYmd()" />
            <span class="hint">{{ days }} 天</span></div>
          <div class="frow"><label></label><div class="chips">
            <button class="chip-btn" :disabled="!lastDay" @click="fillToToday" :title="lastDay ? '从已生成的最后一天（' + ymd(lastDay) + '）往后补到今天' : '选中的表还有没生成过的'">补到今天</button>
            <button class="chip-btn" @click="quick(7)">近 7 天</button>
            <button class="chip-btn" @click="quick(30)">近 30 天</button>
            <button class="chip-btn" @click="quick(90)">近 90 天</button>
            <button class="chip-btn" @click="quick(365)">近一年</button></div></div>
          <div class="frow"><label>表</label><div class="chips">
            <button v-for="t in TABLES" :key="t.key" class="chip-btn" :class="{on: picked.includes(t.key)}" :title="t.table"
              @click="toggle(t.key)">{{ t.label }}</button></div></div>
          <div class="opts compact">
            <div class="opt"><div><b>写入 ADS 库</b><Tip text="按「景区 + 日期」先删后写，和跑批一样；重复生成同一段日期会覆盖成同样的数据"/></div><Switch v-model="f.write_db"/></div>
            <div class="opt"><div><b>推送下游</b><Tip :text="pushOff ? '推送总开关已关闭（推送页），这次只生成不推送' : '生成后把选中的表推给下游，推送设置和跑批一样'"/></div>
              <Switch v-model="f.push"/></div>
          </div>
          <div class="actions"><span class="hint">一次最多 {{ cov ? cov.max_days : 400 }} 天，一年大约 1 分钟；更长的分几段生成</span>
            <button class="btn primary" :disabled="busy || !f.start || !f.end" @click="submit"><Icon name="play" :size="14"/>开始生成</button></div>
        </div>
      </div>

      <div class="card sect">
        <div class="sect-head"><h3>已生成的数据<small>{{ f.scenic }}</small></h3>
          <button class="s-btn-icon" title="刷新" @click="load"><Icon name="refresh" :size="16" :spin="loading"/></button></div>
        <div class="sect-body">
          <div v-if="cov && cov.db_error" class="hint t-orange">查不到库里的数据：{{ cov.db_error }}</div>
          <div v-for="t in rows" :key="t.key" class="mg-row">
            <div class="mg-head"><b>{{ TABLE_LABEL[t.key] }}</b><small class="mono">{{ t.table }}</small>
              <span class="grow"></span>
              <Tag v-if="t.error" color="orange" icon="alert">查询失败</Tag>
              <Tag v-else-if="!t.days" color="grey">还没有数据</Tag>
              <Tag v-else color="green">{{ fmtNum(t.days) }} 天</Tag></div>
            <div v-if="t.segments.length" class="chips">
              <span v-for="g in t.segments.slice(-6)" :key="g.start" class="chip grey">{{ segText(g) }}</span>
              <span v-if="t.segments.length > 6" class="hint">等 {{ t.segments.length }} 段</span></div>
            <div v-if="(t.missing || []).length" class="chips">
              <span class="hint">所选日期里没有的：</span>
              <span v-for="g in t.missing.slice(0, 6)" :key="g.start" class="chip warn">{{ segText(g) }}</span>
              <span v-if="t.missing.length > 6" class="hint">等 {{ missDays(t) }} 天</span></div>
            <div v-else-if="!t.error && t.days" class="hint">所选日期已全部生成</div>
          </div>
          <div v-if="!rows.length" class="hint">先在左边选表</div>
        </div>
      </div>
    </div>

    <div class="card sect" v-if="sc">
      <div class="sect-head"><h3>每日自动生成<Tip wide text="每天到点自动生成「近 N 天」的数据（到当天为止），哪天没跑成下一次顺带补上；同一天重复生成结果一样。景区、表、写库、推送保存时取上面「生成数据」里的设置。到点规则和任务定时一样：服务停机超过 30 分钟的不补跑，定时总开关关着时不执行"/></h3>
        <span class="mg-sc-state">
          <Tag v-if="sc.error" color="orange" icon="alert">设置有误：{{ sc.error }}</Tag>
          <Tag v-else-if="!sc.config.enabled" color="grey">未开启</Tag>
          <Tag v-else-if="!sc.scheduler_enabled" color="orange" icon="alert">定时总开关已暂停，到点不会执行</Tag>
          <Tag v-else color="green" icon="clock">下次 {{ sc.next_fire }}</Tag>
          <span v-if="sc.last_run" class="hint">上次：<a class="link" :href="'#/runs/' + sc.last_run.id">#{{ sc.last_run.id }}</a>
            <StatusTag :status="sc.last_run.status"/></span></span></div>
      <div class="sect-body">
        <div class="frow"><label>开启</label><Switch v-model="sf.enabled"/></div>
        <div class="frow"><label>每天</label><div class="chips">
          <span v-for="(t,i) in sf.times" :key="t" class="time-chip">{{ t }}<a @click="sf.times.splice(i,1)">×</a></span>
          <span class="time-add"><input v-model="newTime" type="time" @keyup.enter="addTime"/><Icon name="clock" :size="14"/></span>
          <button class="btn light" @click="addTime"><Icon name="plus" :size="13" :sw="2.4"/>添加</button></div></div>
        <div class="frow"><label>每次生成</label>近 <input type="number" min="1" max="60" v-model.number="sf.recent_days" class="w64"/> 天（到当天为止）</div>
        <div class="frow ro" v-if="sc.saved"><label>已保存</label><div class="chips">
          <span class="chip grey">{{ sc.config.name }}（{{ sc.config.scenic }}）</span>
          <span class="chip grey">表：{{ tabText(sc.config.tables) }}</span>
          <span class="chip grey">{{ sc.config.skip_db ? '不写库' : '写库' }}</span>
          <span class="chip grey">推送：{{ tabText(sc.config.push) }}</span>
          <a class="link" @click="useSaved">填回上面的表单</a></div></div>
        <div class="frow ro" v-if="sc.command_preview && sc.config.enabled"><label>命令</label>
          <span class="mono ell grow" :title="sc.command_preview">{{ sc.command_preview }}</span></div>
        <div class="actions"><span class="hint">景区、表、写库、推送用上面「生成数据」里当前的设置</span>
          <button class="btn primary" :disabled="saving" @click="saveSc">{{ saving ? '保存中…' : '保存' }}</button></div>
      </div>
    </div>
  </div>`,
}
