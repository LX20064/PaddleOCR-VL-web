<template>
  <div class="qp card" :class="{ folded }">
    <!-- 头部：标题 + 操作 + 统计 -->
    <div class="qp-head">
      <div class="qp-title" @click="foldable && (folded = !folded)">
        <span v-if="foldable" class="qp-arrow">{{ folded ? '▸' : '▾' }}</span>{{ title }}
        <span class="qp-count">{{ queue.length }} 个文件</span>
      </div>
      <div class="qp-actions">
        <el-button size="small" @click="$emit('add-files')"><el-icon style="margin-right:3px"><FolderAdd /></el-icon>添加文件</el-button>
        <el-button size="small" @click="$emit('add-folder', includeSub)"><el-icon style="margin-right:3px"><FolderOpened /></el-icon>添加文件夹</el-button>
        <el-checkbox v-model="includeSub" size="small">包括子文件夹</el-checkbox>
        <el-button size="small" :disabled="!selectedPath" @click="$emit('remove')"><el-icon style="margin-right:3px"><Delete /></el-icon>移除</el-button>
        <el-button size="small" :disabled="!queue.length" @click="$emit('clear')"><el-icon style="margin-right:3px"><Close /></el-icon>清空</el-button>
        <!-- 后端未就绪时仍可点击：startBatch 会自动拉起 OCR 服务（服务未启动点此按钮即触发启动） -->
        <el-button type="primary" size="small" :loading="store.running" :disabled="store.running || pendingCount === 0" @click="$emit('start')">
          <el-icon style="margin-right:3px"><VideoPlay /></el-icon>批量识别（{{ pendingCount }}）
        </el-button>
        <el-button size="small" :disabled="!store.running" @click="$emit('stop')">
          <el-icon style="margin-right:3px"><VideoPause /></el-icon>停止
        </el-button>
      </div>
      <div class="qp-stats">
        <span>完成 <b style="color:var(--ok)">{{ stats.done }}</b></span>
        <span>失败 <b style="color:var(--err)">{{ stats.fail }}</b></span>
        <span>已停止 <b>{{ stats.stopped }}</b></span>
        <span>总页 <b>{{ stats.pages }}</b></span>
        <span>耗时 <b>{{ stats.secs.toFixed(1) }}s</b></span>
      </div>
    </div>

    <!-- 队列列表 -->
    <div v-show="!folded" class="qp-body" @dragover.prevent @drop.prevent="$emit('drop', $event)" @dragenter.prevent>
      <div v-if="queue.length === 0" class="qp-empty">{{ emptyHint }}</div>
      <div v-else class="qp-list" :class="{ grid: gridMode }">
        <div
          v-for="it in queue" :key="it.path"
          class="qp-item" :class="['st-' + it.status, it.path === selectedPath ? 'active' : '']"
          @click="$emit('select', it.path)"
        >
          <span class="qp-item-name" :title="it.path">{{ it.name }}</span>
          <span class="qp-item-meta">{{ it.pages ? it.pages + ' 页' : '' }}</span>
          <span class="qp-item-status">{{ statusLabel(it.status) }}</span>
          <div v-if="it.status === 'run'" class="q-progress"><i :style="{ width: Math.round((it.progress || 0) * 100) + '%' }"></i></div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { store, statusLabel } from '../store'
import { FolderAdd, FolderOpened, Delete, Close, VideoPlay, VideoPause } from '@element-plus/icons-vue'

defineEmits(['add-files', 'add-folder', 'remove', 'clear', 'start', 'stop', 'select', 'drop'])

const props = defineProps({
  queue: Array,
  selectedPath: String,
  stats: Object,
  pendingCount: { type: Number, default: 0 },
  title: { type: String, default: '任务队列' },
  emptyHint: { type: String, default: '将图片或 PDF 拖到这里，或点击「添加文件」' },
  foldable: { type: Boolean, default: false },
  gridMode: { type: Boolean, default: false },
  startFolded: { type: Boolean, default: false },
})
const folded = ref(props.startFolded)
// 添加文件夹时是否包含子文件夹（默认不包含）
const includeSub = ref(false)
</script>

<style scoped>
.qp { display: flex; flex-direction: column; min-height: 0; flex-shrink: 0; }
.qp-head { display: flex; align-items: center; gap: 10px; padding: 6px 12px; flex-wrap: wrap; }
.qp-title { display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; cursor: pointer; user-select: none; }
.qp-arrow { display: inline-block; width: 14px; }
.qp-count { font-size: 12px; color: var(--muted); font-weight: 400; }
.qp-actions { display: flex; gap: 6px; flex: 1; justify-content: flex-end; flex-wrap: wrap; }
.qp-stats { display: flex; gap: 12px; font-size: 12px; color: var(--muted); }
.qp-body { border-top: 1px solid var(--border); min-height: 0; display: flex; flex-direction: column; }
.qp-empty {
  margin: 6px 12px 10px; flex: 1; display: flex; align-items: center; justify-content: center;
  color: var(--muted); font-size: 12px; border: 2px dashed var(--border); border-radius: 8px; min-height: 60px;
}
.qp-list { overflow-y: auto; padding: 6px 12px 10px; display: flex; flex-direction: column; gap: 5px; }
.qp-list.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 6px; }
.qp-item {
  display: flex; align-items: center; gap: 8px; padding: 6px 10px; border-radius: 8px;
  background: rgba(0, 0, 0, .04); cursor: pointer; border: 1px solid transparent; flex-wrap: wrap;
}
.qp-item:hover { background: rgba(128, 134, 148, 0.10); }
.qp-item.active { border-color: var(--accent); background: var(--accent-soft); }
.qp-item-name { flex: 1; min-width: 0; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.qp-item-meta { font-size: 11px; color: var(--muted); }
.qp-item-status { font-size: 11px; width: 44px; text-align: right; }
.qp-item .q-progress { flex-basis: 100%; }
.st-done .qp-item-status { color: var(--ok); }
.st-fail .qp-item-status { color: var(--err); }
.st-run .qp-item-status { color: var(--accent); }
.st-stop .qp-item-status { color: #909399; }
</style>
