import { ref, reactive, onMounted, onUnmounted, watch, nextTick, computed } from 'vue'
import { api, KIND, TRIGGER, TABLE_LABEL, duration, fmtNum, notify } from '../api.js'
import { STATUS_TAG } from '../ui.js'

const KIND_AV = { run: ['layers', 'indigo'], push: ['send', 'cyan'], notify: ['bell', 'amber'] }

export const RunList = {
  setup() {
    const f = reactive({ status: '', kind: '', offset: 0, limit: 20 })
    const data = ref({ total: 0, items: [] }), err = ref(''), loading = ref(true)
    let timer
    const load = async () => {
      const q = new URLSearchParams({ status: f.status, kind: f.kind, offset: f.offset, limit: f.limit })
      try { data.value = await api('/api/runs?' + q); err.value = '' } catch (e) { err.value = e.message }
      finally { loading.value = false }
    }
    watch(() => [f.status, f.kind], () => { f.offset = 0; load() })
    onMounted(() => { load(); timer = setInterval(() => {
      if (data.value.items.some(r => ['queued', 'running'].includes(r.status))) load() }, 3000) })
    onUnmounted(() => clearInterval(timer))
    const page = d => { f.offset = Math.max(0, f.offset + d * f.limit); load() }
    const reset = () => { f.status = ''; f.kind = ''; f.offset = 0; load() }
    const cancel = async r => {
      if (!confirm(`取消执行 #${r.id}？`)) return
      try { await api(`/api/runs/${r.id}/cancel`, { method: 'POST', body: {} }); notify('已取消'); load() }
      catch (e) { notify(e.message, 'err') }
    }
    const cols = [
      { key: 'title', title: '执行', width: 380 }, { key: 'trigger', title: '触发', width: 120 },
      { key: 'status', title: '状态', width: 110 }, { key: 'scenic', title: '景区', width: 120 },
      { key: 'created_at', title: '创建时间', width: 160 }, { key: 'dur', title: '耗时', width: 90 },
      { key: 'op', title: '', width: 60, align: 'r' }]
    const pages = computed(() => Math.max(1, Math.ceil(data.value.total / f.limit)))
    return { f, data, err, loading, page, reset, cancel, cols, pages, STATUS_TAG, KIND, KIND_AV, TRIGGER, duration }
  },
  template: `
  <div>
    <div class="card">
      <div class="card-head"><h3>执行记录<Tip text="每一次跑批 / 补推的状态；点一行看各景区结果和完整日志"/></h3>
        <div class="toolbar">
          <div class="seg"><button v-for="s in ['', 'running', 'success', 'blocked', 'failed', 'cancelled']" :key="s"
            :class="{on: f.status===s}" @click="f.status=s">{{ s ? STATUS_TAG[s].text : '全部' }}</button></div>
          <select v-model="f.kind"><option value="">全部类型</option>
            <option v-for="(v,k) in KIND" :key="k" :value="k">{{ v }}</option></select>
          <button class="btn light" @click="reset">重置</button></div></div>
      <div v-if="err" class="alert red">{{ err }}</div>
      <STable :columns="cols" :data="data.items" :loading="loading" clickable :min-height="350"
        @row-click="r => $router.push('/runs/'+r.id)">
        <template #title="{ row }"><span class="s-name"><Avatar square :icon="(KIND_AV[row.kind]||KIND_AV.run)[0]" :color="(KIND_AV[row.kind]||KIND_AV.run)[1]"/>
          <span class="s-ell" :title="(row.task_name || '临时执行') + ' · ' + row.title">
            <b>{{ row.task_name || '临时执行' }}</b><small>#{{ row.id }} · {{ KIND[row.kind] || row.kind }} · {{ row.title }}</small></span></span></template>
        <template #trigger="{ row }"><Avatar :text="TRIGGER[row.trigger]" :color="row.trigger==='schedule' ? 'violet' : 'light-blue'"/> {{ TRIGGER[row.trigger] }}</template>
        <template #status="{ row }"><StatusTag :status="row.status"/></template>
        <template #scenic="{ row }"><template v-if="row.scenic_total">{{ row.scenic_total - row.scenic_failed }}/{{ row.scenic_total }} 通过
          <div v-if="row.quality_blocked" class="t-orange small">回退 {{ row.quality_blocked }}</div></template><span v-else class="muted">-</span></template>
        <template #created_at="{ row }">{{ row.created_at.slice(0,16) }}</template>
        <template #dur="{ row }">{{ duration(row.started_at, row.finished_at) || '-' }}</template>
        <template #op="{ row }"><button v-if="['queued','running'].includes(row.status)" class="s-btn-icon danger" title="取消"
          @click.stop="cancel(row)"><Icon name="stop" :size="15"/></button>
          <button v-else class="s-btn-icon" title="查看"><Icon name="more" :size="18" :sw="2.6"/></button></template>
      </STable>
      <div class="pager"><span class="muted">共 {{ data.total }} 条 · 第 {{ f.offset / f.limit + 1 }}/{{ pages }} 页</span>
        <button class="s-btn-icon" :disabled="f.offset===0" @click="page(-1)"><Icon name="arrowL" :size="15"/></button>
        <button class="s-btn-icon" :disabled="f.offset+f.limit>=data.total" @click="page(1)"><Icon name="arrowR" :size="15"/></button></div>
    </div>
  </div>`,
}

