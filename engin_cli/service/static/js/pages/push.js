import { ref, reactive, onMounted, computed } from 'vue'
import { api, TABLES, TABLE_LABEL, PUSH_RESULT, fmtNum, notify, refreshSched, todayYmd } from '../api.js'
import { Ring } from '../charts.js'

const RING_C = { ok: ['#a194ec', '#5446b8'], dry_run: ['#8be3ef', '#22b8cf'], business_fail: ['#f6a4b8', '#ee5a7c'],
  http_error: ['#f28aa3', '#e04a6e'], network_error: ['#ffd88a', '#f5a524'], timeout: ['#ffe3a8', '#f7b43c'],
  ssl_error: ['#54506c', '#282441'] }

export default {
  components: { Ring },
  setup() {
    const view = ref(null), form = reactive({}), saving = ref(false)
    const logs = ref(null), q = reactive({ date: '', result: '', kw: '' }), loadingLogs = ref(true)
    const re = reactive({ scenics: '', tables: 'drill,macro', start: todayYmd(), end: todayYmd() })
    const detail = ref(null)
    const loadView = async () => {
      try { view.value = await api('/api/push/settings'); Object.assign(form, view.value.settings)
        for (const k of ['push_batch_size', 'push_timeout', 'push_retries']) if (form[k] === null) form[k] = '' }
      catch (e) { notify(e.message, 'err') }
    }
    const loadLogs = async () => {
      loadingLogs.value = true
      try { logs.value = await api('/api/push/logs?' + new URLSearchParams({ date: q.date, result: q.result, q: q.kw }))
        if (!q.date) q.date = logs.value.date }
      catch (e) { notify(e.message, 'err') } finally { loadingLogs.value = false }
    }
    onMounted(() => { loadView(); loadLogs() })
    const save = async (patch) => {
      saving.value = true
      try { view.value = await api('/api/push/settings', { method: 'PUT', body: { ...form, ...(patch || {}) } })
        Object.assign(form, view.value.settings)
        for (const k of ['push_batch_size', 'push_timeout', 'push_retries']) if (form[k] === null) form[k] = ''
        notify('推送设置已保存，下一次执行生效'); refreshSched() }
      catch (e) { notify(e.message, 'err') } finally { saving.value = false }
    }
    const setMaster = v => save({ enabled: v })
    const toggleRe = k => {
      const s = new Set(re.tables.split(',').filter(Boolean))
      s.has(k) ? s.delete(k) : s.add(k)
      re.tables = TABLES.map(t => t.key).filter(x => s.has(x)).join(',')
    }
    const doRepush = async () => {
      try { const r = await api('/api/maint/push', { method: 'POST', body: re }); notify(`已提交补推 #${r.run_id}`)
        location.hash = '/runs/' + r.run_id } catch (e) { notify(e.message, 'err') }
    }
    const reset = () => { q.result = ''; q.kw = ''; loadLogs() }
    const parts = computed(() => Object.entries(logs.value?.counts || {}).map(([k, v]) => ({
      value: v, label: (PUSH_RESULT[k] || {}).text || k, colors: RING_C[k] || ['#8682a0', '#6b7075'] })))
    const total = computed(() => Object.values(logs.value?.counts || {}).reduce((a, v) => a + v, 0))
    const okRate = computed(() => total.value ? Math.round(((logs.value.counts.ok || 0) + (logs.value.counts.dry_run || 0)) / total.value * 1000) / 10 : null)
    const cols = [
      { key: 'time', title: '时间', width: 100 }, { key: 'table', title: '表', width: 250 },
      { key: 'scenic', title: '景区', width: 150 }, { key: 'batch', title: '批次', width: 90 },
      { key: 'rows', title: '行数', width: 80, align: 'r' }, { key: 'result', title: '结果', width: 120 },
      { key: 'resp', title: '响应', width: 260 }, { key: 'op', title: '', width: 56, align: 'r' }]
    const ymd = s => s ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}` : ''
    return { view, form, saving, save, setMaster, logs, q, loadLogs, loadingLogs, reset, re, toggleRe, doRepush,
             parts, total, okRate, cols, detail, ymd, TABLES, TABLE_LABEL, PUSH_RESULT, fmtNum, todayYmd }
  },
  template: `
  <div v-if="view">
    <div class="card banner">
      <Switch :model-value="form.enabled" @change="setMaster"/>
      <div class="grow"><b>启用推送</b><Tip wide :text="'任务算完、写库后，按任务里勾的表推给下游。关掉后所有任务只算不推（「补推」照常能用）。推送失败不影响跑批成败。推送记录写在 ' + (view.log_dir || '（未开启记录）')"/></div>
      <Tag :color="form.enabled ? 'green' : 'grey'" :icon="form.enabled ? 'tickCircle' : 'minus'">{{ form.enabled ? '已启用' : '已关闭' }}</Tag>
    </div>

    <div class="grid two">
      <div class="card sect">
        <div class="sect-head"><h3>推送设置<Tip text="留空 = 用 settings_local.py 里的配置；保存后下一次执行生效"/></h3></div>
        <div class="sect-body">
          <div class="frow"><label>推送地址</label>
            <input v-model="form.push_url" class="grow" :placeholder="view.base.push_url || '未配置'" /></div>
          <div class="frow"><label>每批行数</label><input v-model="form.push_batch_size" class="w100" :placeholder="String(view.base.push_batch_size)"/>
            <label class="l2">超时</label><input v-model="form.push_timeout" class="w100" :placeholder="view.base.push_timeout + ' 秒'"/>
            <label class="l2">重试</label><input v-model="form.push_retries" class="w100" :placeholder="view.base.push_retries + ' 次'"/></div>
          <div class="opts compact">
            <div class="opt"><div><b>试推</b><Tip text="只组报文不真发，验证配置用（推送记录里结果是「试推」）"/></div><Switch v-model="form.push_dry_run"/></div>
            <div class="opt"><div><b>推送失败算执行失败</b><Tip text="默认推失败不影响跑批成败（数据已落库，可以补推）"/></div><Switch v-model="form.push_strict"/></div>
          </div>
          <div class="frow ro"><label>请求头</label><div class="chips">
            <span v-for="(v,k) in view.headers" :key="k" class="chip grey">{{ k }}: {{ v }}</span>
            <span v-if="!Object.keys(view.headers).length" class="hint">未配置<Tip text="在 settings_local.py 的 push_headers 或环境变量 OPINION_PUSH_TOKEN 里配，页面不显示明文"/></span></div></div>
          <div class="frow ro" v-if="Object.keys(view.table_names).length"><label>表名映射</label><div class="chips">
            <span v-for="(v,k) in view.table_names" :key="k" class="chip grey" :title="k">{{ TABLE_LABEL[k] || k }} → {{ v }}</span></div></div>
          <div class="actions"><span class="hint">生效地址<Tip :text="view.effective.push_url || '未配置'"/></span>
            <button class="btn primary" :disabled="saving" @click="save()">{{ saving ? '保存中…' : '保存推送设置' }}</button></div>
        </div>
      </div>

      <div class="col-stack">
        <div class="card sect push-sum">
          <div class="sect-head"><h3>推送情况<small>{{ ymd(logs && logs.date) || '暂无记录' }}</small></h3></div>
          <div class="push-sum-body">
            <Ring :parts="parts" :size="190" :thickness="18" :gap="10" :hole="0.56" :icons="false"
              :value="okRate === null ? '—' : okRate + '%'" sub="请求成功率" />
            <div class="legend-list">
              <div v-for="(v,k) in (logs ? logs.counts : {})" :key="k"><Tag :color="(PUSH_RESULT[k]||{}).color || 'grey'"
                :icon="(PUSH_RESULT[k]||{}).icon">{{ (PUSH_RESULT[k]||{}).text || k }}</Tag><b>{{ v }}</b></div>
              <div class="muted small">成功推出 <b>{{ fmtNum(logs ? logs.rows_ok : 0) }}</b> 行 · 共 {{ total }} 个请求</div>
            </div>
          </div>
        </div>
        <div class="card sect">
          <div class="sect-head"><h3>补推<Tip text="不重算，从库里读出这几天算好的结果再推一遍"/></h3></div>
          <div class="sect-body">
            <div class="frow"><label>景区</label><ScenicPicker v-model="re.scenics" placeholder="全部景区" /></div>
            <div class="frow"><label>表</label><div class="chips">
              <button v-for="t in TABLES" :key="t.key" class="chip-btn" :class="{on: re.tables.split(',').includes(t.key)}"
                @click="toggleRe(t.key)">{{ t.label }}</button></div></div>
            <div class="frow"><label>日期</label><DateRange v-model:start="re.start" v-model:end="re.end" :clearable="false" :max="todayYmd()" />
              <button class="btn primary" @click="doRepush"><Icon name="send" :size="14"/>开始补推</button></div>
          </div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head stick"><h3>推送记录<Tip text="每个请求一行：推了哪个表、哪个景区、对方怎么回的；点右边的眼睛看完整响应"/></h3>
        <div class="toolbar">
          <DatePicker v-model="q.date" :clearable="false" placeholder="推送日期" @change="loadLogs" />
          <select v-model="q.result" @change="loadLogs"><option value="">全部结果</option>
            <option v-for="(v,k) in PUSH_RESULT" :key="k" :value="k">{{ v.text }}</option></select>
          <span class="search"><Icon name="search" :size="15"/><input v-model="q.kw" placeholder="景区 / 表 / 响应内容" @keyup.enter="loadLogs"/></span>
          <button class="btn light" @click="reset">重置</button></div></div>
      <STable :columns="cols" :data="logs ? logs.items : []" :loading="loadingLogs" :min-height="350" row-key="__k">
        <template #time="{ row }">{{ (row.time || '').slice(11) }}</template>
        <template #table="{ row }"><span class="s-name"><Avatar square icon="db" :color="row.table && row.table.includes('macro') ? 'indigo' : row.table && row.table.includes('drill') ? 'violet' : 'light-blue'"/>
          <span class="s-ell" :title="row.push_table || row.table"><b>{{ TABLE_LABEL[row.table] || row.table }}</b><small>{{ row.push_table || row.table }}</small></span></span></template>
        <template #scenic="{ row }"><span class="s-ell" :title="(row.scenic||[]).join(',') + ' ' + (row.travel_dates||[]).join(',')">
          {{ (row.scenic || []).join(',') || '-' }}<small>{{ (row.travel_dates || []).length ? row.travel_dates[0] + ((row.travel_dates.length > 1) ? ' 等 ' + row.travel_dates.length + ' 天' : '') : '' }}</small></span></template>
        <template #batch="{ row }">{{ row.batch }}/{{ row.batches }}<small v-if="row.attempt > 1" class="t-orange"> 第{{ row.attempt }}次</small></template>
        <template #rows="{ row }">{{ fmtNum(row.rows) }}</template>
        <template #result="{ row }"><Tag :color="(PUSH_RESULT[row.result]||{}).color || 'grey'" :icon="(PUSH_RESULT[row.result]||{}).icon">
          {{ (PUSH_RESULT[row.result]||{}).text || row.result }}</Tag><small v-if="row.status" class="muted"> {{ row.status }}</small></template>
        <template #resp="{ row }"><span class="s-ell mono" :title="row.response || row.error">{{ row.response || row.error || '-' }}</span></template>
        <template #op="{ row }"><button class="s-btn-icon" title="查看完整响应" @click="detail = row"><Icon name="eye" :size="16"/></button></template>
      </STable>
    </div>

    <div v-if="detail" class="modal-mask" @click.self="detail=null">
      <div class="modal">
        <div class="card-head"><h3>推送记录 · {{ detail.time }}</h3><button class="s-btn-icon" @click="detail=null"><Icon name="clear" :size="18"/></button></div>
        <pre class="log light">{{ JSON.stringify(detail, null, 2) }}</pre>
      </div>
    </div>
  </div>`,
}
