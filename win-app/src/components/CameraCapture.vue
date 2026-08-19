<template>
  <div class="camera">
    <!-- 左侧：实时预览 / 拍照结果 -->
    <div class="card cam-stage">
      <video v-if="streamOn && !shot" ref="videoEl" autoplay playsinline muted class="cam-video"></video>
      <img v-else-if="shot" :src="shot" class="cam-video" />
      <div v-else class="cam-empty">
        <el-icon :size="46" style="color:var(--muted)"><VideoCamera /></el-icon>
        <div>开启摄像头，将文档对准取景框后拍照</div>
        <el-button type="primary" class="scan-btn" size="large" @click="start">
          <el-icon style="margin-right:4px"><VideoCamera /></el-icon>开启摄像头
        </el-button>
      </div>
      <!-- 取景辅助框（仅预览时显示） -->
      <div v-if="streamOn && !shot" class="cam-guide"></div>
      <div class="cam-status" v-if="streamOn">
        <span class="dot" :class="recording ? 'rec' : ''"></span>
        {{ recording ? '已拍照，可继续拍或处理' : '实时预览中' }}
      </div>
      <div v-if="err" class="cam-err">{{ err }}</div>
    </div>

    <!-- 右侧：控制面板 -->
    <div class="card cam-panel">
      <div class="tp-title">摄像头</div>
      <div class="tp-row">
        <span class="tp-label">设备</span>
        <el-select v-model="deviceId" size="small" style="flex:1" placeholder="选择摄像头" @change="switchDevice">
          <el-option v-for="d in devices" :key="d.deviceId" :label="d.label || ('摄像头 ' + (d.index + 1))" :value="d.deviceId" />
        </el-select>
      </div>
      <div class="tp-row">
        <span class="tp-label">画质</span>
        <el-select v-model="res" size="small" style="flex:1" @change="switchDevice">
          <el-option label="最高可用" value="max" />
          <el-option label="1920 × 1080" value="1080" />
          <el-option label="1280 × 720" value="720" />
          <el-option label="640 × 480" value="480" />
        </el-select>
      </div>
      <div class="tp-sep"></div>

      <el-button v-if="streamOn" class="scan-btn cam-off" style="width:100%" @click="turnOff">
        <el-icon style="margin-right:4px"><VideoPause /></el-icon>关闭摄像头
      </el-button>

      <template v-if="!shot">
        <el-button type="primary" class="scan-btn" size="large" style="width:100%" :disabled="!streamOn" @click="capture">
          <el-icon style="margin-right:4px"><Camera /></el-icon>拍照
        </el-button>
        <div class="cam-tip">建议：将文档摆正、光照均匀，尽量铺满画面</div>
      </template>

      <template v-else>
        <div class="tp-title">处理这张照片</div>
        <el-button type="primary" class="scan-btn" style="width:100%" @click="addToTray">
          <el-icon style="margin-right:4px"><FolderAdd /></el-icon>加入页面列表
        </el-button>
        <el-button class="scan-btn" style="width:100%" @click="toOcr">
          <el-icon style="margin-right:4px"><Document /></el-icon>直接文字识别
        </el-button>
        <el-button class="scan-btn" style="width:100%" @click="saveAs">保存到…</el-button>
        <div class="tp-sep"></div>
        <el-button class="scan-btn" style="width:100%" @click="retakeAndRestart">重新拍摄</el-button>
      </template>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onBeforeUnmount, onActivated, onDeactivated } from 'vue'
import { ElMessage } from 'element-plus'
import { VideoCamera, Camera, VideoPause, Document, FolderAdd } from '@element-plus/icons-vue'
import { store, log } from '../store'

const emit = defineEmits(['parse'])

const videoEl = ref(null)
const streamOn = ref(false)
const recording = ref(false)
const stream = ref(null)
const devices = ref([])
const deviceId = ref('')
const res = ref('max')
const shot = ref('')          // 拍照后的 dataURL
const err = ref('')
const userStopped = ref(false)  // 用户是否主动关闭过摄像头（切回页面时不自动重启）
const defaultDirs = reactive({ photoDir: '', scanDir: '', ocrDir: '' })

