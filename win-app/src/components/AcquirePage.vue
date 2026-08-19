<template>
  <div class="acquire">
    <!-- 顶部：来源切换（分段控件） -->
    <div class="card acq-tabs">
      <div class="seg">
        <button
          v-for="t in tabs" :key="t.key"
          :class="{ active: activeTab === t.key }"
          @click="activeTab = t.key"
        >
          <el-icon style="margin-right:5px;vertical-align:-2px"><component :is="t.icon" /></el-icon>{{ t.label }}
        </button>
      </div>
    </div>

    <!-- 来源工作区 -->
    <div class="acq-body">
      <!-- 扫描仪 -->
      <div v-show="activeTab === 'scanner'" class="acq-pane">
        <div class="card acq-console">
          <ScanConsole @scan-done="onScanDone" />
        </div>
        <div class="card acq-tips">
          <div class="tips-title">扫描说明</div>
          <ul class="tips-list">
            <li>扫描完成后，页面自动加入下方「已获取页面」</li>
            <li>ADF（送纸器）支持多页连续扫描，开启「自动」扫至纸空</li>
            <li>开启「增强」自动去白边 / 去底色</li>
            <li>扫描完成后点击底部「识别全部」直接进行 OCR</li>
          </ul>
        </div>
      </div>

      <!-- 摄像头 -->
      <div v-show="activeTab === 'camera'" class="acq-pane">
        <CameraCapture @parse="onCamParse" />
      </div>
    </div>

    <!-- 底部：已获取页面托盘 -->
    <div class="card tray">
      <div class="tray-head">
        <span class="tray-title">已获取页面 <span class="tray-count">{{ tray.length }}</span></span>
        <span style="flex:1"></span>
        <el-button type="primary" size="small" :disabled="!tray.length" @click="sendAllToOcr">
          <el-icon style="margin-right:3px"><VideoPlay /></el-icon>识别全部
        </el-button>
        <el-button size="small" text :disabled="!tray.length" @click="tray.splice(0)">清空</el-button>
      </div>
      <div v-if="tray.length" class="tray-strip">
        <div v-for="(it, i) in tray" :key="it.path" class="tray-item" @click="openPreview(i)">
          <div class="tray-thumb">
            <img v-if="it.uri" :src="it.uri" />
            <div v-else class="tray-file">
              <el-icon :size="24"><Document /></el-icon>
              <span>{{ fileExt(it.path) }}</span>
            </div>
          </div>
          <span class="tray-name" :title="it.path">{{ it.name }}</span>
          <button class="tray-x" title="移除" @click.stop="tray.splice(i, 1)">×</button>
        </div>
      </div>
      <div v-else class="tray-empty">扫描或拍照的页面会出现在这里，可批量识别</div>
    </div>

    <!-- 页面预览对话框 -->
    <el-dialog v-model="previewOpen" :title="previewItem?.name" width="min(760px, 86vw)">
      <div class="pv-body">
        <img v-if="previewUri" :src="previewUri" class="pv-img" />
        <div v-else class="pv-file">
          <el-icon :size="42"><Document /></el-icon>
          <div style="word-break:break-all;font-size:12px;color:var(--muted)">{{ previewItem?.path }}</div>
        </div>
      </div>
      <template #footer>
        <el-button @click="removePreview">从列表移除</el-button>
        <el-button type="primary" @click="sendPreviewToOcr">识别此页</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { store, log } from '../store'
import { Printer, Camera, Document, VideoPlay } from '@element-plus/icons-vue'
import ScanConsole from './ScanConsole.vue'
import CameraCapture from './CameraCapture.vue'

const props = defineProps({ tab: { type: String, default: 'scanner' } })
const emit = defineEmits(['navigate'])

const tabs = [
  { key: 'scanner', label: '扫描仪', icon: Printer },
  { key: 'camera', label: '摄像头', icon: Camera },
]
const activeTab = ref(props.tab)
watch(() => props.tab, (v) => { if (v && tabs.some((t) => t.key === v)) activeTab.value = v })

const tray = store.acquireTray

function isImage(p) { return !!p && /\.(jpg|jpeg|png|bmp|tif|tiff|webp)$/i.test(p) }
function fileExt(p) { const m = (p || '').match(/\.([^.]+)$/); return m ? m[1].toUpperCase() : 'FILE' }

