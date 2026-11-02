// 接口调用：统一带 X-Requested-With（服务端据此挡跨站提交），出错抛带中文信息的 Error
export async function api(path, { method = 'GET', body } = {}) {
  const opt = { method, headers: { 'X-Requested-With': 'omx' } }
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json'
    opt.body = JSON.stringify(body)
  }
  const res = await fetch(path, opt)
  let data = null
  try { data = await res.json() } catch (e) { /* 非 JSON */ }
  if (!res.ok) throw new Error((data && data.error) || `请求失败（HTTP ${res.status}）`)
  return data
}

export const STATUS = {
  queued: { text: '排队中', cls: 'gray' },
  running: { text: '执行中', cls: 'blue' },
  success: { text: '成功', cls: 'green' },
  failed: { text: '失败', cls: 'red' },
  blocked: { text: '已回退', cls: 'orange' },
  cancelled: { text: '已取消', cls: 'gray' },
}

export const KIND = { run: '跑批', push: '补推', notify: '通知测试' }
export const TRIGGER = { schedule: '定时', manual: '手动' }

export const TABLES = [
  { key: 'drill', label: '下钻表', table: 'ads_trf_social_opinion_drill_analysis_di',
    desc: '词 → 评论 → 作品明细，可按采集关键字过滤', color: 'violet', icon: 'layers' },
  { key: 'macro', label: '大盘表', table: 'ads_trf_social_opinion_macro_gran_metric_di',
    desc: '景区 × 日期 × 9 种周期 × 渠道的 KPI、词云、地域', color: 'indigo', icon: 'chart' },
  { key: 'core', label: '核心表', table: 'ads_trf_social_opinion_comment_core_di',
    desc: '每天评论数、情感得分（core）', color: 'light-blue', icon: 'db' },
  { key: 'platform', label: '渠道表', table: 'ads_trf_social_opinion_comment_platform_di',
    desc: '分渠道评论数、得分', color: 'cyan', icon: 'db' },
  { key: 'dimension', label: '维度表', table: 'ads_trf_social_opinion_comment_dimension_score_di',
    desc: '一 / 二 / 三级维度得分', color: 'green', icon: 'db' },
  { key: 'content', label: '内容表', table: 'ads_trf_social_opinion_comment_content_di',
    desc: '情感词 / 关键词', color: 'amber', icon: 'db' },
]
export const TABLE_LABEL = Object.fromEntries(TABLES.flatMap(t => [[t.key, t.label], [t.table, t.label]]))

// 推送记录的结果 → 标签
export const PUSH_RESULT = {
  ok: { color: 'green', icon: 'tickCircle', text: '成功' },
  business_fail: { color: 'pink', icon: 'clear', text: '业务失败' },
  http_error: { color: 'red', icon: 'clear', text: 'HTTP 错误' },
  network_error: { color: 'orange', icon: 'alert', text: '网络错误' },
  timeout: { color: 'orange', icon: 'clock', text: '超时' },
  ssl_error: { color: 'red', icon: 'shield', text: '证书错误' },
  dry_run: { color: 'cyan', icon: 'comment', text: '试推' },
}

export const GRANULARITIES = [
  ['td', '今日'], ['latest_1d', '近1日'], ['latest_7d', '近7日'], ['wtd', '本周'],
  ['latest_30d', '近30日'], ['mtd', '本月'], ['latest_60d', '近60日'],
  ['latest_90d', '近90日'], ['qtd', '本季度'],
]

export const CHANNELS = {
  all: '整体', douyin: '抖音', xiaohongshu: '小红书', weibo: '微博',
  kuaishou: '快手', ctrip: '携程', tongcheng: '同程',
}

export function duration(a, b) {
  if (!a) return ''
  const s = Math.max(0, Math.round(((b ? new Date(b.replace(' ', 'T')) : new Date())
    - new Date(a.replace(' ', 'T'))) / 1000))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m${s % 60}s`
  return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`
}

export function fmtNum(v, d = 0) {
  if (v === null || v === undefined || v === '') return '-'
  const n = Number(v)
  return Number.isFinite(n) ? n.toLocaleString('zh-CN', { maximumFractionDigits: d }) : String(v)
}

export function todayYmd(offset = 0) {
  const t = new Date(Date.now() + offset * 86400000)
  return `${t.getFullYear()}${String(t.getMonth() + 1).padStart(2, '0')}${String(t.getDate()).padStart(2, '0')}`
}

// 全局轻提示 / 顶栏调度状态
import { reactive } from 'vue'
export const sched = reactive({ data: null })
export async function refreshSched() {
  try { sched.data = await api('/api/scheduler') } catch (e) { /* 忽略 */ }
}
export const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
export const toast = reactive({ items: [] })
export function notify(text, type = 'ok') {
  const item = { id: Math.random(), text, type }
  toast.items.push(item)
  setTimeout(() => { toast.items.splice(toast.items.indexOf(item), 1) }, type === 'err' ? 6000 : 3000)
}
