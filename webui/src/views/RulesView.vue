<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api'

const catalog = ref([]), configured = ref([]), prompts = ref([]), selectedId = ref(''), search = ref(''), contactQuery = ref(''), contacts = ref([])
const loading = ref(true), saving = ref(false), error = ref(''), toast = ref(''), contactTimer = ref(null)
const selected = computed(() => configured.value.find(g => g.id === selectedId.value))
const filteredCatalog = computed(() => { const q=search.value.trim().toLowerCase(); return catalog.value.filter(g => !q || `${g.name} ${g.username}`.toLowerCase().includes(q)) })

function addGroup(group) {
  if (!configured.value.some(item => item.id === group.username)) configured.value.push({ id: group.username, name: group.name, forward_to: [], system_prompt_file: 'prompts/class_group.md', enabled: true, history_limit: 30, aggregation_seconds: 8, importance_threshold: 70 })
  selectedId.value = group.username
}
function removeGroup() { const i=configured.value.findIndex(g=>g.id===selectedId.value); if(i>=0) configured.value.splice(i,1); selectedId.value=configured.value[0]?.id||'' }
function addTarget(contact) { if(selected.value && !selected.value.forward_to.includes(contact.name)) selected.value.forward_to.push(contact.name); contactQuery.value=''; contacts.value=[] }
function removeTarget(target) { selected.value.forward_to = selected.value.forward_to.filter(item=>item!==target) }
async function save() { saving.value=true;error.value='';try{const result=await api.saveGroups(configured.value);toast.value=result.hot_reload?.applied?'规则已保存并热更新':'规则已保存';setTimeout(()=>toast.value='',2500)}catch(e){error.value=e.message}finally{saving.value=false} }
watch(contactQuery, value => { clearTimeout(contactTimer.value); if(!value.trim()){contacts.value=[];return} contactTimer.value=setTimeout(async()=>{try{contacts.value=(await api.contacts(value)).filter(x=>!x.is_group)}catch(e){error.value=e.message}},250) })
onMounted(async()=>{try{const [s,g,p]=await Promise.all([api.settings(),api.groups(),api.prompts()]);configured.value=JSON.parse(JSON.stringify(s.groups));catalog.value=g;prompts.value=p;if(configured.value.length)selectedId.value=configured.value[0].id}catch(e){error.value=e.message}finally{loading.value=false}})
</script>

<template>
  <div class="page-head"><div><h2>监听与转发</h2><p>先从完整微信群聊目录选择监听对象，再为每个群设置转发链路和判断策略。</p></div><div class="actions"><span class="pill success">支持热更新</span><button class="btn primary" :disabled="saving" @click="save">{{ saving?'保存中…':'保存全部规则' }}</button></div></div>
  <div v-if="error" class="error-box">{{ error }}</div><div v-if="loading" class="loading">正在读取微信完整群聊目录…</div>
  <div v-else class="rules-layout">
    <aside class="card catalog-panel">
      <div class="card-head"><h3>① 微信群聊目录</h3><span class="pill neutral">{{ catalog.length }} 个</span></div>
      <div class="catalog-search search"><input v-model="search" class="input" placeholder="搜索群名或内部 ID" /></div>
      <div class="catalog-list">
        <button v-for="group in filteredCatalog" :key="group.username" class="catalog-item" :class="{ active: selectedId===group.username, configured: configured.some(x=>x.id===group.username) }" @click="addGroup(group)">
          <span class="group-avatar">{{ group.name.slice(0,1) }}</span><span><strong>{{ group.name }}</strong><small>{{ group.message_count }} 条历史消息</small></span><i>{{ configured.some(x=>x.id===group.username)?'✓':'＋' }}</i>
        </button>
      </div>
    </aside>
    <section class="workspace">
      <div class="card configured-strip">
        <div><small>② 已配置监听</small><div class="rule-tabs"><button v-for="group in configured" :key="group.id" :class="{active:selectedId===group.id}" @click="selectedId=group.id"><span class="status-dot" :class="{online:group.enabled}"></span>{{ group.name }}</button></div></div>
        <span class="pill success">{{ configured.length }} 条规则</span>
      </div>
      <div v-if="selected" class="card editor-card">
        <div class="card-head"><div><h3>③ 规则详情 · {{ selected.name }}</h3><small class="muted mono">{{ selected.id }}</small></div><button class="btn danger small" @click="removeGroup">移除监听</button></div>
        <div class="card-body">
          <div class="section-title"><span>A</span><h4>监听行为</h4></div>
          <div class="form-grid"><div class="field"><label>后台显示名称</label><input v-model="selected.name" class="input" /></div><div class="field"><label>监听状态</label><select v-model="selected.enabled" class="select"><option :value="true">启用</option><option :value="false">停用</option></select></div><div class="field"><label>初始历史条数</label><input v-model.number="selected.history_limit" type="number" min="1" max="200" class="input" /><small>Agent 首次判断随请求携带的最近消息。</small></div><div class="field"><label>消息聚合窗口（秒）</label><input v-model.number="selected.aggregation_seconds" type="number" min="0.1" max="60" step="0.5" class="input" /><small>连续消息会合并成一个事件。</small></div></div>
          <div class="divider"></div><div class="section-title"><span>B</span><h4>转发与澄清链路</h4></div>
          <div class="target-chips"><span v-for="target in selected.forward_to" :key="target">{{ target }}<button @click="removeTarget(target)">×</button></span><em v-if="!selected.forward_to.length">尚未选择 forward_to</em></div>
          <div class="contact-picker"><div class="search"><input v-model="contactQuery" class="input" placeholder="搜索联系人昵称、备注或微信号" /></div><div v-if="contacts.length" class="contact-results"><button v-for="contact in contacts" :key="contact.username" @click="addTarget(contact)"><span>{{ contact.name.slice(0,1) }}</span><div><strong>{{ contact.name }}</strong><small>{{ contact.nickname || contact.username }}</small></div><i>添加</i></button></div></div>
          <p class="hint">重要摘要和 Agent 的澄清问题都会发往上述联系人。澄清答复将自动写入本群长期记忆。</p>
          <div class="divider"></div><div class="section-title"><span>C</span><h4>重要性判断策略</h4></div>
          <div class="form-grid"><div class="field"><label>Prompt 模板</label><select v-model="selected.system_prompt_file" class="select"><option value="">无群专属 Prompt</option><option v-for="prompt in prompts" :key="prompt.name" :value="`prompts/${prompt.name}`">{{ prompt.name }}</option></select><small><RouterLink to="/prompts">前往 Prompt 管理器编辑内容</RouterLink></small></div><div class="field"><label>转发阈值：{{ selected.importance_threshold }}</label><input v-model.number="selected.importance_threshold" type="range" min="0" max="100" class="range" /><small>AI 分数低于阈值时，程序拒绝执行转发。</small></div></div>
        </div>
      </div>
      <div v-else class="card empty"><strong>从左侧选择一个群聊</strong>选择后将在这里配置转发对象、Prompt 和判断阈值。</div>
    </section>
  </div>
  <div v-if="toast" class="toast">{{ toast }}</div>