// ---- 扫描完成：入托盘 ----
async function onScanDone(r) {
  if (!r || !r.ok) return
  let added = 0
  if (r.pages?.length) {
    for (const p of r.pages) {
      const uri = await window.api.imageDataUri(p)
      tray.push({ path: p, name: p.split(/[\\/]/).pop(), source: 'scan', uri })
      added++
    }
  } else if (r.file) {
    tray.push({ path: r.file, name: r.file.split(/[\\/]/).pop(), source: 'scan', uri: null })
    added = 1
  }
  ElMessage.success(`扫描完成，已加入 ${added} 页`)
}

// ---- 摄像头联动 ----
function onCamParse() { emit('navigate', 'doc') }

// ---- 托盘操作 ----
function sendAllToOcr() {
  store.ocrInbox.push(...tray.map((t) => t.path))
  log(`▶ 已送出 ${tray.length} 个页面到文档识别`)
  tray.splice(0)
  emit('navigate', 'doc')
}

// ---- 预览 ----
const previewOpen = ref(false)
const previewIdx = ref(-1)
const previewUri = ref('')
const previewItem = computed(() => tray[previewIdx.value] || null)

async function openPreview(i) {
  previewIdx.value = i
  const it = tray[i]
  previewUri.value = it?.uri || ''
  if (it && !previewUri.value && isImage(it.path)) {
    previewUri.value = await window.api.imageDataUri(it.path) || ''
  }
  previewOpen.value = true
}
function removePreview() {
  if (previewIdx.value >= 0) tray.splice(previewIdx.value, 1)
  previewOpen.value = false
}
function sendPreviewToOcr() {
  const it = previewItem.value
  if (!it) return
  store.ocrInbox.push(it.path)
  tray.splice(previewIdx.value, 1)
  previewOpen.value = false
  emit('navigate', 'doc')
}
</script>

<style scoped>
.acquire { display: flex; flex-direction: column; gap: 10px; height: 100%; min-height: 0; }

/* 来源切换 */
.acq-tabs { padding: 8px 12px; flex: none; }
.seg { display: inline-flex; gap: 4px; background: rgba(128,134,148,0.10); padding: 3px; border-radius: 9px; }
.seg button {
  border: none; background: none; padding: 6px 20px; border-radius: 7px; cursor: pointer;
  font-size: 13px; color: var(--muted); transition: all .15s;
}
.seg button.active { background: var(--card-solid); color: var(--accent); font-weight: 600; box-shadow: 0 1px 3px rgba(0,0,0,.12); }

/* 工作区 */
.acq-body { flex: 1; min-height: 0; }
.acq-pane { display: flex; gap: 10px; height: 100%; min-height: 0; }
.acq-console { flex: 0 0 360px; padding: 12px; overflow-y: auto; }
.acq-tips { flex: 1; padding: 18px 22px; overflow-y: auto; }
.tips-title { font-size: 14px; font-weight: 600; margin-bottom: 10px; }
.tips-list { margin: 0; padding-left: 18px; color: var(--muted); font-size: 13px; line-height: 2.1; }

/* 托盘 */
.tray { flex: none; }
.tray-head { display: flex; align-items: center; gap: 8px; padding: 8px 12px; }
.tray-title { font-size: 13px; font-weight: 600; }
.tray-count { font-size: 12px; color: var(--muted); font-weight: 400; }
.tray-strip { display: flex; gap: 8px; overflow-x: auto; padding: 0 12px 10px; }
.tray-item {
  position: relative; flex: none; width: 108px; cursor: pointer;
  border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
  background: rgba(0,0,0,.03); transition: border-color .15s;
}
.tray-item:hover { border-color: var(--accent); }
.tray-thumb { height: 84px; display: flex; align-items: center; justify-content: center; overflow: hidden; }
.tray-thumb img { width: 100%; height: 100%; object-fit: cover; }
.tray-file { display: flex; flex-direction: column; align-items: center; gap: 4px; color: var(--muted); font-size: 10px; font-weight: 700; }
.tray-name { display: block; font-size: 11px; padding: 4px 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tray-x {
  position: absolute; top: 3px; right: 3px; width: 18px; height: 18px; border-radius: 50%;
  border: none; background: rgba(0,0,0,.45); color: #fff; font-size: 12px; line-height: 1;
  cursor: pointer; display: none;
}
.tray-item:hover .tray-x { display: block; }
.tray-empty {
  margin: 0 12px 10px; padding: 16px; text-align: center;
  color: var(--muted); font-size: 12px; border: 2px dashed var(--border); border-radius: 8px;
}

/* 预览对话框 */
.pv-body { display: flex; align-items: center; justify-content: center; min-height: 200px; }
.pv-img { max-width: 100%; max-height: 60vh; border-radius: 8px; }
.pv-file { display: flex; flex-direction: column; align-items: center; gap: 10px; color: var(--muted); }
</style>
