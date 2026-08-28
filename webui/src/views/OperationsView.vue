<script setup>
import { onMounted, ref, watch } from 'vue'
import { api } from '../api'
const tab=ref('runs'),groups=ref([]),groupId=ref(''),runs=ref([]),schedules=ref([]),failed=ref({inbox:[],deliveries:[]}),error=ref(''),toast=ref('')
async function load(){try{if(tab.value==='runs')runs.value=await api.runs(100);if(tab.value==='schedules'&&groupId.value)schedules.value=await api.schedules(groupId.value);if(tab.value==='failed')failed.value=await api.failed()}catch(e){error.value=e.message}}
async function retry(){const result=await api.retryFailed();toast.value=`已重新入队：消息 ${result.inbox_requeued}，投递 ${result.deliveries_requeued}`;await load();setTimeout(()=>toast.value='',2500)}
watch([tab,groupId],load)
onMounted(async()=>{try{groups.value=(await api.settings()).groups;groupId.value=groups.value[0]?.id||'';await load()}catch(e){error.value=e.message}})
</script>
<template>
  <div class="page-head"><div><h2>日程与审计</h2><p>查看 Agent 决策、内部提醒和需要人工处理的失败任务。</p></div><div class="tabs"><button v-for="item in [{id:'runs',t:'Agent 运行'},{id:'schedules',t:'日程提醒'},{id:'failed',t:'失败队列'}]" :key="item.id" :class="{active:tab===item.id}" @click="tab=item.id">{{ item.t }}</button></div></div>
  <div v-if="error" class="error-box">{{ error }}</div>
  <section v-if="tab==='runs'" class="card"><div class="card-head"><h3>最近 100 次 Agent 决策</h3><span class="pill neutral">{{ runs.length }}</span></div><div class="table-wrap"><table class="table"><thead><tr><th>ID</th><th>群聊</th><th>结果</th><th>详情</th><th>时间</th></tr></thead><tbody><tr v-for="run in runs" :key="run.id"><td>#{{ run.id }}</td><td class="mono">{{ run.group_id }}</td><td><span class="pill" :class="run.outcome==='important'?'success':run.outcome==='error'?'danger':run.outcome==='awaiting_clarification'?'warning':'neutral'">{{ run.outcome }}</span></td><td class="truncate">{{ run.detail }}</td><td>{{ new Date(run.created_at).toLocaleString() }}</td></tr></tbody></table></div></section>
  <section v-if="tab==='schedules'" class="card"><div class="card-head"><h3>内部日程提醒</h3><select v-model="groupId" class="select group"><option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }}</option></select></div><div class="table-wrap"><table class="table"><thead><tr><th>标题</th><th>内容</th><th>执行时间</th><th>状态</th></tr></thead><tbody><tr v-for="item in schedules" :key="item.id"><td>{{ item.title }}</td><td class="truncate">{{ item.content }}</td><td>{{ new Date(item.run_at).toLocaleString() }}</td><td><span class="pill neutral">{{ item.status }}</span></td></tr></tbody></table><div v-if="!schedules.length" class="empty">暂无日程提醒</div></div></section>
  <section v-if="tab==='failed'" class="card"><div class="card-head"><h3>需要人工处理</h3><button class="btn primary small" @click="retry">重新入队全部失败项</button></div><div class="card-body"><h4>消息处理失败</h4><div v-if="failed.inbox?.length" class="table-wrap"><table class="table"><tbody><tr v-for="item in failed.inbox" :key="item.id"><td>#{{ item.id }}</td><td class="mono">{{ item.group_id }}</td><td>{{ item.last_error }}</td><td>{{ new Date(item.received_at).toLocaleString() }}</td></tr></tbody></table></div><p v-else class="muted">无失败 inbox</p><div class="divider"></div><h4>微信投递失败</h4><div v-if="failed.deliveries?.length" class="table-wrap"><table class="table"><tbody><tr v-for="item in failed.deliveries" :key="item.id"><td>#{{ item.id }}</td><td>{{ item.target }}</td><td>尝试 {{ item.attempts }} 次</td><td>{{ item.last_error }}</td></tr></tbody></table></div><p v-else class="muted">无失败 delivery</p></div></section>
  <div v-if="toast" class="toast">{{ toast }}</div>
</template>
<style scoped>
.tabs{display:flex;background:#e7ece9;padding:3px;border-radius:10px}.tabs button{border:0;background:transparent;padding:7px 12px;border-radius:8px;font-size:12px;color:var(--muted)}.tabs button.active{background:#fff;color:var(--ink);box-shadow:0 2px 7px rgba(0,0,0,.08)}.group{width:220px}.card-body h4{font-size:12px}
</style>
