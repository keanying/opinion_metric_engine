import { ref, reactive, onMounted, computed } from 'vue'
import { api, todayYmd, notify, CHANNELS } from '../api.js'
import { Ring } from '../charts.js'

export default {
  components: { Ring },
  setup() {
    const days = ref(14), data = ref({ items: [] }), err = ref(''), loading = ref(true), kw = ref('')
    const scenics = ref([]), today = todayYmd()
    const chk = reactive({ scenic: '', start: todayYmd(-30), end: todayYmd(), running: false, result: null,
                           push: true, repaired: {} })
    const load = async () => {
      loading.value = true
      try { data.value = await api('/api/quality?days=' + days.value); err.value = '' } catch (e) { err.value = e.message }
      finally { loading.value = false }
    }
    onMounted(async () => {
      load()
      try { scenics.value = await api('/api/metrics/scenics')
        if (scenics.value.length && !chk.scenic) chk.scenic = scenics.value[0].scenic_id } catch (e) { /* 库没配 */ }
    })
    const check = async () => {
      chk.running = true; chk.result = null; chk.repaired = {}
      try { chk.result = await api('/api/quality/check', { method: 'POST', body: { scenic: chk.scenic, start: chk.start, end: chk.end } })
        chk.push = chk.result.push_ready }
      catch (e) { notify(e.message, 'err') } finally { chk.running = false }
    }
    const repair = async dates => {
      const r = chk.result
      if (!dates.length) return
      const span = dates.length === 1 ? dates[0] : `${dates[0]} ~ ${dates[dates.length - 1]}`
      if (!confirm(`按当前源数据重算 ${r.scenic} ${span}（${dates.length} 天有问题）并写库${chk.push ? '、推送' : ''}？`)) return
      try {
        const x = await api('/api/quality/repair', { method: 'POST', body: { scenic: r.scenic, dates, push: chk.push } })
        for (const d of dates) chk.repaired[d] = x.run_id
        notify(`已提交修复 #${x.run_id}，跑完后点「重新检测」确认`)
      } catch (e) { notify(e.message, 'err') }
    }
    const allDates = computed(() => (chk.result?.days || []).map(d => d.date))
    const wk = d => '周' + '日一二三四五六'[new Date(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6)).getDay()]
    const fmtD = d => `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}`
    const stats = computed(() => {
      const it = data.value.items
      return { blocked: it.filter(x => x.quality_blocked).length, fixed: it.filter(x => x.ok && x.attempts > 1).length,
               failed: it.filter(x => !x.ok && !x.quality_blocked).length }
    })
    const parts = computed(() => [
      { value: stats.value.blocked, label: '回退', colors: ['#e7c98e', '#c9974f'], icon: 'alert' },
      { value: stats.value.fixed, label: '重算后通过', colors: ['#86a490', '#4a6a56'], icon: 'refresh' },
      { value: stats.value.failed, label: '其他失败', colors: ['#e2a197', '#c9655b'], icon: 'clear' }])
    const shown = computed(() => data.value.items.filter(x => !kw.value ||
      JSON.stringify(x).toLowerCase().includes(kw.value.toLowerCase())))
    const cols = [
      { key: 'run', title: '执行', width: 220 }, { key: 'scenic', title: '景区', width: 170 },
      { key: 'result', title: '结果', width: 120 }, { key: 'attempts', title: '次数', width: 60 },
      { key: 'errors', title: '问题' }]
    const dayCols = [
      { key: 'date', title: '日期', width: 140 }, { key: 'count', title: '问题', width: 170 },
      { key: 'channels', title: '渠道', width: 110 }, { key: 'samples', title: '明细', width: 380 },
      { key: 'op', title: '', width: 110, align: 'r' }]
    return { days, data, err, loading, kw, shown, load, scenics, chk, check, repair, allDates, wk, fmtD, today,
             stats, parts, cols, dayCols, CHANNELS }
  },
  template: `
  <div>
    <div class="card sect open">
        <div class="sect-head"><h3>数据检测<Tip wide text="只读检测库里大盘表已有的数据：区间相同的行指标一致（td@T-1 = latest_1d@T …）、各周期评论数 = 每天 td 之和。检测出来哪天不对，可以点「修复」按当前源数据把那几天重算一遍"/></h3></div>
        <div class="sect-body stick">
          <div class="frow">
            <select v-if="scenics.length" v-model="chk.scenic" class="w240">
              <option v-for="s in scenics" :key="s.scenic_id" :value="s.scenic_id">{{ s.scenic_name || s.scenic_id }}（{{ s.scenic_id }}）</option></select>
            <input v-else v-model="chk.scenic" class="w240" placeholder="景区编号" />
            <DateRange v-model:start="chk.start" v-model:end="chk.end" :clearable="false" :max="today" />
            <button class="btn primary" :disabled="chk.running || !chk.scenic" @click="check"><Icon name="shield" :size="14"/>{{ chk.running ? '检测中…' : '开始检测' }}</button>
          </div>
        </div>
        <div v-if="chk.result" class="check-result">
          <div class="result-bar" :class="chk.result.days.length ? 'bad' : 'ok'">
            <Icon :name="chk.result.days.length ? 'alert' : 'tickCircle'" :size="18" :sw="2.2"/>
            <b>{{ chk.result.message }}</b><span class="muted small">{{ chk.result.scenic }} · {{ fmtD(chk.result.start) }} ~ {{ fmtD(chk.result.end) }} · {{ chk.result.rows }} 行</span>
            <span class="grow"></span>
            <template v-if="chk.result.days.length">
              <label class="check sm"><Switch v-model="chk.push" size="sm" :disabled="!chk.result.push_ready"/>修复后推送
                <Tip v-if="!chk.result.push_ready" text="推送总开关关着或没配推送地址，修复只重算写库、不推送（推送页可以配置）"/></label>
              <button class="btn primary sm" @click="repair(allDates)"><Icon name="tool" :size="14"/>全部修复（{{ chk.result.days.length }} 天）</button>
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
            <template #op="{ row }">
              <a v-if="chk.repaired[row.date]" class="btn light sm" :href="'#/runs/' + chk.repaired[row.date]">修复中 #{{ chk.repaired[row.date] }}</a>
              <button v-else class="btn light sm" @click="repair([row.date])"><Icon name="tool" :size="14"/>修复</button></template>
          </STable>
          <ul v-else class="checklist"><li v-for="c in chk.result.checks" :key="c">{{ c }} ✓</li></ul>
        </div>
      </div>

    <div class="grid one-two">
      <div class="card center">
        <div class="card-head"><h3>近 {{ days }} 天跑批检测<Tip text="每次跑批算完自动检测的结果（景区 · 次）"/></h3></div>
        <Ring :parts="parts" :size="200" :thickness="20" :value="String(stats.blocked + stats.fixed + stats.failed)"
          label="问题" sub="景区·次" :hole="0.5" />
        <div class="stat4 three">
          <div><span class="cap">回退</span><b>{{ stats.blocked }}</b><i class="ubar"><em style="width:60%;background:#c9974f"></em></i></div>
          <div><span class="cap">重算后通过</span><b>{{ stats.fixed }}</b><i class="ubar"><em style="width:60%;background:#5d7c68"></em></i></div>
          <div><span class="cap">其他失败</span><b>{{ stats.failed }}</b><i class="ubar"><em style="width:60%;background:#c9655b"></em></i></div>
        </div>
      </div>
      <div class="card">
        <div class="card-head stick"><h3>检测记录<Tip text="每次跑批算完都检测；不通过先重算，还不通过就回退（不写库、不推送）并发飞书通知"/></h3>
          <div class="toolbar">
            <div class="seg"><button v-for="d in [7,14,30]" :key="d" :class="{on: days===d}" @click="days=d; load()">近 {{ d }} 天</button></div>
            <span class="search"><Icon name="search" :size="15"/><input v-model="kw" placeholder="景区 / 问题"/></span>
            <button class="btn light" @click="kw=''; load()">重置</button></div></div>
        <div v-if="err" class="alert red">{{ err }}</div>
        <STable :columns="cols" :data="shown" :loading="loading" :min-height="260" row-key="__k"
          :empty="data.items.length ? '搜索无结果' : '这段时间没有质量问题'">
          <template #run="{ row }"><span class="s-name"><Avatar square icon="shield" :color="row.quality_blocked ? 'amber' : 'indigo'"/>
            <span class="s-ell"><a :href="'#/runs/'+row.run_id"><b>{{ row.task_name || '临时执行' }}</b></a>
              <small>#{{ row.run_id }} · {{ row.created_at.slice(5,16) }}</small></span></span></template>
          <template #scenic="{ row }"><span class="s-name"><Avatar :text="row.scenic"/><span class="s-ell" :title="row.scenic">{{ row.scenic }}</span></span></template>
          <template #result="{ row }"><Tag :color="row.quality_blocked ? 'orange' : (row.ok ? 'blue' : 'pink')"
            :icon="row.quality_blocked ? 'alert' : (row.ok ? 'refresh' : 'clear')">{{ row.quality_blocked ? '已回退' : (row.ok ? '重算后通过' : '失败') }}</Tag></template>
          <template #errors="{ row }"><ul class="errs tight"><li v-for="(e,j) in row.errors" :key="j">{{ e }}</li></ul></template>
        </STable>
      </div>
    </div>
  </div>`,
}
