import { ref, reactive, onMounted, onUnmounted, watch, nextTick, computed } from 'vue'
import { api, KIND, TRIGGER, TABLE_LABEL, duration, fmtNum, notify, confirmBox } from '../api.js'
import { STATUS_TAG } from '../ui.js'

const KIND_AV = { run: ['layers', 'indigo'], mock: ['file', 'violet'], push: ['send', 'cyan'], notify: ['bell', 'amber'] }

export const RunList = {
  setup() {
    // 滚动自动翻页：先拿 20 条，表格滚到底再接着拿下一批，接在后面
    const PAGE = 20
    const f = reactive({ status: '', kind: '' })
    const data = ref({ total: 0, items: [] }), err = ref(''), loading = ref(true), more = ref(false)
    let timer
    const query = (offset, limit) => '/api/runs?' + new URLSearchParams({ status: f.status, kind: f.kind, offset, limit })
    // 刷新：重拿最新的一批（执行中的状态会变、新执行排在最前），跟已经滚动加载的合并，不丢后面已加载的
    const load = async () => {
      try {
        const r = await api(query(0, PAGE)), fresh = new Set(r.items.map(x => x.id))
        data.value = { total: r.total, items: r.items.concat(data.value.items.filter(x => !fresh.has(x.id))) }
        err.value = ''
      } catch (e) { err.value = e.message } finally { loading.value = false }
    }
    const loadMore = async () => {
      if (more.value || loading.value || data.value.items.length >= data.value.total) return
      more.value = true
      try {
        const r = await api(query(data.value.items.length, PAGE))
        const seen = new Set(data.value.items.map(x => x.id))
        data.value = { total: r.total, items: data.value.items.concat(r.items.filter(x => !seen.has(x.id))) }
      } catch (e) { notify(e.message, 'err') } finally { more.value = false }
    }
    watch(() => [f.status, f.kind], () => { loading.value = true; data.value = { total: 0, items: [] }; load() })
    onMounted(() => { load(); timer = setInterval(() => {
      if (data.value.items.some(r => ['queued', 'running'].includes(r.status))) load() }, 3000) })
    onUnmounted(() => clearInterval(timer))
    const reset = () => { f.status = ''; f.kind = '' }
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
    return { f, data, err, loading, more, loadMore, reset, cancel, cols, STATUS_TAG, KIND, KIND_AV, TRIGGER, duration }
  },
  template: `
  <div class="page-fill">
    <div class="card fill">
      <div class="card-head"><h3>执行记录<Tip text="每一次跑批 / 补推的状态；点一行看各景区结果和完整日志"/></h3>
        <div class="toolbar">
          <div class="seg"><button v-for="s in ['', 'running', 'success', 'blocked', 'failed', 'cancelled']" :key="s"
            :class="{on: f.status===s}" @click="f.status=s">{{ s ? STATUS_TAG[s].text : '全部' }}</button></div>
          <select v-model="f.kind"><option value="">全部类型</option>
            <option v-for="(v,k) in KIND" :key="k" :value="k">{{ v }}</option></select>
          <button class="btn light" @click="reset">重置</button></div></div>
      <div v-if="err" class="alert red">{{ err }}</div>
      <STable :columns="cols" :data="data.items" :loading="loading" clickable scroll
        @row-click="r => $router.push('/runs/'+r.id)" @reach-end="loadMore">
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
        <template #foot><div v-if="data.items.length" class="s-table-foot">
          <template v-if="more">加载中…</template>
          <template v-else-if="data.items.length < data.total">已显示 {{ data.items.length }} / 共 {{ data.total }} 条，往下滚动加载更多
            <a class="link" @click.stop="loadMore">加载更多</a></template>
          <template v-else>已全部显示（共 {{ data.total }} 条）</template></div></template>
      </STable>
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
    // 处理说明按类别分组；每张表的推送结果上面卡片里已经有了，这里不重复
    const NOTE_GROUPS = [
      ['数据量', /^(评论明细|大盘 KPI|下钻明细|源表|取数|读取)/],
      ['缺数回补', /回补|补零|补数|复制往前|完整性|补齐/],
      ['过滤', /过滤|关键字|掩码|snippet|脱敏/],
      ['质量检测', /质量|检测|重算/],
      ['推送', /推送|pkId|下游/],
    ]
    const noteGroups = s => {
      const out = NOTE_GROUPS.map(([label]) => ({ label, items: [] })).concat([{ label: '其他', items: [] }])
      for (const n of s.notes || []) {
        if (/^推送 ads_\w+：/.test(n)) continue
        // 先看更具体的类别（过滤 / 回补 / 质量 / 推送），都不是再算「数据量」
        const hit = [2, 1, 3, 4, 0].find(i => NOTE_GROUPS[i][1].test(n))
        out[hit === undefined ? out.length - 1 : hit].items.push(n)
      }
      return out.filter(g => g.items.length)
    }
    return { run, err, log, logEl, follow, wrap, open, lq, onlyBad, cancel, scenics, rowsOf, totals, passed, cmd, copy,
             download, logHtml, logLines, badCount, KIND, TRIGGER, TABLE_LABEL, duration, fmtNum, noteGroups }
  },
  template: `
  <div>
    <div class="detail-head card">
      <a class="dh-back" href="#/runs" title="返回执行记录" aria-label="返回执行记录"><Icon name="arrowL" :size="18" :sw="2.2"/></a>
      <div class="dh-title">
        <div class="dh-crumb"><a href="#/runs">执行记录</a><span>/</span>详情</div>
        <h2>执行 #{{ id }}<StatusTag v-if="run" :status="run.status"/></h2>
        <span v-if="run" class="muted ell1" :title="(run.task_name || '临时执行') + ' · ' + run.title">{{ run.task_name || '临时执行' }} · {{ run.title }}</span>
      </div>
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
                <div class="sc-box2">
                  <ul v-if="(s.errors || []).length || s.exception" class="errs">
                    <li v-if="s.exception">{{ s.exception }}</li><li v-for="(e,i) in s.errors" :key="i">{{ e }}</li></ul>
                  <div v-if="rowsOf(s).length" class="tb-cards">
                    <div v-for="r in rowsOf(s)" :key="r.table" class="tb-card" :title="r.table">
                      <b>{{ TABLE_LABEL[r.table] || r.table }}</b>
                      <dl><dt>算出</dt><dd>{{ fmtNum(r.n) }}</dd>
                        <dt>写入库</dt><dd>{{ fmtNum(r.load) }}</dd>
                        <dt>推给下游</dt>
                        <dd v-if="!r.push" class="muted">不推送</dd>
                        <dd v-else-if="r.push.skipped" class="muted">跳过</dd>
                        <dd v-else-if="r.push.ok" class="t-green"><Icon name="tickCircle" :size="13" :sw="2.2"/>{{ fmtNum(r.push.rows) }}</dd>
                        <dd v-else class="t-red" :title="r.push.error"><Icon name="clear" :size="13" :sw="2.2"/>失败</dd></dl>
                    </div>
                  </div>
                  <div v-if="noteGroups(s).length" class="note-groups">
                    <div v-for="g in noteGroups(s)" :key="g.label" class="note-g">
                      <span class="ng-lab">{{ g.label }}</span>
                      <ul><li v-for="(n,i) in g.items" :key="i">{{ n }}</li></ul></div>
                  </div>
                  <span v-else-if="!rowsOf(s).length" class="muted small">没有说明</span>
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
