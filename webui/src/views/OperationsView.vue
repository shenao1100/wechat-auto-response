<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api'

const tab = ref('runs')
const groups = ref([])
const runs = ref([])
const schedules = ref([])
const failed = ref({ inbox: [], deliveries: [] })
const error = ref('')
const toast = ref('')
const loading = ref(false)

const groupNames = computed(() => Object.fromEntries(groups.value.map(group => [group.id, group.name])))

function formatTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString()
}

function outcomeClass(outcome) {
  if (outcome === 'important' || outcome === 'chain_joined' || outcome === 'chain_already_joined' || outcome === 'direct_chat') return 'success'
  if (outcome === 'error' || outcome === 'max_steps') return 'danger'
  if (outcome === 'awaiting_clarification') return 'warning'
  return 'neutral'
}

function outcomeLabel(outcome) {
  return {
    important: '重要并转发', ignored: '已忽略', awaiting_clarification: '等待确认', direct_chat: '私聊已回复', chain_joined: '已自动接龙', chain_already_joined: '本人已接龙，跳过',
    max_steps: '达到步数上限', error: '运行错误',
  }[outcome] || outcome
}

function statusClass(status) {
  if (status === 'sent') return 'success'
  if (status === 'failed' || status === 'cancelled') return 'danger'
  if (status === 'pending' || status === 'sending') return 'warning'
  return 'neutral'
}

function statusLabel(status) {
  return { pending: '等待执行', sending: '发送中', sent: '已发送', failed: '失败', cancelled: '已取消' }[status] || status
}

function parseRunDetail(detail) {
  if (!detail) return { reason: '—', flags: [], raw: '' }
  try {
    const parsed = JSON.parse(detail)
    if (!parsed || typeof parsed !== 'object') return { reason: String(detail), flags: [], raw: '' }
    const flags = []
    if (typeof parsed.important === 'boolean') flags.push(`重要：${parsed.important ? '是' : '否'}`)
    if (typeof parsed.forwarded === 'boolean') flags.push(`已转发：${parsed.forwarded ? '是' : '否'}`)
    if (typeof parsed.awaiting_clarification === 'boolean') flags.push(`等待确认：${parsed.awaiting_clarification ? '是' : '否'}`)
    return {
      reason: parsed.reason || '—', flags,
      raw: Object.keys(parsed).some(key => !['important', 'forwarded', 'awaiting_clarification', 'reason'].includes(key))
        ? JSON.stringify(parsed, null, 2) : '',
    }
  } catch (_) {
    return { reason: String(detail), flags: [], raw: '' }
  }
}

function parseTargets(value) {
  if (Array.isArray(value)) return value
  try {
    const parsed = JSON.parse(value || '[]')
    return Array.isArray(parsed) ? parsed : [String(value)]
  } catch (_) {
    return value ? [String(value)] : []
  }
}

