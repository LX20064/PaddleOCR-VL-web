<template>
  <nav class="rail">
    <button
      v-for="item in items" :key="item.key"
      class="rail-item" :class="{ active: active === item.key }"
      @click="change(item.key)"
    >
      <el-icon :size="21"><component :is="item.icon" /></el-icon>
      <span class="rail-label">{{ item.label }}</span>
    </button>
    <div class="rail-spacer"></div>
    <button class="rail-item" :disabled="store.running" :title="settingsBtnTitle" @click="$emit('open-settings')">
      <el-icon :size="21"><Setting /></el-icon>
      <span class="rail-label">设置</span>
    </button>
    <div class="rail-ver">v{{ store.sys?.appVersion || '0.1.0' }}</div>
  </nav>
</template>

<script setup>
import { computed } from 'vue'
import { store } from '../store'
import { HomeFilled, FolderAdd, Document, Files, Setting } from '@element-plus/icons-vue'

defineProps({ active: String })
const emit = defineEmits(['change', 'open-settings'])

// 识别进行中冻结设置入口：防止中途修改识别参数导致同批次内参数不一致
const settingsBtnTitle = computed(() =>
  store.running ? '批量识别进行中，暂不可修改设置' : '设置'
)

const items = [
  { key: 'home', label: '首页', icon: HomeFilled },
  { key: 'acquire', label: '扫描和拍照', icon: FolderAdd },
  { key: 'doc', label: '文档识别', icon: Document },
  { key: 'convert', label: '工具箱', icon: Files },
]

function change(m) {
  localStorage.setItem('app.module', m)
  emit('change', m)
}
</script>

<style scoped>
.rail {
  flex: none; width: 84px;
  display: flex; flex-direction: column; align-items: stretch; gap: 4px;
  padding: 10px 8px;
  border-radius: 12px;
  background: var(--card);
  border: 1px solid var(--border);
  box-shadow: var(--shadow);
  backdrop-filter: blur(40px) saturate(2.2);
  -webkit-backdrop-filter: blur(40px) saturate(2.2);
}
.rail-item {
  display: flex; flex-direction: column; align-items: center; gap: 4px;
  padding: 10px 2px; border-radius: 10px;
  color: var(--muted); cursor: pointer; border: none; background: none;
  transition: all .16s;
}
.rail-item:hover { background: rgba(128, 134, 148, 0.10); color: var(--text); }
.rail-item:disabled { opacity: .4; cursor: not-allowed; }
.rail-item:disabled:hover { background: none; }
.rail-item.active { background: var(--accent-soft); color: var(--accent); }
.rail-label { font-size: 11px; font-weight: 500; }
.rail-item.active .rail-label { font-weight: 600; }
.rail-spacer { flex: 1; }
.rail-ver { font-size: 10px; color: var(--muted); text-align: center; opacity: .6; padding-top: 4px; }
</style>
