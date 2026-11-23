// 演示数据：给演示景区（默认 PFTSCM00000001 A级景区）按日期区间生成数据（engin_cli mock-gen）
// 评论是按「景区 + 日期」凭空生成的，再走正常的跑批计算；同一天每次生成结果都一样，分几次生成、重复生成都没关系
import { ref, reactive, onMounted, computed, watch } from 'vue'
import { api, notify, todayYmd, TABLES, TABLE_LABEL, fmtNum } from '../api.js'
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
    return { f, cov, loading, busy, pushOff, picked, toggle, rows, days, lastDay, fillToToday, quick, missDays, submit,
             load, TABLES, TABLE_LABEL, fmtNum, todayYmd, ymd, segText }
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
  </div>`,
}
