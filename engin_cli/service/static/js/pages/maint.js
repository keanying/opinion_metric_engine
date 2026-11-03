import { ref, reactive, onMounted } from 'vue'
import { api, todayYmd, notify, confirmBox } from '../api.js'
import { TaskBody, blankConfig } from './tasks.js'

export default {
  components: { TaskBody },
  setup() {
    const cfg = reactive({ ...blankConfig(), date_mode: 'range', start_date: todayYmd(-7), end_date: todayYmd() })
    const cleanDays = ref(90), config = ref(null), pushOff = ref(false)
    onMounted(async () => {
      try { config.value = await api('/api/config') } catch (e) { notify(e.message, 'err') }
      try { pushOff.value = !(await api('/api/push/settings')).settings.enabled } catch (e) { /* 忽略 */ }
    })
    const go = id => { location.hash = '/runs/' + id }
    const doRun = async () => {
      try { const r = await api('/api/maint/run', { method: 'POST', body: { config: cfg } }); notify(`已提交执行 #${r.run_id}`); go(r.run_id) }
      catch (e) { notify(e.message, 'err') }
    }
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
    return { cfg, cleanDays, config, pushOff, doRun, doNotify, doClean, show }
  },
  template: `
  <div>
    <div class="grid three-tools">
      <div class="card tool">
        <span class="icon-sq grad"><Icon name="refresh" :size="18" :sw="2.2"/></span>
        <div><h3>重跑一段日期<Tip text="临时执行，不建任务。在下面设置日期、表、推送后点「开始执行」"/></h3></div>
        <button class="btn primary" @click="doRun"><Icon name="play" :size="14"/>开始执行</button>
      </div>
      <div class="card tool">
        <span class="icon-sq green"><Icon name="bell" :size="18" :sw="2.2"/></span>
        <div><h3>飞书通知测试<Tip text="发一条测试消息，确认 notify_feishu_webhook 配对了"/></h3></div>
        <button class="btn light" @click="doNotify">发送测试消息</button>
      </div>
      <div class="card tool">
        <span class="icon-sq orange"><Icon name="del" :size="18" :sw="2.2"/></span>
        <div><h3>清理执行记录<Tip text="删除这么多天以前的执行记录、日志文件和调度日志，最少保留 7 天"/></h3><p><input type="number" min="7" v-model.number="cleanDays" class="w64" /> 天前</p></div>
        <button class="btn light danger" @click="doClean">清理</button>
      </div>
    </div>

    <TaskBody :cfg="cfg" :push-off="pushOff" />

    <div class="card" v-if="config">
      <div class="card-head"><h3>当前配置<Tip text="只读；修改请编辑 settings_local.py 后重启服务（推送参数可在「推送」页改），密钥已打码"/></h3></div>
      <div class="grid cards3">
        <div v-for="(v,k) in config" :key="k" class="cfg"><h4>{{ k }}</h4>
          <dl class="kv"><template v-for="(vv,kk) in v" :key="kk"><dt>{{ kk }}</dt><dd :title="show(vv)">{{ show(vv) }}</dd></template></dl></div>
      </div>
    </div>
  </div>`,
}
