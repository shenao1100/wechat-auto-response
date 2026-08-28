<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

const prompts=ref([]),selectedName=ref(''),content=ref(''),original=ref(''),newName=ref(''),error=ref(''),toast=ref(''),loading=ref(true)
const dirty=computed(()=>content.value!==original.value)
async function selectPrompt(name){selectedName.value=name;error.value='';try{const result=await api.prompt(name);content.value=result.content;original.value=result.content}catch(e){error.value=e.message}}
async function refresh(){prompts.value=await api.prompts();if(!selectedName.value&&prompts.value.length)await selectPrompt(prompts.value[0].name)}
async function save(){try{const result=await api.savePrompt(selectedName.value,content.value);original.value=content.value;toast.value=result.hot_reload?.applied?'Prompt 已保存并热更新':'Prompt 已保存';await refresh();setTimeout(()=>toast.value='',2500)}catch(e){error.value=e.message}}
async function createPrompt(){let name=newName.value.trim();if(!name)return;if(!name.endsWith('.md'))name+='.md';try{await api.savePrompt(name,'# 新 Prompt\n\n请在这里定义群聊的重要性边界、正例和反例。\n');newName.value='';await refresh();await selectPrompt(name)}catch(e){error.value=e.message}}
async function remove(){if(!confirm(`确定删除 ${selectedName.value}？`))return;try{await api.deletePrompt(selectedName.value);selectedName.value='';content.value='';original.value='';await refresh()}catch(e){error.value=e.message}}
onMounted(async()=>{try{await refresh()}catch(e){error.value=e.message}finally{loading.value=false}})
</script>

<template>
  <div class="page-head"><div><h2>Prompt 管理</h2><p>Prompt 属于策略模板，由监听规则引用；修改模板不会改变群聊与转发层级。</p></div><div class="actions"><span v-if="dirty" class="pill warning">未保存</span><button class="btn primary" :disabled="!selectedName||!dirty" @click="save">保存 Prompt</button></div></div>
  <div v-if="error" class="error-box">{{ error }}</div><div v-if="loading" class="loading">正在加载 Prompt…</div>
  <div v-else class="prompt-layout">
    <aside class="card prompt-list"><div class="card-head"><h3>策略模板</h3><span class="pill neutral">{{ prompts.length }}</span></div><div class="new-prompt"><input v-model="newName" class="input" placeholder="模板名，例如 class_notice" @keyup.enter="createPrompt"/><button class="btn small" @click="createPrompt">新建</button></div><button v-for="prompt in prompts" :key="prompt.name" :class="{active:selectedName===prompt.name}" @click="selectPrompt(prompt.name)"><span>¶</span><div><strong>{{ prompt.name }}</strong><small>{{ Math.ceil(prompt.size/1024) }} KB</small></div></button></aside>
    <section class="card editor"><div v-if="selectedName" class="card-head"><div><h3>{{ selectedName }}</h3><small class="muted">Markdown · 群专属规则优先于通用规则</small></div><button class="btn danger small" @click="remove">删除</button></div><textarea v-if="selectedName" v-model="content" class="prompt-editor" spellcheck="false"></textarea><div v-else class="empty"><strong>选择或创建 Prompt</strong>建议包含清晰的重要/忽略边界，以及贴近真实群消息的正反例。</div></section>
  </div><div v-if="toast" class="toast">{{ toast }}</div>
</template>

<style scoped>
.prompt-layout{display:grid;grid-template-columns:280px 1fr;gap:16px;min-height:650px}.prompt-list{overflow:hidden}.new-prompt{display:flex;gap:7px;padding:12px;border-bottom:1px solid var(--line)}.prompt-list>button{border:0;background:#fff;width:100%;display:flex;align-items:center;gap:10px;padding:11px 15px;text-align:left}.prompt-list>button:hover,.prompt-list>button.active{background:#f0f7f3}.prompt-list>button.active{box-shadow:inset 3px 0 var(--green)}.prompt-list>button>span{color:var(--green);font-weight:800}.prompt-list>button div{flex:1}.prompt-list strong,.prompt-list small{display:block}.prompt-list strong{font-size:12px}.prompt-list small{font-size:10px;color:var(--muted);margin-top:3px}.editor{display:flex;flex-direction:column;overflow:hidden}.prompt-editor{flex:1;min-height:570px;border:0;outline:0;resize:none;padding:22px;font:13px/1.8 "Cascadia Code","Microsoft YaHei",monospace;color:#26352c;background:linear-gradient(90deg,#f8faf9 1px,transparent 1px);background-size:100% 30px}.editor .card-head small{display:block;margin-top:4px}@media(max-width:850px){.prompt-layout{grid-template-columns:1fr}.prompt-editor{min-height:500px}}
</style>