</template>

<style scoped>
.rules-layout{display:grid;grid-template-columns:330px minmax(0,1fr);gap:16px;align-items:start}.catalog-panel{position:sticky;top:105px;overflow:hidden}.catalog-search{padding:13px;border-bottom:1px solid var(--line)}.catalog-list{max-height:calc(100vh - 260px);overflow:auto;padding:7px}.catalog-item{width:100%;border:0;background:transparent;display:flex;align-items:center;gap:10px;padding:10px;border-radius:9px;text-align:left;color:var(--ink)}.catalog-item:hover,.catalog-item.active{background:#f0f7f3}.catalog-item.active{box-shadow:inset 3px 0 var(--green)}.catalog-item>span:nth-child(2){flex:1;min-width:0}.catalog-item strong,.catalog-item small{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.catalog-item strong{font-size:12px}.catalog-item small{font-size:10px;color:var(--muted);margin-top:3px}.catalog-item i{font-style:normal;color:#91a198}.catalog-item.configured i{color:var(--green)}.group-avatar{width:34px;height:34px;flex:0 0 auto;border-radius:10px;display:grid;place-items:center;background:#e9f2ed;color:#436452;font-weight:800}.workspace{display:flex;flex-direction:column;gap:14px;min-width:0}.configured-strip{padding:13px 16px;display:flex;align-items:center;justify-content:space-between}.configured-strip small{font-size:10px;color:var(--muted);font-weight:700}.rule-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}.rule-tabs button{border:1px solid var(--line);background:#fff;border-radius:8px;padding:6px 9px;font-size:11px;display:flex;align-items:center;gap:7px}.rule-tabs button.active{border-color:#9fd4b7;background:var(--green-soft);color:var(--green-dark)}.rule-tabs .status-dot{width:6px;height:6px;box-shadow:none}.target-chips{display:flex;gap:8px;flex-wrap:wrap;min-height:35px}.target-chips span{background:var(--green-soft);color:var(--green-dark);padding:7px 9px;border-radius:8px;font-size:12px;font-weight:700}.target-chips button{border:0;background:none;color:inherit;margin-left:7px}.target-chips em{font-size:12px;color:var(--muted);font-style:normal;padding:8px}.contact-picker{position:relative;margin-top:10px}.contact-results{position:absolute;z-index:8;left:0;right:0;top:43px;background:white;border:1px solid var(--line);border-radius:10px;box-shadow:0 14px 30px rgba(0,0,0,.13);padding:5px;max-height:260px;overflow:auto}.contact-results button{width:100%;display:flex;align-items:center;gap:10px;border:0;background:white;padding:8px;border-radius:7px;text-align:left}.contact-results button:hover{background:#f3f8f5}.contact-results button>span{width:30px;height:30px;border-radius:8px;background:#edf3ef;display:grid;place-items:center}.contact-results button div{flex:1}.contact-results strong,.contact-results small{display:block}.contact-results strong{font-size:12px}.contact-results small{font-size:10px;color:var(--muted)}.contact-results i{font-style:normal;color:var(--green);font-size:11px}.hint{font-size:11px;color:var(--muted);background:#f7faf8;border-left:3px solid #9bc9ae;padding:9px 11px;line-height:1.6}.range{accent-color:var(--green);width:100%;margin:8px 0}.editor-card .card-head>div small{display:block;margin-top:5px}.field a{color:var(--green-dark)}@media(max-width:980px){.rules-layout{grid-template-columns:1fr}.catalog-panel{position:static}.catalog-list{max-height:350px}}
</style>
