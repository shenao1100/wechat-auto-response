<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
const items=ref([]),filter=ref('pending'),answers=ref({}),error=ref(''),loading=ref(true)
const pendingCount=computed(()=>items.value.filter(x=>x.status==='pending').length)
async function load(){loading.value=true;try{items.value=await api.clarifications(filter.value==='all'?'':filter.value)}catch(e){error.value=e.message}finally{loading.value=false}}
async function answer(item){const value=(answers.value[item.id]||'').trim();if(!value)return;try{await api.answerClarification(item.id,value);answers.value[item.id]='';await load()}catch(e){error.value=e.message}}
onMounted(load)
</script>
<template>
  <div class="page-head"><div><h2>待确认事项</h2><p>Agent 只在缺少用户偏好或背景事实时提问；答案写入共享记忆，并重新评估原消息。</p></div><div class="actions"><span class="pill warning">{{ pendingCount }} 待答复</span><select v-model="filter" class="select compact" @change="load"><option value="pending">仅待确认</option><option value="answered">已回答</option><option value="all">全部</option></select></div></div>
  <div v-if="error" class="error-box">{{ error }}</div><div v-if="loading" class="loading">正在加载待确认事项…</div>
  <div v-else-if="items.length" class="clarification-list"><article v-for="item in items" :key="item.id" class="card clarification"><header><div><span class="id">#{{ item.id }}</span><span class="pill" :class="item.status==='pending'?'warning':'success'">{{ item.status==='pending'?'等待答复':'已写入记忆' }}</span></div><time>{{ new Date(item.created_at).toLocaleString() }}</time></header><div class="question"><small>Agent 想确认</small><h3>{{ item.question }}</h3></div><dl><div><dt>询问对象</dt><dd>{{ item.target }}</dd></div><div><dt>记忆键</dt><dd class="mono">{{ item.memory_key }}</dd></div><div><dt>所属群</dt><dd class="mono">{{ item.group_id }}</dd></div></dl><div v-if="item.status==='pending'" class="answer-box"><textarea v-model="answers[item.id]" class="textarea" rows="3" :placeholder="`回答 #${item.id}，提交后将写入长期记忆`"></textarea><button class="btn primary" :disabled="!answers[item.id]?.trim()" @click="answer(item)">提交并重新评估</button></div><div v-else class="answered"><small>{{ item.answered_by }} 的答复</small><p>{{ item.answer }}</p></div></article></div>
  <div v-else class="card empty"><strong>没有{{ filter==='pending'?'待确认':'' }}事项</strong>Agent 能自行判断时不会打扰 forward_to。</div>
</template>
<style scoped>
.compact{width:130px}.clarification-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.clarification{padding:20px}.clarification header{display:flex;justify-content:space-between;align-items:center}.clarification header>div{display:flex;align-items:center;gap:8px}.clarification time{font-size:10px;color:var(--muted)}.id{font-weight:800;color:var(--green)}.question{margin:20px 0}.question small,.answered small{font-size:10px;color:var(--muted);font-weight:700}.question h3{font-size:16px;line-height:1.6;margin:5px 0}.clarification dl{display:grid;grid-template-columns:repeat(3,1fr);background:#f7f9f8;border-radius:10px;padding:12px;margin:0}.clarification dl div{min-width:0}.clarification dt{font-size:9px;color:var(--muted);margin-bottom:4px}.clarification dd{font-size:11px;margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.answer-box{margin-top:15px;display:flex;flex-direction:column;gap:8px;align-items:flex-end}.answered{margin-top:15px;padding:12px;background:var(--green-soft);border-radius:9px}.answered p{font-size:12px;line-height:1.6;margin:5px 0;white-space:pre-wrap}@media(max-width:900px){.clarification-list{grid-template-columns:1fr}}
</style>