function formatJson(value) {
  if (!value) return '—'
  try { return JSON.stringify(JSON.parse(value), null, 2) } catch (_) { return String(value) }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    if (tab.value === 'runs') runs.value = await api.runs(100)
    if (tab.value === 'schedules') schedules.value = await api.schedules()
    if (tab.value === 'failed') failed.value = await api.failed()
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function retry() {
  try {
    const result = await api.retryFailed()
    toast.value = `已重新入队：消息 ${result.inbox_requeued}，投递 ${result.deliveries_requeued}`
    await load()
    setTimeout(() => { toast.value = '' }, 2500)
  } catch (e) {
    error.value = e.message
  }
}

watch(tab, load)
onMounted(async () => {
  try {
    const configured = (await api.settings()).groups
    const configuredIds = new Set(configured.map(group => group.id))
    const directTargets = [...new Set(configured.flatMap(group => group.forward_to || []))]
      .filter(target => !configuredIds.has(target))
      .map(target => ({ id: target, name: `私聊 · ${target}`, forward_to: [target], direct: true }))
    groups.value = [...configured, ...directTargets]
    await load()
  } catch (e) {
    error.value = e.message
  }
})
</script>

<template>
  <div class="page-head">
    <div><h2>日程与审计</h2><p>完整查看 Agent 决策依据、内部提醒和需要人工处理的失败任务。</p></div>
    <div class="head-actions">
      <div class="tabs"><button v-for="item in [{id:'runs',t:'Agent 运行'},{id:'schedules',t:'日程提醒'},{id:'failed',t:'失败队列'}]" :key="item.id" :class="{active:tab===item.id}" @click="tab=item.id">{{ item.t }}</button></div>
      <button class="btn small" :disabled="loading" @click="load">{{ loading ? '加载中…' : '刷新' }}</button>
    </div>
  </div>

  <div v-if="error" class="error-box">{{ error }}</div>

  <section v-if="tab==='runs'" class="card audit-card">
    <div class="card-head"><div><h3>最近 100 次 Agent 决策</h3><small>理由和状态字段完整展示，不再省略长文本</small></div><span class="pill neutral">{{ runs.length }}</span></div>
    <div class="table-wrap">
      <table class="table detail-table runs-table">
        <thead><tr><th>ID / 时间</th><th>群聊</th><th>结果</th><th>完整决策详情</th></tr></thead>
        <tbody><tr v-for="run in runs" :key="run.id">
          <td class="meta-cell"><strong>#{{ run.id }}</strong><time>{{ formatTime(run.created_at) }}</time></td>
          <td class="group-cell"><strong>{{ groupNames[run.group_id] || '未知群聊' }}</strong><code>{{ run.group_id }}</code></td>
          <td><span class="pill" :class="outcomeClass(run.outcome)">{{ outcomeLabel(run.outcome) }}</span><code class="outcome-code">{{ run.outcome }}</code></td>
          <td class="detail-cell"><p>{{ parseRunDetail(run.detail).reason }}</p><div v-if="parseRunDetail(run.detail).flags.length" class="flag-list"><span v-for="flag in parseRunDetail(run.detail).flags" :key="flag">{{ flag }}</span></div><pre v-if="parseRunDetail(run.detail).raw">{{ parseRunDetail(run.detail).raw }}</pre></td>
        </tr></tbody>
      </table>
      <div v-if="!loading && !runs.length" class="empty">暂无 Agent 运行记录</div>
    </div>
  </section>

  <section v-if="tab==='schedules'" class="card audit-card">
    <div class="card-head"><div><h3>全局日程提醒</h3><small>所有监听群与 forward_to 私聊共享，包括完整内容、来源、目标和生命周期时间</small></div><span class="pill success">全局共享 · {{ schedules.length }}</span></div>
    <div class="table-wrap">
      <table class="table detail-table schedule-table">
        <thead><tr><th>ID / 状态</th><th>群聊</th><th>标题与完整内容</th><th>执行时间</th><th>转发目标</th><th>创建 / 发送</th></tr></thead>
        <tbody><tr v-for="item in schedules" :key="item.id">
          <td class="meta-cell"><strong>#{{ item.id }}</strong><span class="pill" :class="statusClass(item.status)">{{ statusLabel(item.status) }}</span><code>{{ item.status }}</code></td>
          <td class="group-cell"><strong>{{ groupNames[item.group_id] || '未知群聊' }}</strong><code>{{ item.group_id }}</code></td>
          <td class="content-cell"><strong>{{ item.title }}</strong><p>{{ item.content }}</p></td>
          <td class="time-cell"><time>{{ formatTime(item.run_at) }}</time></td>
          <td><div class="target-list"><span v-for="target in parseTargets(item.targets_json)" :key="target">{{ target }}</span><em v-if="!parseTargets(item.targets_json).length">—</em></div></td>
          <td class="time-cell"><small>创建</small><time>{{ formatTime(item.created_at) }}</time><small>发送</small><time>{{ formatTime(item.sent_at) }}</time></td>
        </tr></tbody>
      </table>
      <div v-if="!loading && !schedules.length" class="empty">暂无日程提醒</div>
    </div>
  </section>

  <section v-if="tab==='failed'" class="failed-layout">
    <article class="card audit-card">
      <div class="card-head"><div><h3>消息处理失败</h3><small>Agent 未能完成处理的收件记录</small></div><span class="pill danger">{{ failed.inbox?.length || 0 }}</span></div>
      <div class="table-wrap"><table v-if="failed.inbox?.length" class="table detail-table failed-table"><thead><tr><th>ID / 状态 / 时间</th><th>群聊 / 消息键</th><th>完整原始消息</th><th>完整错误信息</th></tr></thead><tbody><tr v-for="item in failed.inbox" :key="item.id"><td class="meta-cell"><strong>#{{ item.id }}</strong><span class="pill danger">{{ item.status }}</span><time>收到 {{ formatTime(item.received_at) }}</time><time>完成 {{ formatTime(item.finished_at) }}</time></td><td class="group-cell"><strong>{{ groupNames[item.group_id] || '未知群聊' }}</strong><code>{{ item.group_id }}</code><code>{{ item.message_key }}</code></td><td><pre class="payload-detail">{{ formatJson(item.payload_json) }}</pre></td><td class="error-detail">{{ item.last_error || '未记录错误详情' }}</td></tr></tbody></table><div v-else class="empty">无失败 inbox</div></div>
    </article>
    <article class="card audit-card">
      <div class="card-head"><div><h3>微信投递失败</h3><small>发送到目标账户时失败的投递记录</small></div><div class="card-actions"><span class="pill danger">{{ failed.deliveries?.length || 0 }}</span><button class="btn primary small" :disabled="loading || (!failed.inbox?.length && !failed.deliveries?.length)" @click="retry">全部重新入队</button></div></div>
      <div class="table-wrap"><table v-if="failed.deliveries?.length" class="table detail-table failed-table"><thead><tr><th>投递 / Outbox / 状态</th><th>目标与尝试次数</th><th>完整待发送内容</th><th>创建 / 发送</th><th>完整错误信息</th></tr></thead><tbody><tr v-for="item in failed.deliveries" :key="item.id"><td class="meta-cell"><strong>#{{ item.id }}</strong><code>outbox #{{ item.outbox_id }}</code><span class="pill danger">{{ item.status }}</span></td><td><strong>{{ item.target }}</strong><small class="block-muted">已尝试 {{ item.attempts }} 次</small></td><td><pre class="payload-detail">{{ item.text || '—' }}</pre></td><td class="time-cell"><small>创建</small><time>{{ formatTime(item.created_at) }}</time><small>发送</small><time>{{ formatTime(item.sent_at) }}</time></td><td class="error-detail">{{ item.last_error || '未记录错误详情' }}</td></tr></tbody></table><div v-else class="empty">无失败 delivery</div></div>
    </article>
  </section>

  <div v-if="toast" class="toast">{{ toast }}</div>
</template>

<style scoped>
.head-actions,.card-actions{display:flex;align-items:center;gap:9px}.tabs{display:flex;background:#e7ece9;padding:3px;border-radius:10px}.tabs button{border:0;background:transparent;padding:7px 12px;border-radius:8px;font-size:12px;color:var(--muted)}.tabs button.active{background:#fff;color:var(--ink);box-shadow:0 2px 7px rgba(0,0,0,.08)}
.audit-card{overflow:hidden}.card-head>div:first-child small{display:block;margin-top:5px;color:var(--muted);font-size:10px}.group{width:240px}.detail-table{table-layout:auto;min-width:900px}.detail-table th{position:sticky;top:0;z-index:1}.detail-table td{padding:15px 14px;line-height:1.6}.detail-table tbody tr:hover{background:#fafcfb}
.meta-cell{width:145px}.meta-cell strong,.meta-cell time,.meta-cell code{display:block}.meta-cell time{color:var(--muted);font-size:10px;margin-top:6px}.meta-cell .pill{margin:7px 0 4px}.group-cell{width:210px}.group-cell strong,.group-cell code{display:block}.group-cell code{margin-top:5px;color:var(--muted);font-size:10px;overflow-wrap:anywhere}.outcome-code{display:block;margin-top:6px;color:var(--muted);font-size:9px}.detail-cell{min-width:360px}.detail-cell p,.content-cell p{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}.detail-cell pre{margin:9px 0 0;padding:10px;border:1px solid var(--line);border-radius:8px;background:#f7faf8;white-space:pre-wrap;overflow-wrap:anywhere;font-size:10px}.flag-list{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}.flag-list span{padding:3px 7px;border-radius:6px;background:#edf3ef;color:#526158;font-size:10px}
.schedule-table{min-width:1120px}.content-cell{min-width:350px}.content-cell strong{display:block;margin-bottom:7px;font-size:13px}.time-cell{width:175px}.time-cell small,.time-cell time{display:block}.time-cell small{color:var(--muted);font-size:9px;margin-top:7px}.time-cell small:first-child{margin-top:0}.target-list{display:flex;gap:6px;flex-wrap:wrap;min-width:150px}.target-list span{background:var(--green-soft);color:var(--green-dark);padding:4px 7px;border-radius:6px;font-size:10px}.target-list em{color:var(--muted);font-style:normal}
.failed-layout{display:grid;gap:16px}.failed-table{min-width:1150px}.error-detail{min-width:350px;color:#8d3030;white-space:pre-wrap;overflow-wrap:anywhere}.payload-detail{min-width:330px;max-width:560px;margin:0;white-space:pre-wrap;overflow-wrap:anywhere;font:10px/1.6 "Cascadia Code",Consolas,monospace;color:#34463b}.block-muted{display:block;color:var(--muted);font-size:10px;margin-top:5px}
@media(max-width:900px){.head-actions{width:100%;align-items:stretch;flex-direction:column}.tabs{overflow:auto}.tabs button{flex:1;white-space:nowrap}.head-actions>.btn{align-self:flex-end}.card-head{align-items:flex-start;gap:12px}.group{width:min(240px,45vw)}}
</style>