const RES_MAP = { 1080: { width: { ideal: 1920 }, height: { ideal: 1080 } }, 720: { width: { ideal: 1280 }, height: { ideal: 720 } }, 480: { width: { ideal: 640 }, height: { ideal: 480 } } }

async function listDevices() {
  try {
    const all = await navigator.mediaDevices.enumerateDevices()
    devices.value = all.filter((d) => d.kind === 'videoinput').map((d, i) => ({ ...d, index: i }))
    // 已有设备时默认选中第一个，避免下拉框一直显示「选择摄像头」占位文案
    if (devices.value.length && !devices.value.some((d) => d.deviceId === deviceId.value)) {
      deviceId.value = devices.value[0].deviceId
    }
  } catch (e) { err.value = '无法枚举摄像头：' + e.message }
}

async function start() {
  err.value = ''
  userStopped.value = false
  try {
    await stopStream()
    const constraints = { audio: false, video: { deviceId: deviceId.value ? { exact: deviceId.value } : undefined } }
    if (res.value !== 'max') Object.assign(constraints.video, RES_MAP[res.value])
    stream.value = await navigator.mediaDevices.getUserMedia(constraints)
    streamOn.value = true
    shot.value = ''
    requestAnimationFrame(() => { if (videoEl.value) videoEl.value.srcObject = stream.value })
    // 用当前实际使用的设备 id 同步下拉框显示（权限获取前后 id 可能不一致）
    await listDevices()
    const activeId = stream.value?.getVideoTracks()?.[0]?.getSettings()?.deviceId
    if (activeId) deviceId.value = activeId
    else if (!deviceId.value && devices.value.length) deviceId.value = devices.value[0].deviceId
  } catch (e) {
    err.value = '摄像头启动失败：' + (e.name === 'NotAllowedError' ? '未获得权限，请在系统中允许摄像头访问。' : e.message)
  }
}

async function switchDevice() {
  if (streamOn.value) await start()
}

async function stopStream() {
  if (stream.value) {
    stream.value.getTracks().forEach((t) => t.stop())
    stream.value = null
  }
  streamOn.value = false
}

// 关闭摄像头：停止预览并清空已拍照片
function turnOff() {
  userStopped.value = true
  stopStream()
  retake()
  err.value = ''
}

async function capture() {
  const v = videoEl.value
  if (!v || !v.videoWidth) { err.value = '摄像头尚未就绪，请稍候再拍'; return }
  const canvas = document.createElement('canvas')
  canvas.width = v.videoWidth
  canvas.height = v.videoHeight
  canvas.getContext('2d').drawImage(v, 0, 0)
  shot.value = canvas.toDataURL('image/jpeg', 0.92)
  recording.value = true
  // 拍摄完成后自动关闭摄像头，避免一直占用设备
  await stopStream()
}

function retake() {
  shot.value = ''
  recording.value = false
}

// 拍照文件命名：cam_<本地日期>_<本地时间>_<毫秒>（如 cam_20260819_141500_123）
// 秒级时间可读、毫秒位保证同秒连拍不重名；作为源文件进入识别队列后，
// OCR 结果文件夹也会以此可读名称归档。
function camName() {
  const d = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  return `cam_${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_` +
    `${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}_` +
    `${String(d.getMilliseconds()).padStart(3, '0')}`
}

// 「重新拍摄」：清空已拍照片并重新开启摄像头预览，避免拍完一次后停留在空态需手动重开
async function retakeAndRestart() {
  retake()
  await start()
}

async function saveShot(name, dir) {
  // 默认保存到「本机照片」文件夹：优先跟随设置里的 photoDir（避免 onMounted 缓存旧值），
  // 未设置时回退到系统默认；defaultDirs 尚未就绪时现场获取。
  let target = dir || store.settings?.photoDir || defaultDirs.photoDir
  if (!target) {
    const d = await window.api.getDefaultDirs()
    Object.assign(defaultDirs, d)
    target = store.settings?.photoDir || d.photoDir
  }
  const r = await window.api.saveImageData({
    data: shot.value,
    name,
    dir: target,
  })
  if (!r.ok) throw new Error(r.error)
  return r.file
}

