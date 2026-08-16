#!/usr/bin/env bash
# ============================================================
# 多架构编译 llama.cpp（NVIDIA CC 7.5 ~ 12.0 全覆盖）
#
# 用法：
#   bash build_llama_cuda.sh [llama.cpp 目录]
#
# 环境变量：
#   LLAMA_CPP_REPO     llama.cpp 仓库地址（默认官方 github）
#   LLAMA_CPP_COMMIT   锁定 commit（默认与项目本地一致，保证 PaddleOCR-VL 转换兼容）
#   CUDA_ARCH          覆盖默认架构列表（分号分隔）
#   JOBS               编译并行度（默认 $(nproc)）
#
# 说明：
#   - 首次运行会 clone llama.cpp 并 checkout 指定 commit
#   - 目标架构同时编译 PTX（-virtual，向前 JIT 兼容）与本机 SASS（-real）
#   - sm_100（B200）由 90-virtual 的 Hopper PTX 向前 JIT 覆盖
#   - sm_120（RTX 50）需要 CUDA 12.8+，这里编译为 120a-real
# ============================================================
set -euo pipefail

LLAMA_CPP_DIR="${1:-tools/llama.cpp}"
LLAMA_CPP_REPO="${LLAMA_CPP_REPO:-https://github.com/ggml-org/llama.cpp.git}"
LLAMA_CPP_COMMIT="${LLAMA_CPP_COMMIT:-ba360efe1f574ebae727aad64112d18ecedca85a}"

# CC 7.5~12.0 架构覆盖：
#   75-virtual  Turing (RTX 20 / T4) PTX，向前 JIT 到更高架构
#   80-virtual  Ampere (A100) PTX
#   86-real     RTX 30
#   89-real     RTX 40 / L4 / L40
#   90-virtual  Hopper PTX，向前 JIT 覆盖 sm_100 / sm_120
#   120a-real   RTX 50 (Blackwell，需 CUDA 12.8+)
CUDA_ARCH="${CUDA_ARCH:-75-virtual;80-virtual;86-real;89-real;90-virtual;120a-real}"
JOBS="${JOBS:-$(nproc)}"

BUILD_DIR="$LLAMA_CPP_DIR/build-cuda"

# 1. 准备源码（已有源码则跳过；否则 clone + checkout）
if [ -f "$LLAMA_CPP_DIR/CMakeLists.txt" ]; then
    echo "==> llama.cpp 源码已就绪：$LLAMA_CPP_DIR（跳过 clone）"
else
    echo "==> clone llama.cpp -> $LLAMA_CPP_DIR"
    git clone "$LLAMA_CPP_REPO" "$LLAMA_CPP_DIR"
    git -C "$LLAMA_CPP_DIR" checkout "$LLAMA_CPP_COMMIT"
fi

# 2. CMake 配置（GGML_NATIVE=OFF 避免只编构建机架构）
echo "==> CMake 配置（架构：$CUDA_ARCH）"
cmake -S "$LLAMA_CPP_DIR" -B "$BUILD_DIR" \
    -DGGML_CUDA=ON \
    -DGGML_NATIVE=OFF \
    -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCH" \
    -DCMAKE_BUILD_TYPE=Release

# 3. 编译（仅需 llama-server 与 llama-quantize 两个可执行）
echo "==> 编译 llama-server + llama-quantize（并行 $JOBS）"
cmake --build "$BUILD_DIR" --config Release -j "$JOBS" \
    --target llama-server llama-quantize

echo "==> 完成"
echo "    llama-server:  $BUILD_DIR/bin/llama-server"
echo "    llama-quantize: $BUILD_DIR/bin/llama-quantize"
