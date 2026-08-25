# 更新日志

本项目的所有重要变更均记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增（win-app 桌面应用）

- 集成 FairScan 桌面版独有能力（Python 移植，非 JVM 集成）：图片文档识别前可先本地预处理——
  - **边缘检测 + 透视裁剪**（TFLite 前景分割 + 四边形检测 + 透视校正，模型 `offline/models/fairscan-segmentation-model.tflite`，与 Android 端一致的官方文档分割模型）；
  - **去阴影**（形态学背景估计 + 除法归一化）。
  - **GPU 加速**：ai-edge-litert 随离线包分发，TFLite 推理自动优先 **WebGPU（Dawn/D3D12）**（任意 DirectX12 显卡，无需 CUDA 工具链），不可用时回退 CPU/XNNPACK——RTX 5060 实测 GPU 0.022s/图 vs CPU 0.051s。
  - 设置抽屉「识别参数 → 图片文档预处理」两个开关（默认关闭）；仅对图片生效，PDF 自动跳过。
  - 新增「智能文档」前端工作台（独立侧栏模块）：参考 FairScan Desktop UI 三栏布局，支持添加/拖拽图片、原图/结果预览、检测四角可视化拖拽编辑、`--quad` 重处理、批量保存结果、一键送入 OCR 识别。
  - 识别、导出、图表裁剪均基于预处理结果，但输出文件命名仍以原文件为准；预处理失败自动回退原图，不中断批量。
  - 预处理临时输出写入 `%TEMP%\paddleocr_preprocess_*`，纳入启动/退出统一清理。
