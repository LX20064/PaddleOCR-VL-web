<template>
  <div class="titlebar">
    <span class="logo">VL</span>
    <span class="title">PaddleOCR-VL 扫描</span>
    <span class="spacer"></span>
    <span class="tb-item" style="margin-right:10px">
      <span class="backend-chip" :class="chipClass" :title="chipTitle" @click="onStart">
        <span class="dot"></span>{{ chipText }}
      </span>
    </span>
    <span class="tb-item" style="margin-right:6px">
      <span class="backend-chip" :title="gpuText">{{ gpuText }}</span>
    </span>
    <span class="tb-item">
      <button class="tb-btn" :disabled="store.running" :title="settingsBtnTitle" @click="$emit('open-settings')">
        <el-icon :size="17"><Setting /></el-icon>
      </button>
    </span>
    <span class="tb-item">
      <button class="tb-btn" title="最小化" @click="winMin">
        <el-icon><Minus /></el-icon>
      </button>
    </span>
    <span class="tb-item">
      <button class="tb-btn" title="最大化" @click="winMax">
        <el-icon><FullScreen /></el-icon>
      </button>
    </span>
    <span class="tb-item">
      <button class="tb-btn close" title="关闭" @click="winClose">
        <el-icon><Close /></el-icon>
      </button>
    </span>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { ElMessage } from 'element-plus'
import { store } from '../store'
import { useBackendStatus } from '../composables/useBackendStatus'
import { Setting, Minus, FullScreen, Close } from '@element-plus/icons-vue'

const { chipClass, chipText, chipTitle } = useBackendStatus()
const gpuText = computed(() => {
  const g = store.sys?.gpu
  return g ? `${g.name} · ${g.mem}` : 'GPU 未检测到'
})
// 识别进行中冻结设置入口：防止中途修改识别参数导致同批次内参数不一致
const settingsBtnTitle = computed(() =>
  store.running ? '批量识别进行中，暂不可修改设置' : '设置'
)

async function onStart() {
  if (store.backend?.ready || store.backend?.quantizing) return
  try {
    const r = await window.api.startBackend()
    if (!r.ok) {
      ElMessage.error(r.error || '启动失败')
    }
  } catch (e) {
    ElMessage.error('启动失败：' + (e.message || e))
  }
}

// 窗口控制（模板内联调用 window.api 在 script setup 中会解析失败，统一走 script 函数）
function winMin() { window.api.windowMin() }
function winMax() { window.api.windowMax() }
function winClose() { window.api.windowClose() }
</script>

<style scoped>
.tb-btn:disabled {
  opacity: .4;
  cursor: not-allowed;
}
</style>
