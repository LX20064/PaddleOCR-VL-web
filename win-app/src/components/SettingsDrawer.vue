<template>
  <el-drawer v-model="open" title="设置" size="460px" :with-header="true" class="settings-drawer">
    <el-tabs v-model="tab" class="settings-tabs">
      <!-- ===== 服务 ===== -->
      <el-tab-pane label="服务" name="service">
        <div class="settings-section">
          <div class="svc-card">
            <div style="flex:1">
              <div class="name">后端推理服务</div>
              <div class="desc">{{ backendDesc }}</div>
            </div>
            <el-button v-if="!store.backend?.ready" type="primary" size="small" @click="startSvc" :loading="starting">启动</el-button>
            <el-button v-else type="danger" size="small" plain @click="stopSvc">停止</el-button>
          </div>
          <div class="svc-card">
            <div style="flex:1">
              <div class="name">VLM 模型精度</div>
              <div class="desc">{{ precisionDesc }}</div>
            </div>
            <el-select v-model="precision" size="small" style="width:150px" @change="onPrecisionChange">
              <el-option label="FP16（默认·最高质量）" value="fp16" />
              <el-option label="Q8_0（高质量）" value="q8_0" />
              <el-option label="Q5_K_M（均衡）" value="q5_k_m" />
              <el-option label="Q4_K_M（最快省显存）" value="q4_k_m" />
            </el-select>
          </div>
          <div class="svc-card">
            <div style="flex:1">
              <div class="name">上下文长度</div>
              <div class="desc">llama-server 上下文大小</div>
            </div>
            <el-input-number v-model="ctxSize" :min="1024" :max="131072" :step="1024" size="small" style="width:150px" />
          </div>
          <div class="svc-card">
            <div style="flex:1">
              <div class="name">PDF 渲染 DPI</div>
              <div class="desc">PDF 转图识别清晰度（越高越慢）</div>
            </div>
            <el-input-number v-model="pdfRenderDpi" :min="72" :max="600" :step="10" size="small" style="width:120px" />
          </div>
          <div class="svc-card">
            <div style="flex:1">
              <div class="name">CUDA Graph</div>
              <div class="desc">关闭可省显存，但会慢一些</div>
            </div>
            <el-checkbox v-model="noCudaGraph">关闭 CUDA Graph</el-checkbox>
          </div>
          <div class="svc-card">
            <div style="flex:1">
              <div class="name">模型 / 环境目录</div>
              <div class="desc" style="word-break:break-all">{{ store.setup?.backendDir || '(未检测到离线包)' }}</div>
            </div>
            <el-button size="small" text @click="revealDir">打开</el-button>
          </div>
          <el-alert type="info" :closable="false" size="small">模型参数在下次启动 OCR 服务时生效</el-alert>
        </div>

        <div class="settings-section">
          <div class="sec-title">
            服务日志
            <el-button size="small" text style="float:right" @click="loadLogs">刷新</el-button>
          </div>
          <el-tabs v-model="logTab" size="small">
            <el-tab-pane label="API 日志" name="api">
              <textarea class="log-ta" readonly :value="logs.api"></textarea>
            </el-tab-pane>
            <el-tab-pane label="llama 日志" name="llama">
              <textarea class="log-ta" readonly :value="logs.llama"></textarea>
            </el-tab-pane>
          </el-tabs>
        </div>
      </el-tab-pane>

      <!-- ===== 识别参数 ===== -->
      <el-tab-pane label="识别参数" name="ocr">
        <div class="settings-section">
          <div class="sec-title">预处理与版面</div>
          <div class="opt-grid">
            <el-checkbox v-model="d.use_orientation">文档方向分类</el-checkbox>
            <el-checkbox v-model="d.use_unwarping">文本图像矫正</el-checkbox>
            <el-checkbox v-model="d.use_layout_mode">版面分析</el-checkbox>
            <el-checkbox v-model="d.use_merge_blocks">跨栏合并</el-checkbox>
          </div>
        </div>
        <div class="settings-section">
          <div class="sec-title">内容增强</div>
          <div class="opt-grid">
            <el-checkbox v-model="d.use_seal">印章识别</el-checkbox>
            <el-checkbox v-model="d.use_chart">图表解析</el-checkbox>
            <el-checkbox v-model="d.use_ocr_image_block">图像块内 OCR</el-checkbox>
            <el-checkbox v-model="d.use_format_block">块内容格式化</el-checkbox>
          </div>
        </div>
        <div class="settings-section">
          <div class="sec-title">VLM 生成参数</div>
          <div class="opt-grid">
            <div class="opt-row">
              <span class="opt-label">单页最大输出 token</span>
              <el-input-number :model-value="disp0(d.max_new_tokens)" :min="0" :max="65536" :step="1024" size="small" style="width:150px" placeholder="后端默认(16384)" @update:model-value="d.max_new_tokens = norm0($event)" />
              <span class="opt-hint">0=后端默认 16384</span>
            </div>
            <div class="opt-row">
              <span class="opt-label">最大像素总量</span>
              <el-input-number :model-value="disp0(d.max_pixels)" :min="0" :max="10000000" :step="100000" size="small" style="width:150px" placeholder="不限制" @update:model-value="d.max_pixels = norm0($event)" />
              <span class="opt-hint">0=不限制</span>
            </div>
            <div class="opt-row">
              <span class="opt-label">最小像素总量</span>
              <el-input-number :model-value="disp0(d.min_pixels)" :min="0" :max="10000000" :step="100000" size="small" style="width:150px" placeholder="不限制" @update:model-value="d.min_pixels = norm0($event)" />
              <span class="opt-hint">0=不限制</span>
            </div>
            <div class="opt-row">
              <span class="opt-label">重复惩罚</span>
              <el-input-number v-model="d.repetition_penalty" :min="0.1" :max="2" :step="0.05" :precision="2" size="small" style="width:100px" />
            </div>
          </div>
        </div>
        <div class="settings-section">
          <div class="sec-title">批量识别</div>
          <div class="opt-grid">
            <div class="opt-row">
              <span class="opt-label">最大并行识别数</span>
              <el-input-number v-model="maxParallel" :min="1" :max="8" :step="1" size="small" style="width:100px" />
              <span class="opt-hint">同时识别文件数（1=串行；显存 8GB 建议 ≤2）</span>
            </div>
          </div>
        </div>
        <div class="settings-section">
          <div class="sec-title">导出选项</div>
          <div class="opt-grid">
            <el-checkbox v-model="d.pdf_per_page">PDF 每页输出单独文件</el-checkbox>
            <el-checkbox v-model="d.export_chart">导出图表区域为图片</el-checkbox>
            <el-checkbox v-model="d.keep_source_dir">保持来源子目录结构</el-checkbox>
            <el-checkbox v-model="d.skip_existing">跳过已存在的结果文件</el-checkbox>
          </div>
        </div>
      </el-tab-pane>

      <!-- ===== 高级 ===== -->
      <el-tab-pane label="高级" name="advanced">
        <el-form label-position="top" size="small">
          <el-form-item label="图片默认保存位置">
            <el-input v-model="photoDir" placeholder="图片库/本机照片">
              <template #append>
                <el-button @click="pickPhotoDir">选择</el-button>
              </template>
            </el-input>
          </el-form-item>
          <el-form-item label="扫描结果保存位置">
            <el-input v-model="scanDir" placeholder="文档/已扫描的文档">
              <template #append>
                <el-button @click="pickScanDir">选择</el-button>
              </template>
            </el-input>
          </el-form-item>
          <el-form-item label="OCR 输出保存位置">
            <el-input v-model="ocrDir" placeholder="文档/OCR扫描结果">
              <template #append>
                <el-button @click="pickOcrDir">选择</el-button>
              </template>
            </el-input>
          </el-form-item>
          <el-form-item label="PDF 输出保存位置">
            <el-input v-model="pdfOutDir" placeholder="文档/PDF输出（图片转 PDF 的默认输出目录）">
              <template #append>
                <el-button @click="pickPdfOutDir">选择</el-button>
              </template>
            </el-input>
          </el-form-item>
          <el-form-item label="后端 API 地址">
            <el-input v-model="apiUrl" placeholder="http://127.0.0.1:8080/layout-parsing" />
          </el-form-item>
          <el-form-item label="请求超时（秒）">
            <el-input-number v-model="timeout" :min="60" :max="3600" :step="60" style="width:100%" />
          </el-form-item>
          <el-form-item label="VLM 推理设备（llama）">
            <el-select v-model="device" style="width:100%">
              <el-option label="自动（按显卡计算能力）" value="auto" />
              <el-option label="GPU（需计算能力 ≥ 7.5）" value="gpu" />
              <el-option label="CPU" value="cpu" />
            </el-select>
          </el-form-item>
          <el-form-item label="文档解析设备（Paddle）">
            <el-select v-model="paddleDevice" style="width:100%">
              <el-option label="自动（按显卡计算能力）" value="auto" />
              <el-option label="GPU（需计算能力 ≥ 7.5）" value="gpu" />
              <el-option label="CPU" value="cpu" />
            </el-select>
          </el-form-item>
          <el-checkbox v-model="keepServices">关闭应用后保留 OCR 服务运行</el-checkbox>
          <el-checkbox v-model="rememberModule">启动时打开上次所在页面</el-checkbox>
        </el-form>
        <el-button type="info" size="small" plain style="width:100%" @click="restoreDefaults">
          恢复默认值
        </el-button>
      </el-tab-pane>
    </el-tabs>

    <!-- 统一保存区：任何 tab 下修改后点这里一次性写入 -->
    <template #footer>
      <div class="settings-footer">
        <el-button @click="open = false">取消</el-button>
        <el-button type="primary" style="flex:1" @click="saveAll">保存设置</el-button>
      </div>
    </template>
  </el-drawer>