export const RunDetail = {
  props: { id: [String, Number] },
  setup(p) {
    const run = ref(null), err = ref(''), log = ref(''), logEl = ref(null), follow = ref(true)
    let offset = 0, timer, stopped = false
    const load = async () => {
      try { run.value = await api('/api/runs/' + p.id); err.value = '' } catch (e) { err.value = e.message }
    }
    const pull = async () => {
      if (stopped) return
      try {
        const r = await api(`/api/runs/${p.id}/log?offset=${offset}`)
        if (r.text) {
          log.value += r.text; offset = r.offset
          if (follow.value) { await nextTick(); if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight }
        }
        if (r.done && run.value && ['queued', 'running'].includes(run.value.status)) await load()
        if (!r.done || !run.value || ['queued', 'running'].includes(run.value.status)) timer = setTimeout(pull, 1500)
      } catch (e) { timer = setTimeout(pull, 4000) }
    }
    onMounted(async () => { await load(); pull() })
    onUnmounted(() => { stopped = true; clearTimeout(timer) })
    const cancel = async () => {
      if (!confirm('取消这次执行？')) return
      try { await api(`/api/runs/${p.id}/cancel`, { method: 'POST', body: {} }); notify('已取消'); load() }
      catch (e) { notify(e.message, 'err') }
    }
    const scenics = computed(() => (run.value?.summary?.scenics) || [])
    const rowsOf = s => Object.entries(s.tables || {}).map(([t, n]) => {
      const pp = (s.push || []).find(x => x.table === t)
      return { table: t, n, load: ((s.loads || []).find(x => x.table === t) || {}).rows, push: pp }
    })
    const tcols = [{ key: 'table', title: '表', width: 200 }, { key: 'n', title: '产出', align: 'r' },
      { key: 'load', title: '写入', align: 'r' }, { key: 'push', title: '推送', align: 'r', width: 120 }]
    return { run, err, log, logEl, follow, cancel, scenics, rowsOf, tcols, KIND, TRIGGER, TABLE_LABEL, duration, fmtNum }
  },
  template: `
  <div>
    <div class="detail-head">
      <a class="btn light sm" href="#/runs"><Icon name="arrowL" :size="14"/>执行记录</a>
      <h2>执行 #{{ id }}</h2><StatusTag v-if="run" :status="run.status"/>
      <span v-if="run" class="muted">{{ run.task_name || '临时执行' }} · {{ run.title }}</span>
      <span class="grow"></span>
      <button v-if="run && ['queued','running'].includes(run.status)" class="btn danger" @click="cancel"><Icon name="stop" :size="14"/>取消执行</button>
    </div>
    <div v-if="err" class="alert red">{{ err }}</div>
    <template v-if="run">
      <div class="kpis">
        <div class="card kpi"><span class="cap">类型 / 触发</span><b>{{ KIND[run.kind] }} · {{ TRIGGER[run.trigger] }}</b></div>
        <div class="card kpi"><span class="cap">开始</span><b>{{ run.started_at || '排队中' }}</b></div>
        <div class="card kpi"><span class="cap">耗时</span><b>{{ duration(run.started_at, run.finished_at) || '-' }}</b></div>
        <div class="card kpi"><span class="cap">退出码</span><b>{{ run.exit_code ?? '-' }}</b>
          <small v-if="run.message" class="t-red">{{ run.message }}</small></div>
      </div>
      <div class="card"><code class="cmd block">python -m engin_cli.cli {{ run.command.join(' ') }}</code></div>

      <div v-if="scenics.length" class="grid cards2">
        <div v-for="s in scenics" :key="s.scenic" class="card">
          <div class="card-head"><h3><span class="s-name"><Avatar :text="s.scenic" :size="26"/>{{ s.scenic }}</span></h3>
            <Tag :color="s.ok ? 'green' : (s.quality_blocked ? 'orange' : 'pink')" :icon="s.ok ? 'tickCircle' : (s.quality_blocked ? 'alert' : 'clear')">
              {{ s.ok ? '通过' : (s.quality_blocked ? '已回退' : '失败') }}</Tag></div>
          <div v-if="s.quality_attempts > 1" class="alert orange small">数据质量检测共计算 {{ s.quality_attempts }} 次</div>
          <div v-if="s.exception" class="alert red small">{{ s.exception }}</div>
          <ul v-if="s.errors && s.errors.length" class="errs"><li v-for="(e,i) in s.errors" :key="i">{{ e }}</li></ul>
          <STable v-if="rowsOf(s).length" :columns="tcols" :data="rowsOf(s)" row-key="table">
            <template #table="{ row }"><span :title="row.table">{{ TABLE_LABEL[row.table] || row.table }}</span></template>
            <template #n="{ row }">{{ fmtNum(row.n) }}</template>
            <template #load="{ row }">{{ fmtNum(row.load) }}</template>
            <template #push="{ row }"><template v-if="row.push">
              <Tag v-if="row.push.skipped" color="grey">跳过</Tag>
              <Tag v-else-if="row.push.ok" color="green" icon="tickCircle">{{ fmtNum(row.push.rows) }}</Tag>
              <Tag v-else color="pink" icon="clear" :title="row.push.error">失败</Tag></template><span v-else class="muted">-</span></template>
          </STable>
          <details v-if="s.notes && s.notes.length"><summary>说明 {{ s.notes.length }} 条</summary>
            <ul class="notes"><li v-for="(n,i) in s.notes" :key="i">{{ n }}</li></ul></details>
        </div>
      </div>

      <div class="card dark-card">
        <div class="card-head"><h3>执行日志<small>实时刷新</small></h3>
          <label class="check light"><input type="checkbox" v-model="follow" />自动滚动</label></div>
        <pre ref="logEl" class="log">{{ log || '（暂无输出）' }}</pre>
      </div>
    </template>
  </div>`,
}
