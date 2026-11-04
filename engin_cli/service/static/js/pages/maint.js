import { ref, reactive, onMounted, computed } from 'vue'
import { api, todayYmd, notify, confirmBox, TABLES, TABLE_LABEL } from '../api.js'
import { TaskBody, blankConfig } from './tasks.js'

export default {
  components: { TaskBody },
  setup() {
    const cfg = reactive({ ...blankConfig(), date_mode: 'range', start_date: todayYmd(-7), end_date: todayYmd() })
    const cleanDays = ref(90), config = ref(null), pushOff = ref(false)
    // 重跑：选一个已有任务 + 一段日期；选「自定义配置」用下面的设置
    const tasks = ref([]), rr = reactive({ task: '', start: todayYmd(-7), end: todayYmd() })
    const fs = ref(null), fsEdit = ref(null), fsSaving = ref(false)
    const loadConfig = async () => { try { config.value = await api('/api/config') } catch (e) { notify(e.message, 'err') } }
    const loadFs = async () => { try { fs.value = await api('/api/notify/settings') } catch (e) { /* 忽略 */ } }
    onMounted(async () => {
      loadConfig(); loadFs()
      try { pushOff.value = !(await api('/api/push/settings')).settings.enabled } catch (e) { /* 忽略 */ }
      try { tasks.value = await api('/api/tasks'); if (tasks.value.length) rr.task = tasks.value[0].id } catch (e) { /* 忽略 */ }
    })
    const curTask = computed(() => tasks.value.find(t => t.id === rr.task))
    const ALL = TABLES.map(t => t.key)
    const tabText = v => { const k = String(v || '').split(',').filter(Boolean)
      return !k.length || k.length === ALL.length ? '全部表' : k.map(x => TABLE_LABEL[x] || x).join('、') }
    const taskText = t => {
      const c = t.config || {}
      return `${c.scenics || '全部景区'} · 计算 ${tabText(c.tables)} · 推送 ${c.push ? tabText(c.push) : '不推送'}` +
        ((c.copies || []).length ? ` · 跑完复制 ${c.copies.length} 个` : '')
    }
    const go = id => { location.hash = '/runs/' + id }
    const doRun = async () => {
      const body = rr.task ? { task_id: rr.task, start: rr.start, end: rr.end } : { config: cfg }
      try { const r = await api('/api/maint/run', { method: 'POST', body }); notify(`已提交执行 #${r.run_id}`); go(r.run_id) }
      catch (e) { notify(e.message, 'err') }
    }
    // 通知设置（飞书机器人）：地址里带令牌，页面上只显示打码后的；输入框留空 = 不改
    const openFs = () => { fsEdit.value = { webhook: '', secret: '', keepSecret: !!(fs.value && fs.value.page_set) } }
    const saveFs = async () => {
      fsSaving.value = true
      try {
        const e = fsEdit.value, body = { webhook: e.webhook.trim() }
        if (!e.keepSecret || e.secret) body.secret = e.secret.trim()
        fs.value = await api('/api/notify/settings', { method: 'PUT', body }); fsEdit.value = null
        notify('通知设置已保存，下一次执行生效'); loadConfig()
      } catch (e) { notify(e.message, 'err') } finally { fsSaving.value = false }
    }
    const clearFs = async () => {
      if (!await confirmBox({ title: '清除页面上的通知设置', okText: '清除',
        message: '清除后改用配置文件里的通知地址（没有就不发通知）。' })) return
      try { fs.value = await api('/api/notify/settings', { method: 'PUT', body: { clear: true } }); fsEdit.value = null
        notify('已清除'); loadConfig() } catch (e) { notify(e.message, 'err') }
    }
    const FS_SRC = { page: '服务页面设置', local: '配置文件', none: '未配置' }
    const doNotify = async () => {
      try { const r = await api('/api/maint/notify-test', { method: 'POST', body: {} }); notify(`已提交 #${r.run_id}`); go(r.run_id) }
      catch (e) { notify(e.message, 'err') }
    }
    const doClean = async () => {
      if (!await confirmBox({ type: 'danger', title: '清理执行记录', okText: '确认清理',
        message: `删除 ${cleanDays.value} 天以前的执行记录、日志文件和调度日志，删除后不能恢复。` })) return
      try { const r = await api('/api/maint/cleanup', { method: 'POST', body: { days: cleanDays.value } })
        notify(`已删除 ${r.deleted_runs} 条记录、${r.deleted_files} 个文件`) } catch (e) { notify(e.message, 'err') }
    }
    const show = v => typeof v === 'object' && v !== null ? JSON.stringify(v) : (v === '' || v === null ? '（空）' : String(v))
    return { cfg, cleanDays, config, pushOff, doRun, doNotify, doClean, show, tasks, rr, curTask, taskText, todayYmd,
             fs, fsEdit, fsSaving, openFs, saveFs, clearFs, FS_SRC }
  },
  template: `
  <div>
    <div class="grid three-tools">
      <div class="card tool">
        <span class="icon-sq grad"><Icon name="refresh" :size="18" :sw="2.2"/></span>
        <div class="tool-main"><h3>重跑一段日期<Tip wide text="选一个已有任务，按它的景区、计算的表、推送、数据质量设置（包括跑完后复制）重跑这段日期，不改任务本身；选「自定义配置」就用下面的设置临时跑一次"/></h3>
          <div class="tool-form">
            <select v-model="rr.task"><option v-for="t in tasks" :key="t.id" :value="t.id">{{ t.name }}{{ t.enabled ? '' : '（已停用）' }}</option>
              <option value="">自定义配置（在下面设置）</option></select>
            <DateRange v-if="rr.task" v-model:start="rr.start" v-model:end="rr.end" :clearable="false" :max="todayYmd()" />
          </div>
          <p class="s-ell" :title="curTask ? taskText(curTask) : ''">{{ curTask ? taskText(curTask) : '日期、景区、表、推送在下面设置' }}</p></div>
        <button class="btn primary" @click="doRun"><Icon name="play" :size="14"/>开始执行</button>
      </div>
      <div class="card tool">
        <span class="icon-sq green"><Icon name="bell" :size="18" :sw="2.2"/></span>
        <div class="tool-main"><h3>通知<Tip wide text="数据质量检测不通过、回退或计算出错时发通知（飞书机器人）。页面上设置的优先；没设置就用配置文件里的。设置后下一次执行生效"/></h3>
          <p v-if="fs"><Tag :color="fs.source === 'none' ? 'grey' : fs.source === 'page' ? 'violet' : 'cyan'">{{ FS_SRC[fs.source] }}</Tag>
            <span v-if="fs.webhook" class="mono"> {{ fs.webhook }}</span><span v-if="fs.secret"> · 已开签名校验</span></p></div>
        <div class="tool-btns"><button class="btn light" @click="openFs"><Icon name="gear" :size="14"/>设置</button>
          <button class="btn light" :disabled="fs && fs.source === 'none'" @click="doNotify">发送测试消息</button></div>
      </div>
      <div class="card tool">
        <span class="icon-sq orange"><Icon name="del" :size="18" :sw="2.2"/></span>
        <div><h3>清理执行记录<Tip text="删除这么多天以前的执行记录、日志文件和调度日志，最少保留 7 天"/></h3><p><input type="number" min="7" v-model.number="cleanDays" class="w64" /> 天前</p></div>
        <button class="btn light danger" @click="doClean">清理</button>
      </div>
    </div>

    <TaskBody v-if="!rr.task" :cfg="cfg" :push-off="pushOff" once />

    <Transition name="dlg">
      <div v-if="fsEdit" class="modal-mask" @click.self="fsEdit=null">
        <div class="modal narrow">
          <div class="card-head"><h3>通知设置</h3><button class="s-btn-icon" @click="fsEdit=null"><Icon name="clear" :size="18"/></button></div>
          <div class="form fs-form">
            <div class="frow"><label>机器人地址</label><input v-model="fsEdit.webhook" autocomplete="off"
              :placeholder="fs && fs.page_set ? '当前：' + fs.webhook + '（留空不改）' : 'https://open.feishu.cn/open-apis/bot/v2/hook/…'" /></div>
            <div class="frow"><label>签名密钥</label><input v-model="fsEdit.secret" type="password" autocomplete="new-password"
              :placeholder="fsEdit.keepSecret && fs.secret ? '已设置（留空不改）' : '机器人开了「签名校验」才填'" /></div>
            <div v-if="fsEdit.keepSecret && fs.secret" class="frow"><label></label>
              <label class="chk"><input type="checkbox" :checked="!fsEdit.keepSecret" @change="fsEdit.keepSecret = !$event.target.checked" /> 去掉签名密钥</label></div>
            <p class="muted small">飞书群 → 设置 → 群机器人 → 自定义机器人，复制 Webhook 地址。保存在系统表里，页面上只显示打码后的。</p>
          </div>
          <div class="dlg-btns">
            <button v-if="fs && fs.page_set" class="btn light danger" @click="clearFs">清除页面设置</button>
            <span class="grow"></span>
            <button class="btn light" @click="fsEdit=null">取消</button>
            <button class="btn primary" :disabled="fsSaving" @click="saveFs">保存</button>
          </div>
        </div>
      </div></Transition>

    <div class="card" v-if="config">
      <div class="card-head"><h3>当前配置<Tip text="只读；修改请编辑 settings_local.py 后重启服务（推送参数可在「推送」页改），密钥已打码"/></h3></div>
      <div class="grid cards3">
        <div v-for="(v,k) in config" :key="k" class="cfg"><h4>{{ k }}</h4>
          <dl class="kv"><template v-for="(vv,kk) in v" :key="kk"><dt :title="kk">{{ kk }}</dt><dd :title="show(vv)">{{ show(vv) }}</dd></template></dl></div>
      </div>
    </div>
  </div>`,
}
