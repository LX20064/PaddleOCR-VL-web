<template>
  <div class="home">
    <!-- 顶部欢迎 + 服务状态 -->
    <div class="card home-hero">
      <div class="hero-left">
        <div class="hero-title">PaddleOCR-VL 扫描识别</div>
        <div class="hero-sub">扫描 · 拍照 · 批量识别 · 导出 Word / PDF / Markdown</div>
      </div>
      <div class="hero-svc">
        <span class="backend-chip" :class="chipClass">
          <span class="dot"></span>{{ chipText }}
        </span>
        <el-button v-if="!store.backend?.ready && !store.backend?.quantizing" type="primary" size="small" :loading="starting" @click="startSvc">
          启动 OCR 服务
        </el-button>
        <el-button v-if="!store.backend?.ready && store.backend?.quantizing" type="primary" size="small" plain disabled>
          正在量化模型…
        </el-button>
      </div>
    </div>

    <!-- 快捷入口 -->
    <div class="home-grid">
      <div v-for="t in tiles" :key="t.title" class="card tile" @click="go(t)">
        <div class="tile-ic" :style="{ background: t.bg }">
          <el-icon :size="24" :color="t.color"><component :is="t.icon" /></el-icon>
        </div>
        <div class="tile-title">{{ t.title }}</div>
        <div class="tile-desc">{{ t.desc }}</div>
      </div>
    </div>

    <!-- 使用流程提示 -->
    <div class="card home-flow">
      <div class="flow-title">推荐工作流</div>
      <div class="flow-steps">
        <div class="flow-step"><span class="flow-no">1</span>扫描和拍照<span class="flow-tip">扫描 / 拍照 / 导入</span></div>
        <div class="flow-arrow">→</div>
        <div class="flow-step"><span class="flow-no">2</span>文档识别<span class="flow-tip">批量 OCR</span></div>
        <div class="flow-arrow">→</div>
        <div class="flow-step"><span class="flow-no">3</span>导出结果<span class="flow-tip">Word / PDF / Markdown</span></div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { store } from '../store'
import { useBackendStatus } from '../composables/useBackendStatus'
import { Printer, Camera, Document, Files } from '@element-plus/icons-vue'

const emit = defineEmits(['navigate'])

const starting = ref(false)
const { chipClass, chipText } = useBackendStatus()

const tiles = [
  { title: '扫描文档', desc: '连接扫描仪，扫描纸质文档', icon: Printer, color: '#2563eb', bg: 'rgba(37,99,235,.12)', nav: ['acquire', 'scanner'] },
  { title: '拍照识别', desc: '用摄像头拍摄文档', icon: Camera, color: '#7c3aed', bg: 'rgba(124,58,237,.12)', nav: ['acquire', 'camera'] },
  { title: '批量识别', desc: '多文件批量 OCR，导出 Word', icon: Document, color: '#16a34a', bg: 'rgba(22,163,74,.12)', nav: ['doc'] },
  { title: '工具箱', desc: '图片合并 PDF、批量转格式', icon: Files, color: '#db2777', bg: 'rgba(219,39,119,.12)', nav: ['convert'] },
]

function go(t) {
  emit('navigate', t.nav[0], t.nav[1])
}

async function startSvc() {
  starting.value = true
  try {
    const r = await window.api.startBackend()
    if (!r.ok) ElMessage.error(r.error || '启动失败')
  } catch (e) {
    ElMessage.error('启动失败：' + (e.message || e))
  } finally {
    starting.value = false
  }
}
</script>

<style scoped>
.home { display: flex; flex-direction: column; gap: 12px; height: 100%; min-height: 0; overflow-y: auto; }

.home-hero {
  display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
  padding: 26px 28px;
}
.hero-left { flex: 1; min-width: 240px; }
.hero-title { font-size: 22px; font-weight: 700; letter-spacing: .5px; }
.hero-sub { font-size: 13px; color: var(--muted); margin-top: 6px; }
.hero-svc { display: flex; align-items: center; gap: 10px; }

.home-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px;
}
.tile {
  padding: 20px; cursor: pointer; transition: all .18s;
  display: flex; flex-direction: column; gap: 8px;
}
.tile:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(15,23,42,.12); border-color: var(--accent); }
.tile-ic {
  width: 44px; height: 44px; border-radius: 12px;
  display: inline-flex; align-items: center; justify-content: center;
}
.tile-title { font-size: 15px; font-weight: 600; }
.tile-desc { font-size: 12px; color: var(--muted); line-height: 1.6; }

.home-flow { padding: 18px 24px; }
.flow-title { font-size: 13px; font-weight: 600; color: var(--muted); margin-bottom: 12px; }
.flow-steps { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.flow-step {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 16px; border-radius: 10px; background: rgba(128,134,148,0.08);
  font-size: 13px; font-weight: 500;
}
.flow-no {
  width: 20px; height: 20px; border-radius: 50%;
  background: var(--accent); color: #fff; font-size: 12px; font-weight: 700;
  display: inline-flex; align-items: center; justify-content: center;
}
.flow-tip { font-size: 11px; color: var(--muted); font-weight: 400; }
.flow-arrow { color: var(--muted); font-size: 16px; }
</style>
