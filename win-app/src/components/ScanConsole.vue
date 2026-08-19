<template>
  <div class="scan-console">
    <div class="sc-head">
      <span class="sc-title">扫描</span>
      <el-button size="small" text @click="loadDevices">刷新设备</el-button>
    </div>

    <!-- 设备选择 -->
    <div class="sc-row">
      <span class="sc-label">扫描仪</span>
      <el-select v-model="deviceId" size="small" style="flex:1" placeholder="选择扫描仪" :loading="loadingDev" :disabled="scanning">
        <el-option v-for="d in devices" :key="d.id" :label="d.name" :value="d.id" />
      </el-select>
    </div>

    <!-- 源 -->
    <div class="sc-row">
      <span class="sc-label">源</span>
      <el-radio-group v-model="form.source" size="small" :disabled="scanning">
        <el-radio-button value="flatbed">平板</el-radio-button>
        <el-radio-button value="adf">送纸器</el-radio-button>
      </el-radio-group>
    </div>

    <!-- 色彩 -->
    <div class="sc-row">
      <span class="sc-label">色彩</span>
      <el-radio-group v-model="form.mode" size="small" :disabled="scanning">
        <el-radio-button value="color">彩色</el-radio-button>
        <el-radio-button value="gray">灰度</el-radio-button>
        <el-radio-button value="bw">黑白</el-radio-button>
      </el-radio-group>
    </div>

    <!-- DPI -->
    <div class="sc-row">
      <span class="sc-label">分辨率</span>
      <el-radio-group v-model="form.dpi" size="small" :disabled="scanning">
        <el-radio-button :value="150">150</el-radio-button>
        <el-radio-button :value="300">300</el-radio-button>
        <el-radio-button :value="600">600</el-radio-button>
      </el-radio-group>
    </div>

    <!-- 格式 -->
    <div class="sc-row">
      <span class="sc-label">格式</span>
      <el-radio-group v-model="form.format" size="small" :disabled="scanning">
        <el-radio-button value="pdf">PDF</el-radio-button>
        <el-radio-button value="jpg">JPG</el-radio-button>
        <el-radio-button value="png">PNG</el-radio-button>
        <el-radio-button value="tiff">TIFF</el-radio-button>
      </el-radio-group>
    </div>

    <div v-if="form.source === 'adf'" class="sc-row">
      <span class="sc-label">页数</span>
      <el-switch v-model="autoPages" size="small" :disabled="scanning" active-text="自动" />
      <el-input-number
        v-if="!autoPages"
        v-model="form.pages"
        :min="1" :max="200" size="small" style="width:110px" :disabled="scanning"
      />
      <span class="sc-hint">{{ autoPages ? '扫描至纸空' : '页' }}</span>
    </div>

    <div class="sc-row">
      <span class="sc-label">增强</span>
      <el-switch v-model="form.enhance" :disabled="scanning" />
      <span class="sc-hint">去白边/去底色</span>
    </div>

    <div class="sc-row">
      <span class="sc-label">保存到</span>
      <el-input v-model="form.outdir" size="small" placeholder="选择文件夹" :disabled="scanning">
        <template #append>
          <el-button size="small" @click="pickDir" :disabled="scanning">选择</el-button>
        </template>
      </el-input>
    </div>

    <!-- 预览 -->
    <div class="sc-preview" :class="{ 'has-img': !!previewUri }">
      <div v-if="!previewUri" class="sc-empty">点击「预览」查看扫描效果</div>
      <img v-else :src="previewUri" alt="预览" />
    </div>

    <!-- 进度 -->
    <div v-if="scanning" class="sc-progress">
      <el-progress :percentage="Math.round((progress || 0) * 100)" :stroke-width="8" />
      <div class="sc-hint">{{ progressDesc }}</div>
    </div>
    <div v-else-if="err" class="sc-err">{{ err }}</div>

    <!-- 操作 -->
    <div class="sc-actions">
      <el-button size="default" @click="doPreview" :loading="previewing" :disabled="!deviceReady || scanning">预览</el-button>
      <el-button type="primary" size="default" style="flex:1" @click="doScan" :loading="scanning" :disabled="!deviceReady">
        {{ scanning ? '扫描中…' : '开始扫描' }}
      </el-button>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { store, log } from '../store'

const emit = defineEmits(['scan-done'])

const devices = ref([])
const deviceId = ref(null)
const loadingDev = ref(false)
const previewUri = ref('')
const previewing = ref(false)
const scanning = ref(false)
const progress = ref(0)
const progressDesc = ref('')
const err = ref('')

const form = reactive({
  source: 'flatbed',
  mode: 'color',
  dpi: 300,
  format: 'pdf',
  pages: 0,
  enhance: true,
  // 默认取设置「扫描结果保存位置」；手动选择仅本次会话生效（不写 localStorage，避免与设置脱节）
  outdir: '',
})

