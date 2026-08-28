import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import DashboardView from './views/DashboardView.vue'
import RulesView from './views/RulesView.vue'
import PromptsView from './views/PromptsView.vue'
import MemoriesView from './views/MemoriesView.vue'
import ClarificationsView from './views/ClarificationsView.vue'
import OperationsView from './views/OperationsView.vue'
import './styles.css'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: DashboardView, meta: { title: '运行概览', section: '总览' } },
    { path: '/rules', component: RulesView, meta: { title: '监听与转发', section: '消息策略' } },
    { path: '/prompts', component: PromptsView, meta: { title: 'Prompt 管理', section: '消息策略' } },
    { path: '/memories', component: MemoriesView, meta: { title: '长期记忆', section: '知识与确认' } },
    { path: '/clarifications', component: ClarificationsView, meta: { title: '待确认事项', section: '知识与确认' } },
    { path: '/operations', component: OperationsView, meta: { title: '日程与审计', section: '系统' } },
  ],
})

createApp(App).use(router).mount('#app')
