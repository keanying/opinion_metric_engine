import { ref, reactive, onMounted, computed } from 'vue'
import { api, todayYmd, notify } from '../api.js'
import { Ring } from '../charts.js'

export default {
  components: { Ring },
  setup() {
    const days = ref(14), data = ref({ items: [] }), err = ref(''), loading = ref(true), kw = ref('')
    const scenics = ref([])
    const chk = reactive({ scenic: '', start: todayYmd(-30), end: todayYmd(), running: false, result: null })
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
      chk.running = true; chk.result = null
      try { chk.result = await api('/api/quality/check', { method: 'POST', body: { scenic: chk.scenic, start: chk.start, end: chk.end } }) }
      catch (e) { notify(e.message, 'err') } finally { chk.running = false }
    }
    const stats = computed(() => {
      const it = data.value.items
      return { blocked: it.filter(x => x.quality_blocked).length, fixed: it.filter(x => x.ok && x.attempts > 1).length,
               failed: it.filter(x => !x.ok && !x.quality_blocked).length }
    })
    const parts = computed(() => [
      { value: stats.value.blocked, label: '回退', colors: ['#ffc15e', '#ff8a3d'], icon: 'alert' },
      { value: stats.value.fixed, label: '重算后通过', colors: ['#8b6cff', '#4f7cff'], icon: 'refresh' },
      { value: stats.value.failed, label: '其他失败', colors: ['#ff7aa2', '#f2456e'], icon: 'clear' }])
    const shown = computed(() => data.value.items.filter(x => !kw.value ||
      JSON.stringify(x).toLowerCase().includes(kw.value.toLowerCase())))
    const cols = [
      { key: 'run', title: '执行', width: 260 }, { key: 'scenic', title: '景区', width: 190 },
      { key: 'result', title: '结果', width: 130 }, { key: 'attempts', title: '计算次数', width: 90 },
      { key: 'errors', title: '问题' }]
    return { days, data, err, loading, kw, shown, load, scenics, chk, check, stats, parts, cols }
  },
  template: `
  <div>
    <div class="grid one-two">
      <div class="card center">
        <div class="card-head"><h3>近 {{ days }} 天检测问题<small>景区 · 次</small></h3></div>
        <Ring :parts="parts" :size="230" :thickness="22" :value="String(stats.blocked + stats.fixed + stats.failed)"
          label="问题" sub="景区·次" :hole="0.5" />
        <div class="stat4 three">
          <div><span class="cap">回退</span><b>{{ stats.blocked }}</b><i class="ubar"><em style="width:60%;background:#ff9a3d"></em></i></div>
          <div><span class="cap">重算后通过</span><b>{{ stats.fixed }}</b><i class="ubar"><em style="width:60%;background:#6c63ff"></em></i></div>
          <div><span class="cap">其他失败</span><b>{{ stats.failed }}</b><i class="ubar"><em style="width:60%;background:#f2456e"></em></i></div>
        </div>
      </div>
      <div class="card sect">
        <div class="sect-head"><h3>在线检测库里的数据<small>只读，不改数据</small></h3></div>
        <p class="hint" style="margin:0 0 14px">对 ADS 大盘表已有的数据做检测：区间相同的行指标一致（td@T-1 = latest_1d@T …）、
          各周期评论数 = 每天 td 之和。用来发现历史数据里的不一致（例如晚到的评论导致前后几天对不上）。</p>
        <div class="sect-body">
          <div class="frow"><label>景区</label>
            <select v-if="scenics.length" v-model="chk.scenic" class="grow">
              <option v-for="s in scenics" :key="s.scenic_id" :value="s.scenic_id">{{ s.scenic_name || s.scenic_id }}（{{ s.scenic_id }}）</option></select>
            <input v-else v-model="chk.scenic" class="grow" placeholder="景区编号" /></div>
          <div class="frow"><label>日期</label><input v-model="chk.start" class="w140" /><span class="hint">~</span>
            <input v-model="chk.end" class="w140" />
            <button class="btn primary" :disabled="chk.running" @click="check"><Icon name="shield" :size="14"/>{{ chk.running ? '检测中…' : '开始检测' }}</button></div>
        </div>
        <div v-if="chk.result" class="check-result">
          <div class="alert" :class="chk.result.errors.length ? 'red' : 'green'">{{ chk.result.message }}（{{ chk.result.rows }} 行）</div>
          <ul class="checklist"><li v-for="c in chk.result.checks" :key="c">{{ c }}</li></ul>
          <ul v-if="chk.result.errors.length" class="errs"><li v-for="(e,i) in chk.result.errors" :key="i">{{ e }}</li></ul>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h3>检测记录<small>每次跑批算完都检测；不通过先重算，还不通过就回退并发飞书通知</small></h3>
        <div class="toolbar">
          <div class="seg"><button v-for="d in [7,14,30]" :key="d" :class="{on: days===d}" @click="days=d; load()">近 {{ d }} 天</button></div>
          <span class="search"><Icon name="search" :size="15"/><input v-model="kw" placeholder="景区 / 问题"/></span>
          <button class="btn light" @click="kw=''; load()">重置</button></div></div>
      <div v-if="err" class="alert red">{{ err }}</div>
      <STable :columns="cols" :data="shown" :loading="loading" :min-height="300" row-key="__k"
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
  </div>`,
}
