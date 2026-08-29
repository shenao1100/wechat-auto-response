<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
const items=ref([]),editing=ref({key:'',value:'',expires_at:null}),error=ref(''),saving=ref(false)
async function load(){try{items.value=await api.memories()}catch(e){error.value=e.message}}
function edit(item={key:'',value:'',expires_at:null}){editing.value={...item}}
async function save(){saving.value=true;try{await api.saveMemory(editing.value);editing.value={key:'',value:'',expires_at:null};await load()}catch(e){error.value=e.message}finally{saving.value=false}}
async function remove(item){if(!confirm(`删除共享记忆 ${item.key}？`))return;await api.deleteMemory(item.key);await load()}
onMounted(load)
</script>
<template>
  <div class="page-head"><div><h2>长期记忆</h2><p>所有监听群共享同一份记忆；任一群确认的稳定事实和偏好都能帮助其他群判断。</p></div><span class="pill success">全局共享</span></div>
  <div v-if="error" class="error-box">{{ error }}</div>
  <div class="memory-layout">
    <section class="card"><div class="card-head"><h3>共享记忆库</h3><span class="pill neutral">{{ items.length }} 条</span></div><div v-if="items.length" class="memory-list"><article v-for="item in items" :key="item.key"><div><code>{{ item.key }}</code><span v-if="item.expires_at" class="pill warning">会过期</span></div><p>{{ item.value }}</p><footer><small>更新于 {{ new Date(item.updated_at).toLocaleString() }}</small><div><button class="btn small" @click="edit(item)">编辑</button><button class="btn danger small" @click="remove(item)">删除</button></div></footer></article></div><div v-else class="empty"><strong>还没有共享记忆</strong>可手工添加，也可由任一群的 Agent 澄清流程自动写入。</div></section>
    <aside class="card memory-form"><div class="card-head"><h3>{{ editing.key?'编辑记忆':'添加记忆' }}</h3></div><div class="card-body"><div class="field"><label>记忆键</label><input v-model="editing.key" class="input mono" placeholder="例如 class.attendance_preference"/><small>使用稳定、可复用的命名，不要用完整问题作为键。</small></div><div class="field"><label>事实或偏好</label><textarea v-model="editing.value" class="textarea" rows="8" placeholder="已确认的信息…"></textarea></div><div class="field"><label>过期时间（可选 ISO 8601）</label><input v-model="editing.expires_at" class="input" placeholder="2026-09-30T23:59:00+08:00"/></div><button class="btn primary full" :disabled="saving||!editing.key||!editing.value" @click="save">{{ saving?'保存中…':'保存记忆' }}</button></div></aside>
  </div>
</template>
<style scoped>
.memory-layout{display:grid;grid-template-columns:1fr 350px;gap:16px;align-items:start}.memory-list{padding:4px 20px}.memory-list article{padding:17px 0;border-bottom:1px solid var(--line)}.memory-list article:last-child{border:0}.memory-list article>div:first-child{display:flex;align-items:center;gap:8px}.memory-list code{font-size:11px;color:var(--green-dark);background:var(--green-soft);padding:4px 7px;border-radius:6px}.memory-list p{font-size:13px;line-height:1.7;white-space:pre-wrap;margin:12px 0}.memory-list footer{display:flex;justify-content:space-between;align-items:center}.memory-list footer small{color:var(--muted);font-size:10px}.memory-list footer div{display:flex;gap:6px}.memory-form{position:sticky;top:105px}.memory-form .field{margin-bottom:16px}.full{width:100%}@media(max-width:900px){.memory-layout{grid-template-columns:1fr}.memory-form{position:static}}
</style>
