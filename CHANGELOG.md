# 更新日志

本项目的所有重要变更均记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增（win-app 桌面应用）

- 自动扫描工具条新增「结果文件夹」按钮，一键打开 OCR 输出目录（`shell.openPath`，跨平台，目录不存在自动创建）。
- 工具箱「图片转 PDF」支持输出目录设置（设置抽屉持久化，默认 `文档/PDF输出`；设置后直接落盘、重名自动加序号）。
- 获取页新增右侧高清大图查看区：点击已获取页面即在右侧预览原图（替代原弹窗），支持「识别此页 / 从列表移除」。
- 大图预览兼容扫描仪全部导出格式：TIFF 自动转 PNG（PIL）、PDF 渲染首页（PyMuPDF）。

### 优化（win-app 桌面应用）

- 扫描仪预览固定 100dpi 低分辨率快速出图，与正式扫描 DPI 解耦（此前预览等同一次 300dpi 全幅扫描）。
- 缓存清理补漏：TIFF 转换临时文件 `paddleocr_tif2png_*` 纳入退出清理前缀；启动时 `cleanupTempScans()` 兜底清理异常退出残留。

### 修复

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