// ADF 页数模式：0 = 自动（扫描至纸空）。UI 用「自动」开关表达，避免出现裸数字 0；
// 关闭开关时才显示页数输入框（最小 1 页）。
const autoPages = ref(true)
watch(autoPages, (on) => {
  if (on) form.pages = 0
  else if (!form.pages) form.pages = 1
})

// 设置中「扫描结果保存位置」的当前值，用于判断面板目录是否被用户手动改过
const defaultDir = ref('')

const deviceReady = computed(() => devices.value.some((d) => d.id === deviceId.value))

let progressOff = null

onMounted(async () => {
  await loadDevices()
  const d = await window.api.scanDefaultDir()
  defaultDir.value = d || ''
  if (!form.outdir) form.outdir = defaultDir.value
  progressOff = window.api.onScanProgress((ev) => {
    progress.value = ev.frac || 0
    progressDesc.value = ev.desc || ''
  })
})

// 设置里改了「扫描结果保存位置」→ 扫描面板跟随新默认目录；
// 若用户已手动指定过其他目录，则不覆盖（保留其本次会话的选择）。
watch(
  () => store.settings?.scanDir,
  async (v) => {
    const old = defaultDir.value
    const d = (v && String(v).trim()) ? String(v).trim() : ((await window.api.scanDefaultDir()) || '')
    defaultDir.value = d
    if (form.outdir === '' || form.outdir === old) form.outdir = d
  },
)
onBeforeUnmount(() => progressOff && progressOff())

async function loadDevices() {
  loadingDev.value = true
  err.value = ''
  try {
    const r = await window.api.scanDevices()
    if (r?.ok) {
      devices.value = r.list || []
      // 设备被拔/更换后，之前选中的 deviceId 可能已不在列表里：回退到首个设备，
      // 避免 deviceReady 因「列表非空但 id 失配」而误判为可用。
      const stillValid = devices.value.some((d) => d.id === deviceId.value)
      deviceId.value = stillValid ? deviceId.value : (devices.value.length ? devices.value[0].id : null)
      if (!devices.value.length) err.value = '未检测到扫描仪'
    } else {
      err.value = r?.error || '无法枚举扫描仪'
    }
  } catch (e) {
    err.value = e.message || '无法枚举扫描仪'
  }
  loadingDev.value = false
}

async function pickDir() {
  const dir = await window.api.chooseDirectory()
  if (dir) form.outdir = dir
}

async function doPreview() {
  if (!deviceReady.value) return
  previewing.value = true
  err.value = ''
  try {
    const r = await window.api.scanPreview(deviceId.value, form.dpi)
    if (r?.ok) previewUri.value = await window.api.imageDataUri(r.path)
    else err.value = r?.error || '预览失败'
  } catch (e) {
    err.value = e.message || '预览失败'
  }
  previewing.value = false
}

async function doScan() {
  if (!deviceReady.value) return
  if (!form.outdir) { err.value = '请先选择保存位置'; return }
  scanning.value = true
  err.value = ''
  progress.value = 0
  progressDesc.value = '准备扫描…'
  const name = 'scan_' + new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '')
  try {
    const r = await window.api.scanRun({
      deviceId: deviceId.value,
      dpi: form.dpi,
      mode: form.mode,
      format: form.format,
      source: form.source,
      pages: form.source === 'adf' ? form.pages : 0,
      enhance: form.enhance,
      outdir: form.outdir,
      name,
    })
    if (r?.ok) {
      progress.value = 1
      progressDesc.value = `扫描完成（${r.count} 页）`
      log(`✔ 扫描完成：${r.count} 页 → ${r.file}`)
      emit('scan-done', { ok: true, file: r.file, pages: r.pages || [], count: r.count, outdir: form.outdir, name })
    } else {
      err.value = r?.error || '扫描失败'
      log(`✖ 扫描失败：${r?.error || ''}`)
    }
  } catch (e) {
    err.value = e.message || '扫描失败'
  }
  scanning.value = false
}
</script>

<style scoped>
.scan-console { display: flex; flex-direction: column; gap: 10px; height: 100%; }
.sc-head { display: flex; align-items: center; justify-content: space-between; }
.sc-title { font-size: 15px; font-weight: 700; }
.sc-row { display: flex; align-items: center; gap: 8px; }
.sc-label { width: 56px; flex-shrink: 0; font-size: 12px; color: var(--muted); }
.sc-hint { font-size: 11px; color: var(--muted); }
.sc-preview {
  flex: 1; min-height: 120px; border: 1px dashed var(--border); border-radius: 8px;
  display: flex; align-items: center; justify-content: center; overflow: hidden;
  background: rgba(0, 0, 0, .03);
}
.sc-preview.has-img { border-style: solid; }
.sc-preview img { max-width: 100%; max-height: 100%; object-fit: contain; }
.sc-empty { color: var(--muted); font-size: 12px; }
.sc-progress { margin-top: 2px; }
.sc-err { color: var(--err); font-size: 12px; word-break: break-all; line-height: 1.4; }
.sc-actions { display: flex; gap: 8px; }
</style>
