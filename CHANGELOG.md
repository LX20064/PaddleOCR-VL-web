# 更新日志

本项目的所有重要变更均记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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
