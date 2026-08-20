<template>
  <div class="doctools">
    <!-- 自动扫描工具条：勾选仅作为「识别完成后自动保存」设置，不自动触发识别；
         识别需点「批量识别」；批量进行中锁定整个设置栏；
         未勾选任何导出方式时自动扫描不可用，点击开关会提示先选择导出方式 -->
    <div class="auto-bar">
      <el-switch
        v-model="q.autoScan.value"
        size="small"
        active-text="自动扫描"
        inactive-text="手动"
        :disabled="store.running"
        @change="onAutoScanChange"
      />
      <span class="auto-label">自动保存格式</span>
      <el-checkbox-group v-model="q.autoFormats.value" size="small" :disabled="store.running">
        <el-checkbox value="docx">Word</el-checkbox>
        <el-checkbox value="html">HTML</el-checkbox>
        <el-checkbox value="json">JSON</el-checkbox>
        <el-checkbox value="zip_md">Markdown</el-checkbox>
      </el-checkbox-group>
      <span class="auto-tip">开启后：识别完成自动按勾选格式保存到 OCR 结果目录（识别本身需点「批量识别」开始）</span>
      <!-- 最右侧：快速打开保存结果文件夹（目录取自设置中的 OCR 输出目录） -->
      <el-tooltip content="打开 OCR 结果保存文件夹" placement="top">
        <el-button class="open-dir-btn" size="small" plain :disabled="store.running" @click="openResultDir">
          <el-icon style="margin-right:3px"><FolderOpened /></el-icon>结果文件夹
        </el-button>
      </el-tooltip>
    </div>

    <div class="dt-row">
      <!-- 左：任务队列 -->
      <QueuePanel
        class="dt-queue"
        title="识别队列"
        empty-hint="添加文件或文件夹，批量识别为 Word / HTML / Markdown；也可直接拖入"
        :queue="q.queue"
        :selected-path="q.selectedPath.value"
        :stats="q.stats.value"
        :pending-count="q.pendingCount.value"
        @add-files="q.addFiles"
        @add-folder="q.addFolder"
        @remove="q.removeSelected"
        @clear="q.clearQueue"
        @start="q.startBatch"
        @stop="q.stopBatch"
        @select="q.select"
        @drop="q.onDrop"
      />

      <!-- 右：识别结果 -->
      <div class="card dt-result">
        <ResultViewer
          :item="q.selectedItem.value"
          :result="q.selectedResult.value"
          pending-hint="尚未识别，点击左侧「批量识别」开始"
          @export="q.exportMode"
          @open-download="q.openDownload"
          @load-images="q.loadSelectedImages"
        />
      </div>
    </div>
  </div>
</template>

<script setup>
import { watch } from 'vue'
import { ElMessage } from 'element-plus'
import { FolderOpened } from '@element-plus/icons-vue'
import { store } from '../store'
import QueuePanel from './QueuePanel.vue'
import ResultViewer from './ResultViewer.vue'
import { useOcrQueue } from '../composables/useOcrQueue'

const q = useOcrQueue()

// 自动扫描开关：未勾选任何导出方式（自动保存格式）时不允许开启，提示先选择导出方式
async function onAutoScanChange(val) {
  if (val && !q.autoFormats.value.length) {
    q.autoScan.value = false
    ElMessage.warning('请选择导出方式')
    return
  }
  if (val) {
    // 开启时明确告知保存位置，避免「识别完成了但不知道结果在哪」的困惑
    try {
      const dirs = await window.api.getDefaultDirs()
      ElMessage.success(`自动扫描已开启：识别完成后自动保存到 ${dirs?.ocrDir || 'OCR 结果目录'}`)
    } catch (e) {
      ElMessage.success('自动扫描已开启：识别完成后自动保存到 OCR 结果目录')
    }
  }
}

// 快速打开保存结果文件夹：目录取设置中的 OCR 输出目录（与自动扫描/手动导出的落盘位置一致）
async function openResultDir() {
  try {
    const dirs = await window.api.getDefaultDirs()
    const dir = dirs?.ocrDir
    if (!dir) { ElMessage.warning('未配置 OCR 结果目录'); return }
    const res = await window.api.openDir(dir)
    if (!res?.ok) ElMessage.error(`打开结果文件夹失败：${res?.error || '未知错误'}`)
  } catch (e) {
    ElMessage.error(`打开结果文件夹失败：${e.message}`)
  }
}

// 接收「获取文档 / 摄像头」送来的待识别文件：入队并自动开始批量识别
watch(() => store.ocrInbox.length, () => {
  if (!store.ocrInbox.length) return
  const paths = store.ocrInbox.splice(0)
  q.pushFiles(paths, { select: true })
  // 空闲时自动开始：startBatch 内部会确保 OCR 服务就绪（未启动则启动一次、预热中则等待），
  // 正在批量时仅入队，由当前循环自动续跑。
  if (!store.running) q.startBatch()
}, { immediate: true })
</script>

<style scoped>
.doctools { display: flex; flex-direction: column; gap: 10px; height: 100%; min-height: 0; }
.auto-bar {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 6px 12px; border: 1px solid var(--border); border-radius: 10px;
  background: var(--card);
}
.auto-label { font-size: 12px; color: var(--muted); }
.auto-tip { font-size: 12px; color: var(--muted); opacity: .85; }
.open-dir-btn { margin-left: auto; }
.dt-row { display: flex; gap: 10px; flex: 1; min-height: 0; }
.dt-queue { flex: 0 0 400px; min-width: 0; }
.dt-queue :deep(.qp-body) { flex: 1; overflow: hidden; }
.dt-queue :deep(.qp-list) { height: 100%; }
.dt-result { flex: 1; min-width: 0; min-height: 0; }
</style>