</template>

<script setup>
import { ref, reactive, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { store } from '../store'

const open = defineModel({ type: Boolean, default: false })
const tab = ref('service')
const starting = ref(false)
const precision = ref('fp16')
const apiUrl = ref('')
const timeout = ref(600)
const device = ref('auto')
const paddleDevice = ref('auto')
const keepServices = ref(false)
const rememberModule = ref(false)
const photoDir = ref('')
const scanDir = ref('')
const ocrDir = ref('')
const pdfOutDir = ref('')
const logTab = ref('api')
const logs = reactive({ api: '', llama: '' })
// 解析后的系统默认目录（getDefaultDirs 返回值），用于保存时判断是否要把默认路径“固化”进配置
const systemDirs = reactive({ photoDir: '', scanDir: '', ocrDir: '', pdfOutDir: '' })

const ctxSize = ref(32768)
const pdfRenderDpi = ref(288)
const noCudaGraph = ref(false)
const maxParallel = ref(1)
const d = reactive({
  use_seal: false, use_chart: false, use_orientation: false, use_unwarping: false,
  use_ocr_image_block: false, use_format_block: false,
  use_layout_mode: true, use_merge_blocks: true,
  pdf_per_page: false, export_chart: false,
  max_pixels: 0, min_pixels: 0, max_new_tokens: 16384, repetition_penalty: 1.0,
  keep_source_dir: false, skip_existing: false,
})

function loadSettingsIntoUI() {
  if (!store.settings) return
  precision.value = store.settings.precision || 'fp16'
  apiUrl.value = store.settings.apiUrl || ''
  timeout.value = store.settings.timeout || 600
  device.value = store.settings.device || 'auto'
  paddleDevice.value = store.settings.paddleDevice || 'auto'
  keepServices.value = !!store.settings.keepServicesAfterQuit
  rememberModule.value = !!store.settings.rememberLastModule
  ctxSize.value = store.settings.ctxSize || 32768
  pdfRenderDpi.value = store.settings.pdfRenderDpi || 288
  noCudaGraph.value = !!store.settings.noCudaGraph
  maxParallel.value = Number(store.settings.maxParallel) || 1
  Object.assign(d, store.settings.defaults || {})
}

// 0 表示「后端默认 / 不限制」：值为 0 时输入框显示占位文字而非 0，更直观；保存语义不变
const disp0 = (v) => (Number(v) > 0 ? Number(v) : undefined)
const norm0 = (v) => (Number(v) > 0 ? Math.round(Number(v)) : 0)

watch(open, async (v) => {
  if (!v || !store.settings) return
  loadSettingsIntoUI()
  // 从主进程获取实际默认路径（会自动创建目录）
  const dirs = await window.api.getDefaultDirs()
  Object.assign(systemDirs, dirs)
  photoDir.value = store.settings.photoDir || dirs.photoDir || ''
  scanDir.value = store.settings.scanDir || dirs.scanDir || ''
  ocrDir.value = store.settings.ocrDir || dirs.ocrDir || ''
  pdfOutDir.value = store.settings.pdfOutDir || dirs.pdfOutDir || ''
  loadLogs()
})

async function pickPhotoDir() {
  const dir = await window.api.chooseDirectory()
  if (dir) photoDir.value = dir
}
async function pickScanDir() {
  const dir = await window.api.chooseDirectory()
  if (dir) scanDir.value = dir
}
async function pickOcrDir() {
  const dir = await window.api.chooseDirectory()
  if (dir) ocrDir.value = dir
}
async function pickPdfOutDir() {
  const dir = await window.api.chooseDirectory()
  if (dir) pdfOutDir.value = dir
}

const backendDesc = computed(() => {
  const b = store.backend
  if (!b) return '检测中…'
  if (b.ready) return 'llama-server + API 均已就绪'
  if (b.llamaUp) return 'llama-server 已启动，等待 API…'
  if (b.apiUp) return 'API 已启动，等待 llama…'
  return '未启动'
})

const precisionDesc = computed(() => {
  if (store.backend?.quantizing) return `正在自动量化 ${String(store.backend.quantizing).toUpperCase()}…`
  const m = (store.backend?.models || []).find((x) => x.precision === precision.value)
  if (!m) return '—'
  if (m.ready) return '模型已就绪'
  if (m.quantizable) return '首次使用时自动量化（基于 FP16 基座）'
  return 'FP16 基座缺失，请先准备模型'
})

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
async function stopSvc() { await window.api.stopBackend() }

function onPrecisionChange() {
  ElMessage.info('精度将在保存后、下次启动 OCR 服务时生效')
}

async function loadLogs() {
  logs.api = (await window.api.getBackendLog('api')) || '(暂无日志)'
  logs.llama = (await window.api.getBackendLog('llama')) || '(暂无日志)'
}

function revealDir() {
  window.api.revealPath(store.setup?.backendDir || '')
}

// 目录字段归一化：原始设置为空（使用系统默认）且当前值仍等于解析默认 → 写回空串，
// 继续跟随系统默认，避免把「Pictures/本机照片」这类默认路径固化进配置文件。
function normDir(val, key) {
  const cur = store.settings?.[key] || ''
  const def = systemDirs[key]
  if (!cur && val === def) return ''
  return val || ''
}

async function saveAll() {
  try {
    const merged = await window.api.saveSettings({
      apiUrl: apiUrl.value,
      timeout: timeout.value,
      device: device.value,
      paddleDevice: paddleDevice.value,
      keepServicesAfterQuit: keepServices.value,
      rememberLastModule: rememberModule.value,
      ctxSize: ctxSize.value,
      pdfRenderDpi: pdfRenderDpi.value,
      noCudaGraph: noCudaGraph.value,
      maxParallel: maxParallel.value,
      precision: precision.value,
      photoDir: normDir(photoDir.value, 'photoDir'),
      scanDir: normDir(scanDir.value, 'scanDir'),
      ocrDir: normDir(ocrDir.value, 'ocrDir'),
      pdfOutDir: normDir(pdfOutDir.value, 'pdfOutDir'),
      defaults: { ...d },
    })
    store.settings = merged
    ElMessage.success('设置已保存')
  } catch (e) {
    ElMessage.error('保存设置失败：' + (e.message || e))
  }
}

async function restoreDefaults() {
  // 传 null 表示恢复默认：主进程 saveSettings 会把对应键从配置中删除（不写入 null）
  await window.api.saveSettings({
    apiUrl: null, timeout: null, device: null, paddleDevice: null,
    keepServicesAfterQuit: null, rememberLastModule: null,
    ctxSize: null, pdfRenderDpi: null, noCudaGraph: null, maxParallel: null,
    precision: null, photoDir: null, scanDir: null, ocrDir: null, pdfOutDir: null,
    defaults: null,
  })
  // 重新拉取干净的默认设置（避免恢复后内存态残留 null）
  const merged = await window.api.getSettings()
  store.settings = merged
  loadSettingsIntoUI()
  const dirs = await window.api.getDefaultDirs()
  photoDir.value = merged.photoDir || dirs.photoDir || ''
  scanDir.value = merged.scanDir || dirs.scanDir || ''
  ocrDir.value = merged.ocrDir || dirs.ocrDir || ''
  pdfOutDir.value = merged.pdfOutDir || dirs.pdfOutDir || ''
  ElMessage.success('已恢复默认设置')
}
</script>

<style scoped>
.settings-tabs { height: 100%; display: flex; flex-direction: column; }
.settings-tabs :deep(.el-tabs__content) { flex: 1; overflow-y: auto; padding-right: 6px; }
.log-ta {
  width: 100%; height: 220px; resize: none;
  border: 1px solid var(--border); border-radius: 8px;
  background: rgba(128,134,148,0.08); color: var(--text);
  font-family: Consolas, monospace; font-size: 12px; padding: 8px;
  user-select: text;
}
.settings-drawer :deep(.el-drawer__header) { margin-bottom: 8px; }
.opt-row { display: flex; align-items: center; gap: 10px; padding: 4px 0; }
.opt-label { flex: 1; font-size: 13px; color: var(--text); }
.opt-hint { font-size: 12px; color: var(--muted); }
.svc-card { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; border-radius: 10px; background: rgba(128,134,148,0.06); margin-bottom: 8px; }
.svc-card .name { font-weight: 600; font-size: 13px; }
.svc-card .desc { font-size: 12px; color: var(--muted); margin-top: 2px; }
.settings-footer { display: flex; gap: 8px; }
</style>