- **「智能文档」1:1 复刻 FairScan Desktop（识别参数/功能/布局）**：`scan_preprocess.py` 重写为四子命令流水线（scan/detect/pdf/旧式兼容）——
  - **识别参数全对齐**：检测模式 CAPTURE（多阈值 `[0.5,0.7,0.75,0.8,0.85,0.9,0.95]`）/IMPORT（仅 0.9）、旋转 auto/0/90/180/270（PP-LCNet_x1_0_doc_ori 方向分类）、色彩 AUTO（Retinex+灰度世界+Lab 色度阈值）/COLOR/GRAYSCALE、输出质量 LOW/BALANCED/HIGH（60/75/80 + 1M/2M/4M maxPixels）、推理线程 1–8、曲面矫正 UVDoc、去阴影、输出目录浏览。
  - **功能全对齐**：摄像头拍照对话框（实时四边形定位 + 拍照入队 CAPTURE 模式）、OCR 词条编辑对话框（可编辑文本 + 保存词条 JSON + PDF 隐形文本层回写）、设置对话框、底部日志折叠面板、命令栏 + CLI 状态。
  - **全链路 GPU**：方向分类 / UVDoc / OCR 统一走 paddlex（Paddle 3.x PIR 新格式 + `create_predictor`，`_infer_device()` 自动 CUDA 优先、无 GPU 回退 CPU），TFLite 分割走 litert WebGPU——实测 GPU 单页 OCR det 0.24s / rec 0.13s / UVDoc 0.21s、分割 0.022s/图。
  - **分割模型**：固定使用原版 `fairscan-segmentation-model.tflite`（ai-edge-litert **CompiledModel WebGPU/D3D12 GPU 优先**、失败回退 CPU/XNNPACK，随包分发 DirectX Shader Compiler dxcompiler/dxil v1.9.2607 支持 Blackwell SM6.8）。按需求删除 `u2netp.onnx` 及 onnxruntime 分割路径，「分割模型」下拉仅保留「原版 TFLite 模型」一项；输出与 Android 端一致 clamp 到 [0,1]。
  - **修复文档方向误判**（竖拍被转横）：`OrientationService.detect_rotation` 先前自行做等比缩放+中心裁剪+ImageNet 归一化，而 paddlex `create_predictor` 会自动应用模型 transform，导致**双重归一化**破坏输入分布。改为直接传 BGR 原图由 paddlex 处理，四种朝向验证全对（0°→0 / 顺90°→270 / 180°→180 / 顺270°→90，单张 0.2s）。
  - **输出质量默认改为「高」（HIGH）**。
  - **移除「OCR 语言」设置**：`--lang` 参数后端接收但从未使用（识别固定 PP-OCRv5_server_rec 中英文模型），前端语言输入框、`params.lang`、main.js 传参一并删除。
  - **核实 OCR 词条注入 PDF 功能实现正常**：对照 FairScan-main 原版 `OcrPdfTextLayer.kt`（GlyphLessFont + Identity-H + ToUnicode CMap + `3 Tr` 不可见渲染），Python 移植版（PyMuPDF `insert_text(render_mode=3)`）端到端实测通过——生成 PDF 内容流含 `3 Tr`、中文（china-s）/英文（helv）词条均可提取；`t:ocr` 事件流、词条坐标（OCR 图 = 页面图，尺寸一致）、`fairscan:pdf` IPC 链路完整。
  - **修复对话框半透明**：设置/词条/摄像头对话框背景原为 `var(--card)`（rgba 白 58% / 深色 55%），透过背景可见页面内容；改为不透明 `var(--card-solid)`。
  - **推理线程设置移入智能文档设置对话框**：从识别参数面板移至「设置」对话框（导出选项之后），上限动态取本机逻辑核心数（`navigator.hardwareConcurrency` / `os.cpus().length`），备注「仅 CPU 回退模式下生效（GPU 加速时自动忽略）」。
  - **检测模式显示名**：「宽容检测」（原 CAPTURE）/「严格检测」（原 IMPORT），下拉不再显示括号说明；检测界面底部日志面板默认收起。
  - **修复 OCR 词条永远只有 1 个**：paddlex rec predictor 不支持批量输入，`next(self.rec(crops))` 只识别第一张裁剪图；改为逐 crop 调用（实测 17 检测框 → 17 词条）。
  - **修复 PDF 词条垂直镜像错位**：baseline 照抄 Kotlin `pageHeight - lineBottom*scaleY`，但 PyMuPDF `insert_text` 为左上原点（y 向下）坐标，二次镜像导致词条上下颠倒；改为 `lineBottom*sy + fontSize*0.2`（实测 17 词条 x 全对齐、行序正确）。
  - **修复文件列表未左对齐**：`.fs-list` 未清除 ul 默认 `padding-left:40px`，文件条整体右移；补 `margin:0;padding:0`。
  - **修复 PDF 词条行间错位**（两处根因）：
    1. `_group_lines` 把竖排大字（高 > 2×宽，如表格竖排「雨心/精」20×100）与横排小字按垂直重叠合并成一行，`lineBottom` 被拉到竖排框底部，导致中间横排行基线全部错位；改为竖排框单独成行、仅横排框间合并。
    2. `write_pdf` 用 `fitz.open(jpeg).rect` 取像素尺寸，但该值返回物理尺寸（JPEG 带 96 DPI 时 688px→516pt），导致 `sx/sy` 缩放错误、文本层 x 方向整体错位；改用 `fitz.Pixmap` 取真实像素。实测 17 词条左边界全部精确对齐、各行正确分离。
  - **修复 PDF 词条行间错位（第二处根因，表格相邻行粘行）**：PP-OCR 词条框贴合字形，表格相邻行的框在 y 向真实重叠（如高的表头框压住下一行顶部），任何基于 y 范围的行分组都会把两行粘成一行，使上一行词条被拉到下一行底部（差 19~22px）——
    1. `_group_lines` 合并判定从「y 范围重叠」改为「中心 y 聚类」（`|中心差| ≤ 0.35 × 较小框高`），表格行正确分离（UI 行分组展示随之修复）；
    2. `_add_ocr_text_layer` 改为**逐词放置**：字号、基线取词条自身框（`font = bh×sy×0.8`、`baseline = bottom×sy + font×0.2`），PDF 对齐不再依赖分组质量。实测扁平发票两行表格词条 x 完全对齐、y 向仅剩原版公式固有的均匀下移。
  - **修复分割 `to_mask` 与原版不一致**：原版 `SegmentationMask.toMat()` 直接在 256×256 概率图上阈值 0.5 二值化；移植版此前把概率图上采样回原图尺寸→阈值→再 INTER_AREA 降回 256×256，往返引入边缘抗锯齿使 mask 边界与原版有细微偏差。改为直接在 256×256 概率图上二值化，与 `SegmentationMask.kt` 完全一致。
  - **透视校正边界模式对齐原版**：`warpPerspective` 由 `BORDER_REPLICATE` 改为原版默认 `BORDER_CONSTANT`（黑边填充，`DocumentDetection.kt` 仅传 4 参数走 OpenCV 默认）。
  - **修复智能文档临时文件泄漏**：摄像头拍照帧写入 `%TEMP%\fairscan-camera\`（每次拍照一张 JPG），但退出清扫 `cleanupTempScans()` 白名单只有 `paddleocr_*` 前缀，导致该目录**永不清理**（实测已累积 62 个残留文件）；`cleanupTempScans()` 增加 `fairscan-` 前缀（覆盖 `fairscan-camera` 与崩溃残留的 `fairscan-words-*.json`），同时将 `paddleocr_preview_` 放宽为 `paddleocr_preview`（覆盖 pdf_preview.py 无参默认目录）。已清理既有残留，退出清扫顺序（先杀 sidecar 进程再删目录）不变。
- 自动扫描工具条新增「结果文件夹」按钮，一键打开 OCR 输出目录（`shell.openPath`，跨平台，目录不存在自动创建）。
- 工具箱「图片转 PDF」支持输出目录设置（设置抽屉持久化，默认 `文档/PDF输出`；设置后直接落盘、重名自动加序号）。
- 获取页新增右侧高清大图查看区：点击已获取页面即在右侧预览原图（替代原弹窗），支持「识别此页 / 从列表移除」。
- 大图预览兼容扫描仪全部导出格式：TIFF 自动转 PNG（PIL）、PDF 渲染首页（PyMuPDF）。

### 优化（win-app 桌面应用）

- **智能文档「设置」对话框永久保存 + OCR 词条模型可选 Server/Mobile**：设置（OCR 模型 / 推理线程 / 导出图片 / 导出 PDF）此前仅存组件内存，重启即还原；现写入 `settings.json`（`fairscan` 键）跨会话保留。新增「OCR 词条模型」下拉：**Server（默认，识别质量高）/ Mobile（质量略低，识别速度快）**，词级坐标与 PDF 文字层回写一致；切换后主进程经 `restartFairScanDaemon` 重启常驻引擎（递增代际隔离旧实例事件，防竞态），下次扫描按新模型预热生效。模型缓存目录固定为 `offline\paddlex-cache`（随包分发），不再暴露为设置项。
- **智能文档扫描提速（常驻服务进程）**：`fairscan:scan` 不再每次新建 Python 进程（冷启动需加载分割/方向/OCR 三套引擎约 9~13s），改为常驻 `serve` 子命令（stdin NDJSON 协议，`electron/main.js` 维护 daemon 生命周期），应用启动时后台预热引擎，首扫延迟降至约 1~2s/批；扫描中拒绝并发，停止扫描即释放常驻进程，下次自动重建并重新预热。
- **OCR 词条模型恢复 server 版**：曾尝试换用 PP-OCRv5_mobile_det/rec 瘦身避免与主流程模型冗余，实测 mobile 版识别质量不满足要求（复杂版面/小字号/手写场景明显下降），已恢复为 PP-OCRv5_server_det/rec；词级坐标、行分组与 PDF 隐形文字层回写能力不变。
- 「智能文档」工作台从「扫描和拍照」内部标签页提升为左侧侧栏独立模块，并在首页快捷入口与工作流中同步更新，入口更直观、与扫描/识别流程解耦。
- 扫描仪预览固定低分辨率快速出图，与正式扫描 DPI 解耦（此前预览等同一次 300dpi 全幅扫描）。**多数 WIA 驱动钳制最小 200dpi**，传 100/150 会被忽略并按 200 扫描，故预览与扫描面板最小分辨率均取 **200**（面板选项 150 移除，仅保留 200/300/600；`cmd_scan` 对传入 DPI 做 `max(200, dpi)` 钳制，保证选项与驱动实际输出一致）。
- 缓存清理补漏：TIFF 转换临时文件 `paddleocr_tif2png_*` 纳入退出清理前缀；启动时 `cleanupTempScans()` 兜底清理异常退出残留。

### 修复

- **缓存清理补漏 `paddleocr_wr_*`**：DOCX 渲染（wordrender）临时目录 `paddleocr_wr_*`（`scan_worker.py` mkdtemp）此前未纳入 `cleanupTempScans()` 清理前缀，正常路径 finally 会删除，但进程被强制结束时会残留；现已加入启动/退出统一清理前缀（连同 `paddleocr_preprocess_*`、`fairscan-*` 一并补齐 README 清单）。
- **修复「智能文档」重复扫描已完成文件**：`startScan` 此前会把列表内全部文件（含已扫描完成项）作为输入重新提交，不清空列表再次扫描即整体重跑一遍；现改为只提交 `status !== 'done'` 的待处理文件，全部完成时给出提示。同时 `result/ocr/error` 事件补充 `path` 字段，前端事件匹配改为**按 path 精确匹配**（兜底文件名），避免文件列表变动时状态写到错误条目。
- **修复批次异常时扫描被永久拒绝**：常驻 `serve` 处理批次若整体抛异常，此前只发 `result ok:false` 而不发 `summary`，而 `main.js` 仅凭 summary 重置 `fairScanBusy` 并发标志，导致后续所有扫描被误拒（「已有扫描任务在运行」）且需重启应用；现异常分支强制补发 summary（`failed=N`）。
- **修复停止扫描与新批次之间的竞态**：`fairscan:stop` 杀掉旧常驻进程后，其残留 stdout 事件 / close 可能晚于新批次到达，误清新批次的前端运行状态并破坏 busy 保护；`main.js` 为 daemon 引入代际计数（`fairScanSeq`），旧实例的全部事件（含 close/exit）一律丢弃，仅做句柄清理。
- **前后端选项接入审计修复（8 项）**：
  - **PDF 输出保存位置重启失效**：`settings.json` 中 `pdfOutDir` 此前只写不读（`readSettings` 漏回读），重启后恒回落「文档/PDF输出」；已补齐读回。
  - **智能文档「导出 PDF」关闭后仍跑 OCR**：常驻 `serve` 无条件预热 OCR 引擎，`ocr_enabled=false` 时词条检测照跑、前端仍出词条（仅不写 PDF）；现 `run_batch` 按 `no_ocr` 跳过 OCR 执行与事件发送。
  - **「PDF 每页输出单独文件」「导出图表区域为图片」从不生效**：手动/自动导出完整解析路径硬编码 `per_page=false, export_chart=false`；现改传设置值；且 `export_chart` 仅在导出模式计算（此前纯识别模式白耗 CPU 裁剪图表）。
  - **「保持来源子目录结构」「跳过已存在结果」缓存导出不生效**：有缓存走 `exportRender` 时不携带这两个设置；现前端补传、`main_render` 实现与完整解析一致的目录规划与跳过逻辑。
  - **智能文档未检出文档整图缩放分支写死 COLOR 且跳过去阴影/曲面矫正**：现 fallback 分支应用 `qs_color` 与 `remove_shadow` 增强，与检出路径行为一致。
  - **死字段清理**：移除 `SmartDocPanel` 的 `params.threads`（无控件、从不读取）、`DEFAULT_SETTINGS.defaults.cache_keep_days`（无 UI、无消费）、`scan_device.py` 的 `--preview` 死参数。

- **修复含公式的 DOCX 在 Microsoft Word 中打不开的问题**
  - 将 `<m:oMath>` / `<m:oMathPara>` 包裹到 `<w:r>` 中，符合 Word 段落内容模型
    （之前直接挂在 `<w:p>` 下，Word 严格校验报错）。
  - 清理 docx-equation 生成的 `<m:rPr>/<m:sty>` 与 `<m:ctrlPr>` 元素，
    这些元素在 Word 严格模式下会导致文档损坏提示；清理后 LibreOffice 与
    Microsoft Word 均可正常渲染。
  - 给 `<m:t>` 首尾含空格的文本节点添加 `xml:space="preserve"`。

- **wordrender 公式支持增强（\genfrac）**
  - 新增 `\genfrac` 预处理，将 latex2mathml 不支持的 `\genfrac{ld}{rd}{th}{style}{num}{den}`
    重写为等价写法：有分数横线时按样式映射为 `\frac` / `\dfrac` / `\tfrac`，
    无横线时映射为 `\atop` 堆叠，定界符用 `\left...\right` 包裹
    （`<` / `>` 自动映射为 `\langle` / `\rangle`）。
  - 支持嵌套 `\genfrac`（分子/分母内再嵌套 genfrac 会被递归改写）以及
    圆括号、方括号、花括号、尖括号等多种定界符。
  - 此前 `\genfrac` 会导致公式转换抛异常并回退为纯文本；修复后
    嵌套 genfrac 与 cases 组合的 8 类复杂用例全部转换为原生 Word 公式，零回退。

## [2026-08-11]

### 修复

- DOCX 导出不再包含 `<div>` 标签，表格以 Word 原生表格导出。
- 新增「导出结构化 HTML」选项，HTML 文件随下载压缩包一并提供。
