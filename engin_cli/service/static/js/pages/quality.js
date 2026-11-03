import { ref, reactive, onMounted, onUnmounted, computed } from 'vue'
import { api, todayYmd, notify, CHANNELS, confirmBox, STATUS } from '../api.js'
import { Ring } from '../charts.js'

export default {
  components: { Ring },
  setup() {
    const days = ref(14), data = ref({ items: [] }), err = ref(''), loading = ref(true), kw = ref('')
    const scenics = ref([]), today = todayYmd()
    const chk = reactive({ scenic: '', start: todayYmd(-30), end: todayYmd(), running: false, result: null,
                           push: true, repair: null })   // repair：{ id, status, start(ms) } 提交的修复执行
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
    // q：不传就按表单上的景区 / 日期；修复跑完自动复检时传修复的那段
    const check = async (q) => {
      q = q && q.scenic ? q : { scenic: chk.scenic, start: chk.start, end: chk.end }
      chk.running = true; chk.result = null; chk.repair = null; stopWatch()
      try { chk.result = await api('/api/quality/check', { method: 'POST', body: q })
        chk.push = chk.result.push_ready }
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
                ['写库', '是'], ['推送', chk.push ? '是（下钻表、大盘表）' : '否']] })) return
      try {
        const x = await api('/api/quality/repair', { method: 'POST',
          body: { scenic: r.scenic, start: r.start, end: r.end, bad_days: r.days.length, push: chk.push } })
        watchRepair(x.run_id, { scenic: r.scenic, start: r.start, end: r.end })
        notify(`已提交修复 #${x.run_id}，跑完会自动重新检测`)
      } catch (e) { notify(e.message, 'err') }
    }
    const spanDays = computed(() => {
      const r = chk.result
      if (!r) return 0
      const t = d => new Date(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6)).getTime()
      return Math.round((t(r.end) - t(r.start)) / 864e5) + 1
    })
    const wk = d => '周' + '日一二三四五六'[new Date(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6)).getDay()]
    const fmtD = d => `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}`
    const stats = computed(() => {
      const it = data.value.items
      return { blocked: it.filter(x => x.quality_blocked).length, fixed: it.filter(x => x.ok && x.attempts > 1).length,
               failed: it.filter(x => !x.ok && !x.quality_blocked).length }
    })
    const parts = computed(() => [
      { value: stats.value.blocked, label: '回退', colors: ['#ffd88a', '#f5a524'], icon: 'alert' },
      { value: stats.value.fixed, label: '重算后通过', colors: ['#a194ec', '#5446b8'], icon: 'refresh' },
      { value: stats.value.failed, label: '其他失败', colors: ['#f6a4b8', '#ee5a7c'], icon: 'clear' }])
    const shown = computed(() => data.value.items.filter(x => !kw.value ||
      JSON.stringify(x).toLowerCase().includes(kw.value.toLowerCase())))
    const cols = [
      { key: 'run', title: '执行', width: 220 }, { key: 'scenic', title: '景区', width: 170 },
      { key: 'result', title: '结果', width: 120 }, { key: 'attempts', title: '次数', width: 60 },
      { key: 'errors', title: '问题' }]
    const dayCols = [
      { key: 'date', title: '日期', width: 170 }, { key: 'count', title: '问题', width: 190 },
      { key: 'channels', title: '渠道', width: 110 }, { key: 'samples', title: '明细' }]
    return { days, data, err, loading, kw, shown, load, scenics, chk, check, repair, repairText, spanDays, wk, fmtD, today,
             stats, parts, cols, dayCols, CHANNELS }
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
        </div>
        <div v-if="chk.result" class="check-result">
          <div class="result-bar" :class="chk.result.days.length ? 'bad' : 'ok'">
            <Icon :name="chk.result.days.length ? 'alert' : 'tickCircle'" :size="18" :sw="2.2"/>
            <b>{{ chk.result.message }}</b><span class="muted small">{{ chk.result.scenic }} · {{ fmtD(chk.result.start) }} ~ {{ fmtD(chk.result.end) }} · {{ chk.result.rows }} 行</span>
            <span class="grow"></span>
            <template v-if="chk.result.days.length">
              <label class="check sm"><Switch v-model="chk.push" size="sm" :disabled="!chk.result.push_ready"/>修复后推送
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
        <div class="card-head"><h3>近 {{ days }} 天跑批检测<Tip text="每次跑批算完自动检测的结果（景区 · 次）"/></h3></div>
        <Ring :parts="parts" :size="200" :thickness="20" :value="String(stats.blocked + stats.fixed + stats.failed)"
          label="问题" sub="景区·次" :hole="0.5" />
        <div class="stat4 three">
          <div><span class="cap">回退</span><b>{{ stats.blocked }}</b><i class="ubar"><em style="width:60%;background:#f5a524"></em></i></div>
          <div><span class="cap">重算后通过</span><b>{{ stats.fixed }}</b><i class="ubar"><em style="width:60%;background:#6b5bd0"></em></i></div>
          <div><span class="cap">其他失败</span><b>{{ stats.failed }}</b><i class="ubar"><em style="width:60%;background:#ee5a7c"></em></i></div>
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
