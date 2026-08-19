<template>
  <div class="doctools">
    <!-- 自动扫描工具条：勾选仅作为「识别完成后自动保存」设置，不自动触发识别；
         识别需点「批量识别」；批量进行中锁定整个设置栏 -->
    <div class="auto-bar">
      <el-switch
        v-model="q.autoScan.value"
        size="small"
        active-text="自动扫描"
        inactive-text="手动"
        :disabled="store.running || !q.autoFormats.value.length"
      />
      <span class="auto-label">自动保存格式</span>
      <el-checkbox-group v-model="q.autoFormats.value" size="small" :disabled="store.running">
        <el-checkbox value="docx">Word</el-checkbox>
        <el-checkbox value="html">HTML</el-checkbox>
        <el-checkbox value="json">JSON</el-checkbox>
        <el-checkbox value="zip_md">Markdown</el-checkbox>
      </el-checkbox-group>
      <span class="auto-tip">开启后：识别完成自动按勾选格式保存到 OCR 结果目录（识别本身需点「批量识别」开始）</span>
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
import { store } from '../store'
import QueuePanel from './QueuePanel.vue'
import ResultViewer from './ResultViewer.vue'
import { useOcrQueue } from '../composables/useOcrQueue'

const q = useOcrQueue()

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
.dt-row { display: flex; gap: 10px; flex: 1; min-height: 0; }
.dt-queue { flex: 0 0 400px; min-width: 0; }
.dt-queue :deep(.qp-body) { flex: 1; overflow: hidden; }
.dt-queue :deep(.qp-list) { height: 100%; }
.dt-result { flex: 1; min-width: 0; min-height: 0; }
</style>
