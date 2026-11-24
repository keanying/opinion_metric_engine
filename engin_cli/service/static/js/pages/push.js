import { ref, reactive, onMounted, computed } from 'vue'
import { api, TABLES, TABLE_LABEL, PUSH_RESULT, fmtNum, notify, refreshSched, todayYmd, confirmBox } from '../api.js'
import { Ring } from '../charts.js'

const RING_C = { ok: ['#a3a6f3', '#5458d8'], dry_run: ['#8be3ef', '#22b8cf'], business_fail: ['#f6a4b8', '#ee5a7c'],
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
    // 推送记录：固定高度的窗口，先拿 PAGE 条，滚到底（或点「加载更多」）再接着拿下一批
    const PAGE = 50, moreLogs = ref(false)
    const logQuery = offset => '/api/push/logs?' + new URLSearchParams({ date: q.date, result: q.result, q: q.kw, offset, limit: PAGE })
    const loadLogs = async () => {
      loadingLogs.value = true
      try { logs.value = await api(logQuery(0))
        if (!q.date) q.date = logs.value.date }
      catch (e) { notify(e.message, 'err') } finally { loadingLogs.value = false }
    }
    const loadMoreLogs = async () => {
      const cur = logs.value
      if (!cur || moreLogs.value || loadingLogs.value || cur.items.length >= cur.total) return
      moreLogs.value = true
      try { const r = await api(logQuery(cur.items.length)); logs.value = { ...r, items: cur.items.concat(r.items) } }
      catch (e) { notify(e.message, 'err') } finally { moreLogs.value = false }
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

    // ── 写入数据源：推送方式之二，写进另一个 MySQL 库（先删本次景区 × 日期，再写入）
    const dsEdit = ref(null), dsSaving = ref(false), dsTest = ref(null), dsTesting = ref('')
    const curDs = computed(() => (view.value?.datasources || []).find(d => d.id === form.target))
    const blankDs = () => ({ id: '', name: '', host: '', port: 3306, user: '', password: '', database: '', remark: '',
                             table_names: Object.fromEntries(TABLES.map(t => [t.table, ''])), has_password: false })
    const openDs = d => {
      dsTest.value = null
      dsEdit.value = d ? { ...blankDs(), ...d, password: '',
                           table_names: Object.fromEntries(TABLES.map(t => [t.table, (d.table_names || {})[t.table] || ''])) }
                       : blankDs()
    }
    const closeDs = () => { dsEdit.value = null; dsTest.value = null }      // 编辑框里测的结果跟着关掉，别再弹出来
    const dsBody = () => { const e = dsEdit.value
      return { id: e.id || undefined, name: e.name, host: e.host, port: e.port, user: e.user, password: e.password,
               database: e.database, remark: e.remark, table_names: e.table_names } }
    const saveDs = async () => {
      dsSaving.value = true
      try {
        const e = dsEdit.value
        await api(e.id ? '/api/datasources/' + e.id : '/api/datasources', { method: e.id ? 'PUT' : 'POST', body: dsBody() })
        notify('数据源已保存'); closeDs(); await loadView()
      } catch (err) { notify(err.message, 'err') } finally { dsSaving.value = false }
    }
    const testDs = async (d) => {
      dsTesting.value = d ? d.id : 'form'; dsTest.value = null
      try { dsTest.value = { name: d ? d.name : (dsEdit.value.name || '未保存的数据源'),
                             ...(await api('/api/datasources/test', { method: 'POST', body: d ? { id: d.id } : dsBody() })) } }
      catch (err) { notify(err.message, 'err') } finally { dsTesting.value = '' }
    }
    const delDs = async d => {
      if (!await confirmBox({ type: 'danger', title: '删除数据源', okText: '删除',
        message: '删除后不能恢复；目标库里已经写进去的数据不受影响。', items: [['数据源', d.name], ['地址', `${d.host}:${d.port}/${d.database}`]] })) return
      try { await api('/api/datasources/' + d.id, { method: 'DELETE' }); notify('已删除'); await loadView() }
      catch (err) { notify(err.message, 'err') }
    }
    const setTarget = v => { form.target = v === 'db' ? ((view.value.datasources[0] || {}).id || 'db') : 'api' }
    const mapText = d => { const n = Object.keys(d.table_names || {}).length; return n ? `${n} 张表改了名` : '表名和本地相同' }
    const dsCols = [{ key: 'name', title: '数据源', width: 200 }, { key: 'addr', title: '地址 / 库', width: 260 },
      { key: 'user', title: '用户', width: 120 }, { key: 'map', title: '表名', width: 150 }, { key: 'op', title: '', width: 170, align: 'r' }]
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
    return { view, form, saving, save, setMaster, logs, q, loadLogs, loadMoreLogs, moreLogs, loadingLogs, reset, re, toggleRe, doRepush,
             parts, total, okRate, cols, detail, ymd, TABLES, TABLE_LABEL, PUSH_RESULT, fmtNum, todayYmd,
             dsEdit, dsSaving, dsTest, dsTesting, curDs, openDs, closeDs, saveDs, testDs, delDs, setTarget, mapText, dsCols }
  },
  template: `
  <div v-if="view">
    <div class="card banner">
      <Switch :model-value="form.enabled" @change="setMaster"/>
      <div class="grow"><b>启用推送</b><Tip wide :text="'任务算完、写库后，按任务里勾的表推给下游。关掉后所有任务只算不推（「补推」照常能用）。推送失败不影响跑批成败。推送记录写在 ' + (view.log_dir || '（未开启记录）')"/></div>
      <Tag :color="form.enabled ? 'green' : 'grey'" :icon="form.enabled ? 'tickCircle' : 'minus'">{{ form.enabled ? '已启用' : '已关闭' }}</Tag>
    </div>

    <div class="grid two">
      <div class="card sect push-set">
        <div class="sect-head"><h3>推送设置<Tip text="留空 = 用 settings_local.py 里的配置；保存后下一次执行生效"/></h3></div>
        <div class="sect-body">
          <div class="frow"><label>推送方式<Tip wide text="API：按推送地址 POST 给下游。写入数据源：直接写进另一个 MySQL 库的表——每张表先删除「本次景区 × 本次日期」的旧行，再写入新行（同一事务，失败回滚）。跑批、补推、复制给其他景区都按这里的方式推"/></label>
            <div class="seg"><button :class="{on: form.target === 'api'}" @click="setTarget('api')">API 接口</button>
              <button :class="{on: form.target !== 'api'}" @click="setTarget('db')">写入数据源</button></div></div>
          <div v-if="form.target !== 'api'" class="frow"><label>数据源</label>
            <select v-model="form.target" class="grow">
              <option v-if="!curDs" :value="form.target" disabled>请选择数据源</option>
              <option v-for="d in view.datasources" :key="d.id" :value="d.id">{{ d.name }}（{{ d.host }}:{{ d.port }}/{{ d.database }}）</option></select>
            <button class="btn light" @click="openDs()"><Icon name="plus" :size="13" :sw="2.4"/>添加</button></div>
          <div v-if="form.target !== 'api' && !view.datasources.length" class="alert orange small">还没有数据源：点「添加」填数据库地址、账号、库名</div>
          <div v-if="view.target_missing && form.target === view.settings.target" class="alert orange small">之前选的数据源已经被删掉了，请重新选择</div>
          <div v-if="form.target === 'api'" class="frow"><label>推送地址</label>
            <input v-model="form.push_url" class="grow" :placeholder="view.base.push_url || '未配置'" /></div>
          <div class="frow"><label>每批行数</label><input v-model="form.push_batch_size" class="w100" :placeholder="String(view.base.push_batch_size)"/>
            <template v-if="form.target === 'api'">
            <label class="l2">超时</label><input v-model="form.push_timeout" class="w100" :placeholder="view.base.push_timeout + ' 秒'"/>
            <label class="l2">重试</label><input v-model="form.push_retries" class="w100" :placeholder="view.base.push_retries + ' 次'"/></template></div>
          <div class="opts compact">
            <div class="opt"><div><b>试推</b><Tip text="只组报文不真发，验证配置用（推送记录里结果是「试推」）"/></div><Switch v-model="form.push_dry_run"/></div>
            <div class="opt"><div><b>推送失败算执行失败</b><Tip text="默认推失败不影响跑批成败（数据已落库，可以补推）"/></div><Switch v-model="form.push_strict"/></div>
          </div>
          <template v-if="form.target === 'api'">
          <div class="frow ro"><label>请求头</label><div class="chips">
            <span v-for="(v,k) in view.headers" :key="k" class="chip grey">{{ k }}: {{ v }}</span>
            <span v-if="!Object.keys(view.headers).length" class="hint">未配置<Tip text="在 settings_local.py 的 push_headers 或环境变量 OPINION_PUSH_TOKEN 里配，页面不显示明文"/></span></div></div>
          <div class="frow ro" v-if="Object.keys(view.table_names).length"><label>表名映射</label><div class="chips">
            <span v-for="(v,k) in view.table_names" :key="k" class="chip grey" :title="k">{{ TABLE_LABEL[k] || k }} → {{ v }}</span></div></div>
          <div class="frow ro"><label>生效地址<Tip text="这次执行实际推到哪：页面上填了用页面的，没填用 settings_local.py 的"/></label>
            <span class="mono ell grow" :title="view.effective.push_url">{{ view.effective.push_url || '未配置' }}</span></div>
          </template>
          <div class="actions"><span class="hint">保存后下一次执行生效</span>
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
            <div class="frow"><label>景区</label><ScenicPicker v-model="re.scenics" single placeholder="全部景区" /></div>
            <div class="frow"><label>表</label><div class="chips">
              <button v-for="t in TABLES" :key="t.key" class="chip-btn" :class="{on: re.tables.split(',').includes(t.key)}"
                @click="toggleRe(t.key)">{{ t.label }}</button></div></div>
            <div class="frow"><label>日期</label><DateRange v-model:start="re.start" v-model:end="re.end" :clearable="false" :max="todayYmd()" />
              <button class="btn primary" @click="doRepush"><Icon name="send" :size="14"/>开始补推</button></div>
          </div>
        </div>
      </div>
    </div>

    <div class="card sect">
      <div class="sect-head"><h3>写入数据源<Tip wide text="推送方式选「写入数据源」时写进这里的库。每张表：先删除本次景区 × 本次日期的旧行，再写入（同一事务）；只写目标表里有的列，目标表名可以和本地不同。只有管理员能添加 / 修改；密码保存在系统表里，页面上不显示"/></h3>
        <button class="btn xs light" @click="openDs()"><Icon name="plus" :size="12" :sw="2.4"/>添加数据源</button></div>
      <STable v-if="view.datasources.length" :columns="dsCols" :data="view.datasources" row-key="id">
        <template #name="{ row }"><span class="s-name"><Avatar square icon="db" :color="row.id === view.settings.target ? 'green' : 'light-blue'"/>
          <span class="s-ell"><b>{{ row.name }}</b><small>{{ row.id === view.settings.target ? '当前推送方式' : (row.remark || '') }}</small></span></span></template>
        <template #addr="{ row }"><span class="mono s-ell">{{ row.host }}:{{ row.port }}/{{ row.database }}</span></template>
        <template #user="{ row }">{{ row.user }}<small v-if="!row.has_password" class="t-orange"> 无密码</small></template>
        <template #map="{ row }">{{ mapText(row) }}</template>
        <template #op="{ row }"><span class="op-btns">
          <button class="btn xs" :disabled="dsTesting === row.id" @click="testDs(row)">{{ dsTesting === row.id ? '测试中…' : '测试' }}</button>
          <button class="s-btn-icon" title="修改" @click="openDs(row)"><Icon name="edit" :size="16"/></button>
          <button class="s-btn-icon danger" title="删除" @click="delDs(row)"><Icon name="del" :size="16"/></button></span></template>
      </STable>
      <p v-else class="hint ds-empty">还没有数据源：点右上角「添加数据源」，填数据库地址、账号、库名；保存后在上面「推送方式」里选它</p>
    </div>

    <Transition name="dlg">
      <div v-if="dsEdit" class="modal-mask" @click.self="closeDs">
        <div class="modal">
          <div class="card-head"><h3>{{ dsEdit.id ? '修改数据源' : '添加数据源' }}</h3><button class="s-btn-icon" @click="closeDs"><Icon name="clear" :size="18"/></button></div>
          <div class="form fs-form ds-form">
            <div class="frow"><label>名称</label><input v-model="dsEdit.name" placeholder="如 客户数仓" /></div>
            <div class="frow"><label>地址</label><input v-model="dsEdit.host" placeholder="如 192.168.1.20" />
              <label class="l2">端口</label><input v-model.number="dsEdit.port" class="w100" /></div>
            <div class="frow"><label>用户名</label><input v-model="dsEdit.user" autocomplete="off" />
              <label class="l2">密码</label><input v-model="dsEdit.password" type="password" autocomplete="new-password"
                :placeholder="dsEdit.has_password ? '已设置（留空不改）' : ''" /></div>
            <div class="frow"><label>库名</label><input v-model="dsEdit.database" placeholder="要写入的数据库" /></div>
            <div class="frow"><label>备注</label><input v-model="dsEdit.remark" placeholder="选填" /></div>
            <div class="frow ds-map-head"><label>表名对应</label><span class="hint">目标库里的表名，留空 = 和本地同名</span></div>
            <div v-for="t in TABLES" :key="t.key" class="frow ds-map"><label :title="t.table">{{ t.label }}</label>
              <input v-model="dsEdit.table_names[t.table]" :placeholder="t.table" /></div>
          </div>
          <div class="dlg-btns">
            <button class="btn light" :disabled="dsTesting === 'form'" @click="testDs()">{{ dsTesting === 'form' ? '测试中…' : '测试连接' }}</button>
            <span class="grow"></span>
            <button class="btn light" @click="closeDs">取消</button>
            <button class="btn primary" :disabled="dsSaving" @click="saveDs">保存</button>
          </div>
          <div v-if="dsTest" class="ds-test">
            <div v-if="dsTest.error" class="alert red small">{{ dsTest.error }}</div>
            <div v-for="t in dsTest.tables" :key="t.table" class="ds-test-row">
              <Tag :color="!t.exists ? 'pink' : !t.writable ? 'pink' : t.missing.length ? 'orange' : 'green'"
                :icon="t.exists && t.writable ? 'tickCircle' : 'clear'">{{ TABLE_LABEL[t.table] }}</Tag>
              <span class="mono">{{ t.target }}</span><span class="muted">{{ t.message }}</span>
              <small v-if="t.missing.length" class="muted" :title="t.missing.join(', ')">缺：{{ t.missing.slice(0, 4).join(', ') }}{{ t.missing.length > 4 ? ' …' : '' }}</small></div>
          </div>
        </div>
      </div></Transition>

    <Transition name="dlg">
      <div v-if="dsTest && !dsEdit" class="modal-mask" @click.self="dsTest=null">
        <div class="modal">
          <div class="card-head"><h3>测试连接 · {{ dsTest.name }}</h3><button class="s-btn-icon" @click="dsTest=null"><Icon name="clear" :size="18"/></button></div>
          <div class="ds-test">
            <div v-if="dsTest.error" class="alert red small">{{ dsTest.error }}</div>
            <div v-else class="alert green small">连接成功</div>
            <div v-for="t in dsTest.tables" :key="t.table" class="ds-test-row">
              <Tag :color="!t.exists ? 'pink' : !t.writable ? 'pink' : t.missing.length ? 'orange' : 'green'"
                :icon="t.exists && t.writable ? 'tickCircle' : 'clear'">{{ TABLE_LABEL[t.table] }}</Tag>
              <span class="mono">{{ t.target }}</span><span class="muted">{{ t.message }}</span>
              <small v-if="t.missing.length" class="muted" :title="t.missing.join(', ')">缺：{{ t.missing.slice(0, 4).join(', ') }}{{ t.missing.length > 4 ? ' …' : '' }}</small></div>
          </div>
        </div>
      </div></Transition>

    <div class="card fill push-logs">
      <div class="card-head"><h3>推送记录<Tip text="每个请求一行：推了哪个表、哪个景区、对方怎么回的；点右边的眼睛看完整响应"/></h3>
        <div class="toolbar">
          <DatePicker v-model="q.date" :clearable="false" placeholder="推送日期" @change="loadLogs" />
          <select v-model="q.result" @change="loadLogs"><option value="">全部结果</option>
            <option v-for="(v,k) in PUSH_RESULT" :key="k" :value="k">{{ v.text }}</option></select>
          <span class="search"><Icon name="search" :size="15"/><input v-model="q.kw" placeholder="景区 / 表 / 响应内容" @keyup.enter="loadLogs"/></span>
          <button class="btn light" @click="reset">重置</button></div></div>
      <STable :columns="cols" :data="logs ? logs.items : []" :loading="loadingLogs" row-key="__k" scroll @reach-end="loadMoreLogs">
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
        <template #foot><div v-if="logs && logs.items.length" class="s-table-foot">
          <template v-if="moreLogs">加载中…</template>
          <template v-else-if="logs.items.length < logs.total">已显示 {{ logs.items.length }} / 共 {{ logs.total }} 条，往下滚动加载更多
            <a class="link" @click.stop="loadMoreLogs">加载更多</a></template>
          <template v-else>已全部显示（共 {{ logs.total }} 条）</template></div></template>
      </STable>
    </div>

    <Transition name="dlg">
      <div v-if="detail" class="modal-mask" @click.self="detail=null">
        <div class="modal">
          <div class="card-head"><h3>推送记录 · {{ detail.time }}</h3><button class="s-btn-icon" @click="detail=null"><Icon name="clear" :size="18"/></button></div>
          <pre class="log light">{{ JSON.stringify(detail, null, 2) }}</pre>
        </div>
      </div></Transition>
  </div>`,
}
