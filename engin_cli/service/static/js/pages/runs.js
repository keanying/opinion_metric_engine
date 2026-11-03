import { ref, reactive, onMounted, onUnmounted, watch, nextTick, computed } from 'vue'
import { api, KIND, TRIGGER, TABLE_LABEL, duration, fmtNum, notify, confirmBox } from '../api.js'
import { STATUS_TAG } from '../ui.js'

const KIND_AV = { run: ['layers', 'indigo'], mock: ['file', 'violet'], push: ['send', 'cyan'], notify: ['bell', 'amber'] }

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
      if (!await confirmBox({ type: 'warn', title: `取消执行 #${r.id}`, okText: '取消执行', cancelText: '继续执行',
        message: '正在跑的会马上停止，已经写入的数据不会回滚；排队中的直接移出队列。', items: [['任务', r.task_name || '临时执行'], ['说明', r.title]] })) return
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
      <div class="card-head stick"><h3>执行记录<Tip text="每一次跑批 / 补推的状态；点一行看各景区结果和完整日志"/></h3>
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
    const run = ref(null), err = ref(''), log = ref(''), logEl = ref(null), follow = ref(true), wrap = ref(true)
    const open = reactive({}), lq = ref(''), onlyBad = ref(false)
    let offset = 0, timer, stopped = false
    const load = async () => {
      try { run.value = await api('/api/runs/' + p.id); err.value = ''
        // 只有一个景区、或者有失败的景区：默认展开
        const sc = run.value?.summary?.scenics || []
        for (const s of sc) if (sc.length === 1 || !s.ok) open[s.scenic] = true
      } catch (e) { err.value = e.message }
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
      if (!await confirmBox({ type: 'warn', title: `取消执行 #${p.id}`, okText: '取消执行', cancelText: '继续执行',
        message: '正在跑的会马上停止，已经写入的数据不会回滚。' })) return
      try { await api(`/api/runs/${p.id}/cancel`, { method: 'POST', body: {} }); notify('已取消'); load() }
      catch (e) { notify(e.message, 'err') }
    }
    const scenics = computed(() => (run.value?.summary?.scenics) || [])
    const rowsOf = s => Object.entries(s.tables || {}).map(([t, n]) => {
      const pp = (s.push || []).find(x => x.table === t)
      return { table: t, n, load: ((s.loads || []).find(x => x.table === t) || {}).rows, push: pp }
    })
    const totals = s => {
      const rs = rowsOf(s)
      return { tables: rs.length, load: rs.reduce((a, r) => a + (r.load || 0), 0),
               push: rs.reduce((a, r) => a + (r.push && r.push.ok && !r.push.skipped ? (r.push.rows || 0) : 0), 0),
               pushFail: rs.filter(r => r.push && !r.push.ok && !r.push.skipped).length }
    }
    const passed = computed(() => scenics.value.filter(s => s.ok).length)
    const cmd = computed(() => run.value ? 'python -m engin_cli.cli ' + run.value.command.join(' ') : '')
    const copy = async (text, what) => {
      try { await navigator.clipboard.writeText(text); notify(`已复制${what}`) }
      catch (e) {                                   // 非 https 下没有 clipboard：退回老办法
        const t = document.createElement('textarea'); t.value = text; document.body.appendChild(t); t.select()
        document.execCommand('copy'); t.remove(); notify(`已复制${what}`)
      }
    }
    const download = () => {
      const a = document.createElement('a')
      a.href = URL.createObjectURL(new Blob([log.value], { type: 'text/plain;charset=utf-8' }))
      a.download = `run_${p.id}.log`; a.click(); URL.revokeObjectURL(a.href)
    }
    // 日志：按关键字 / 只看警告错误 过滤，关键字高亮
    const esc = t => t.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c])
    const logLines = computed(() => {
      let lines = log.value.split('\n')
      if (onlyBad.value) lines = lines.filter(l => /WARNING|ERROR|Traceback|✗|失败|异常|不通过/.test(l))
      if (lq.value.trim()) lines = lines.filter(l => l.toLowerCase().includes(lq.value.trim().toLowerCase()))
      return lines
    })
    const logHtml = computed(() => {
      const k = lq.value.trim()
      return logLines.value.map(l => {
        let h = esc(l)
        if (/ERROR|Traceback|✗/.test(l)) h = `<span class="lg-err">${h}</span>`
        else if (/WARNING/.test(l)) h = `<span class="lg-warn">${h}</span>`
        if (k) h = h.replace(new RegExp(esc(k).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), m => `<mark>${m}</mark>`)
        return h
      }).join('\n')
    })
    const badCount = computed(() => log.value.split('\n').filter(l => /WARNING|ERROR|Traceback|✗/.test(l)).length)
    return { run, err, log, logEl, follow, wrap, open, lq, onlyBad, cancel, scenics, rowsOf, totals, passed, cmd, copy,
             download, logHtml, logLines, badCount, KIND, TRIGGER, TABLE_LABEL, duration, fmtNum }
  },
  template: `
  <div>
    <div class="detail-head">
      <a class="btn light sm" href="#/runs"><Icon name="arrowL" :size="14"/>执行记录</a>
      <h2>执行 #{{ id }}</h2><StatusTag v-if="run" :status="run.status"/>
      <span v-if="run" class="muted ell1">{{ run.task_name || '临时执行' }} · {{ run.title }}</span>
      <span class="grow"></span>
      <button v-if="run && ['queued','running'].includes(run.status)" class="btn danger-solid" @click="cancel"><Icon name="stop" :size="14"/>取消执行</button>
    </div>
    <div v-if="err" class="alert red">{{ err }}</div>
    <template v-if="run">
      <div class="card run-sum">
        <div class="rs-grid">
          <div><span>类型 · 触发</span><b>{{ KIND[run.kind] || run.kind }} · {{ TRIGGER[run.trigger] }}</b></div>
          <div><span>开始</span><b>{{ run.started_at ? run.started_at.slice(5) : '排队中' }}</b></div>
          <div><span>结束</span><b>{{ run.finished_at ? run.finished_at.slice(5) : (run.started_at ? '执行中…' : '-') }}</b></div>
          <div><span>耗时</span><b>{{ duration(run.started_at, run.finished_at) || '-' }}</b></div>
          <div><span>退出码</span><b :class="{'t-red': run.exit_code}">{{ run.exit_code ?? '-' }}</b></div>
          <div><span>景区</span><b>{{ scenics.length ? passed + ' / ' + scenics.length + ' 通过' : '-' }}</b></div>
        </div>
        <div v-if="run.message" class="alert small" :class="run.status === 'blocked' ? 'orange' : 'red'">{{ run.message }}</div>
        <div class="rs-cmd"><code :title="cmd">{{ cmd }}</code>
          <button class="s-btn-icon" title="复制命令" @click="copy(cmd, '命令')"><Icon name="file" :size="16"/></button></div>
      </div>

      <div v-if="scenics.length" class="card">
        <div class="card-head"><h3>景区结果<small>{{ scenics.length }} 个景区</small></h3></div>
        <div class="s-table-wrap"><table class="s-table sc-table">
          <colgroup><col style="width:260px"/><col style="width:110px"/><col style="width:100px"/><col style="width:120px"/><col style="width:140px"/><col/><col style="width:48px"/></colgroup>
          <thead><tr><th>景区</th><th>结果</th><th>质量检测</th><th class="r">写入</th><th class="r">推送</th><th>问题</th><th></th></tr></thead>
          <tbody>
            <template v-for="s in scenics" :key="s.scenic">
              <tr class="click" @click="open[s.scenic] = !open[s.scenic]">
                <td><span class="s-name"><Avatar :text="s.scenic"/><b>{{ s.scenic }}</b></span></td>
                <td><Tag :color="s.ok ? 'green' : (s.quality_blocked ? 'orange' : 'pink')" :icon="s.ok ? 'tickCircle' : (s.quality_blocked ? 'alert' : 'clear')">
                  {{ s.ok ? '通过' : (s.quality_blocked ? '已回退' : '失败') }}</Tag></td>
                <td>{{ s.quality_attempts > 1 ? '算了 ' + s.quality_attempts + ' 次' : (s.quality_attempts ? '一次通过' : '-') }}</td>
                <td class="r">{{ fmtNum(totals(s).load) }} 行</td>
                <td class="r">{{ fmtNum(totals(s).push) }} 行<span v-if="totals(s).pushFail" class="t-red small"> · {{ totals(s).pushFail }} 张失败</span></td>
                <td><span class="ell1" :class="{'t-red': (s.errors||[]).length || s.exception}" :title="(s.errors || []).join('；') || s.exception">
                  {{ s.exception || (s.errors || [])[0] || '—' }}</span></td>
                <td class="r"><span class="chev" :class="{on: open[s.scenic]}"><Icon name="down" :size="16"/></span></td>
              </tr>
              <tr v-if="open[s.scenic]" class="sc-detail"><td colspan="7">
                <div class="sc-box">
                  <table class="mini-table" v-if="rowsOf(s).length">
                    <thead><tr><th>表</th><th class="r">产出</th><th class="r">写入</th><th class="r">推送</th></tr></thead>
                    <tbody><tr v-for="r in rowsOf(s)" :key="r.table"><td :title="r.table">{{ TABLE_LABEL[r.table] || r.table }}</td>
                      <td class="r">{{ fmtNum(r.n) }}</td><td class="r">{{ fmtNum(r.load) }}</td>
                      <td class="r"><template v-if="r.push"><Tag v-if="r.push.skipped" color="grey">跳过</Tag>
                        <Tag v-else-if="r.push.ok" color="green" icon="tickCircle">{{ fmtNum(r.push.rows) }}</Tag>
                        <Tag v-else color="pink" icon="clear" :title="r.push.error">失败</Tag></template><span v-else class="muted">-</span></td></tr></tbody>
                  </table>
                  <div class="sc-notes">
                    <ul v-if="(s.errors || []).length" class="errs"><li v-for="(e,i) in s.errors" :key="i">{{ e }}</li></ul>
                    <ul v-if="(s.notes || []).length" class="notes"><li v-for="(n,i) in s.notes" :key="i">{{ n }}</li></ul>
                    <span v-if="!(s.errors || []).length && !(s.notes || []).length" class="muted small">没有说明</span>
                  </div>
                </div></td></tr>
            </template>
          </tbody>
        </table></div>
      </div>

      <div class="card dark-card">
        <div class="card-head log-head"><h3>执行日志<small>{{ logLines.length }} 行</small></h3>
          <div class="toolbar">
            <span class="search dark"><Icon name="search" :size="14"/><input v-model="lq" placeholder="搜索日志"/></span>
            <button class="btn sm ghost-dark" :class="{on: onlyBad}" @click="onlyBad = !onlyBad">只看警告 / 错误<em v-if="badCount">{{ badCount }}</em></button>
            <label class="check light"><input type="checkbox" v-model="wrap" />换行</label>
            <label class="check light"><input type="checkbox" v-model="follow" />自动滚动</label>
            <button class="s-btn-icon light" title="复制日志" @click="copy(log, '日志')"><Icon name="file" :size="16"/></button>
            <button class="s-btn-icon light" title="下载日志" @click="download"><Icon name="down" :size="16"/></button>
          </div></div>
        <pre ref="logEl" class="log" :class="{nowrap: !wrap}" v-html="logHtml || (run.status === 'queued' ? '排队中：前面还有执行没跑完，轮到它就开始（同一时间只跑一个）' : '（暂无输出）')"></pre>
      </div>
    </template>
  </div>`,
}