async function addToTray() {
  try {
    const file = await saveShot(camName())
    store.acquireTray.push({ path: file, name: file.split(/[\\/]/).pop(), source: 'camera', uri: shot.value })
    ElMessage.success('已加入页面列表')
    retake()
  } catch (e) { err.value = e.message }
}

async function toOcr() {
  try {
    const file = await saveShot(camName())
    store.ocrInbox.push(file)
    emit('parse', file)
  } catch (e) { err.value = e.message }
}

async function saveAs() {
  try {
    const dir = await window.api.chooseDirectory()
    if (!dir) return
    // 主进程 img:save-data 会自动补扩展名，这里不带扩展名避免 .jpg.jpg
    const r = await window.api.saveImageData({ data: shot.value, name: camName(), dir })
    if (!r.ok) throw new Error(r.error)
    log('✔ 照片已保存：' + r.file)
    ElMessage.success('照片已保存：' + r.file)
    retake()
  } catch (e) { err.value = e.message }
}

onMounted(async () => {
  listDevices()
  try {
    Object.assign(defaultDirs, await window.api.getDefaultDirs())
  } catch (_) { /* 忽略 */ }
})
// KeepAlive 下切走页面不卸载，onBeforeUnmount 不触发：用 onDeactivated 确保
// 离开模块时释放摄像头，避免设备灯常亮、被其他程序占用；切回时若此前正在
// 预览（且用户未主动关闭）则自动恢复，拍照后/主动关闭则不打扰。
onDeactivated(() => stopStream())
onBeforeUnmount(() => stopStream())
onActivated(() => {
  if (!userStopped.value && !streamOn.value && !shot.value && deviceId.value && !err.value) start()
})
</script>

<style scoped>
.camera { display: flex; gap: 10px; height: 100%; min-height: 0; }
.cam-stage {
  flex: 1; display: flex; align-items: center; justify-content: center;
  position: relative; min-width: 0; overflow: hidden; padding: 14px;
}
.cam-video { max-width: 100%; max-height: 100%; border-radius: 8px; object-fit: contain; }
.cam-guide {
  position: absolute; inset: 8% 6%;
  border: 2px dashed rgba(37, 99, 235, 0.55); border-radius: 8px;
  pointer-events: none;
}
.cam-empty { text-align: center; color: var(--muted); font-size: 13px; line-height: 2.4; }
.cam-status {
  position: absolute; top: 10px; left: 12px; display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--muted); background: var(--card); border: 1px solid var(--border);
  backdrop-filter: blur(28px) saturate(2);
  -webkit-backdrop-filter: blur(28px) saturate(2);
  padding: 4px 10px; border-radius: 999px;
}
.cam-status .dot { width: 8px; height: 8px; border-radius: 50%; background: #94a3b8; }
.cam-status .dot.rec { background: #dc2626; animation: blink 1s infinite; }
@keyframes blink { 50% { opacity: .25; } }
.cam-err {
  position: absolute; bottom: 10px; left: 14px; right: 14px;
  color: var(--err); font-size: 12px; background: rgba(220,38,38,.08);
  padding: 6px 10px; border-radius: 6px;
}
.cam-panel {
  flex: 0 0 280px; display: flex; flex-direction: column; gap: 12px;
  padding: 14px; overflow-y: auto;
}
.cam-tip { font-size: 12px; color: var(--muted); line-height: 1.7; }
.tp-title { font-size: 13px; font-weight: 600; }
.tp-row { display: flex; align-items: center; gap: 10px; }
.tp-label { font-size: 12px; color: var(--muted); width: 44px; flex: none; }
.tp-sep { height: 1px; background: var(--border); margin: 2px 0; }
.cam-off { color: var(--err); border-color: rgba(220,38,38,.35); }
.cam-off:hover { color: #fff; border-color: var(--err); background: var(--err); }
</style>
