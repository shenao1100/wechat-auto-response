<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

const settings = ref(null), status = ref(null), runs = ref([]), error = ref('')
const total = (bucket = {}) => Object.values(bucket).reduce((a, b) => a + b, 0)
const cards = computed(() => status.value ? [
  { label: '监听群聊', value: settings.value?.groups?.filter(g => g.enabled).length || 0, detail: '已启用规则', icon: '群' },
  { label: '待处理消息', value: status.value.inbox?.pending || 0, detail: `累计 ${total(status.value.inbox)} 条`, icon: '信' },
  { label: '待确认事项', value: status.value.clarifications?.pending || 0, detail: '等待用户补充信息', icon: '?' },
  { label: '长期记忆', value: status.value.memories || 0, detail: '跨会话事实与偏好', icon: '忆' },
] : [])

onMounted(async () => {
  try { [settings.value, status.value, runs.value] = await Promise.all([api.settings(), api.status(), api.runs(8)]) }
  catch (e) { error.value = e.message }
})
</script>

<template>
  <div class="page-head"><div><h2>运行概览</h2><p>查看消息处理、澄清任务和 Agent 决策的整体状态。</p></div><RouterLink to="/rules" class="btn primary">配置监听规则</RouterLink></div>
  <div v-if="error" class="error-box">{{ error }}</div>
  <div v-else-if="!status" class="loading">正在读取运行状态…</div>
  <template v-else>
    <div class="stats-grid">
      <div v-for="card in cards" :key="card.label" class="card stat-card">
        <div class="stat-top"><span>{{ card.label }}</span><i class="stat-icon">{{ card.icon }}</i></div>
        <b>{{ card.value }}</b><small>{{ card.detail }}</small>
      </div>
    </div>
    <div class="dashboard-grid">
      <section class="card">
        <div class="card-head"><h3>监听链路</h3><span class="pill success">CONFIGURED</span></div>
        <div class="card-body group-summary" v-if="settings.groups.length">
          <div v-for="group in settings.groups" :key="group.id" class="summary-row">
            <div class="avatar">{{ group.name.slice(0,1) }}</div>
            <div class="summary-main"><strong>{{ group.name }}</strong><small>{{ group.forward_to.join('、') }}</small></div>
            <div><span class="pill" :class="group.enabled ? 'success' : 'neutral'">{{ group.enabled ? '监听中' : '已停用' }}</span><small class="threshold">阈值 {{ group.importance_threshold || 70 }}</small></div>
          </div>
        </div>
        <div v-else class="empty"><strong>尚未配置群聊</strong>从微信群聊目录添加第一条监听规则。</div>
      </section>
      <section class="card">
        <div class="card-head"><h3>最近 Agent 决策</h3><RouterLink to="/operations" class="btn small">查看全部</RouterLink></div>
        <div v-if="runs.length" class="run-list">
          <div v-for="run in runs" :key="run.id" class="run-row"><span class="decision-dot" :class="run.outcome"></span><div><strong>{{ run.outcome }}</strong><small>{{ run.group_id }}</small></div><time>{{ new Date(run.created_at).toLocaleString() }}</time></div>
        </div>
        <div v-else class="empty">暂无 Agent 运行记录</div>
      </section>
    </div>
  </template>
</template>

<style scoped>
.dashboard-grid{display:grid;grid-template-columns:1.15fr .85fr;gap:16px;margin-top:16px}.summary-row{display:flex;align-items:center;gap:12px;padding:13px 0;border-bottom:1px solid var(--line)}.summary-row:last-child{border:0}.avatar{width:38px;height:38px;border-radius:11px;background:var(--green-soft);color:var(--green-dark);display:grid;place-items:center;font-weight:800}.summary-main{flex:1}.summary-main strong,.summary-main small,.summary-row>div:last-child span,.summary-row>div:last-child small{display:block}.summary-main strong{font-size:13px}.summary-main small{font-size:11px;color:var(--muted);margin-top:4px}.threshold{font-size:10px;color:var(--muted);text-align:right;margin-top:5px}.run-list{padding:5px 18px}.run-row{display:flex;align-items:center;gap:10px;padding:12px 0;border-bottom:1px solid var(--line)}.run-row:last-child{border:0}.run-row div{flex:1}.run-row strong,.run-row small{display:block}.run-row strong{font-size:12px;text-transform:capitalize}.run-row small,.run-row time{font-size:10px;color:var(--muted)}.decision-dot{width:8px;height:8px;border-radius:50%;background:#929f97}.decision-dot.important{background:var(--green)}.decision-dot.error,.decision-dot.max_steps{background:var(--danger)}.decision-dot.awaiting_clarification{background:var(--warning)}@media(max-width:950px){.dashboard-grid{grid-template-columns:1fr}}
</style>
