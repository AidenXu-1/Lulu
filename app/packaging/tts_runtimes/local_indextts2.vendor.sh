#!/usr/bin/env bash
# IndexTTS2 运行时包的「vendor 源码 + 额外离线资产」钩子。
# 被 build_tts_runtime.sh 调用,环境变量:STAGE(打包暂存根)、PY(运行时解释器)。
# 产出:$STAGE/vendor/index-tts(纯 Python 包,去 .git/示例媒体)+ $STAGE/python/nltk_data(g2p-en 需要)。
# 不含模型权重 —— 权重由 App 安装器从官方 HF 另下(IndexTeam/IndexTTS-2,见 indextts2_weights.py)。
set -euo pipefail

IT2_REPO="https://github.com/index-tts/index-tts.git"
IT2_COMMIT="ee40fa7d6c6b8a2c7f06105f9f1e65775b74868c"   # 官方 2.5，固定完整版本。
VENDOR="$STAGE/vendor"

echo "  [vendor] clone index-tts @ ${IT2_COMMIT}"
mkdir -p "$VENDOR"
git clone --quiet --no-checkout "$IT2_REPO" "$VENDOR/index-tts"
git -C "$VENDOR/index-tts" checkout --quiet "$IT2_COMMIT"
# 瘦身:去 .git、示例音频/视频、文档大图(权重本就不在源码里)
rm -rf "$VENDOR/index-tts/.git"
find "$VENDOR/index-tts" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$VENDOR/index-tts" \( -iname "*.wav" -o -iname "*.mp3" -o -iname "*.flac" \
  -o -iname "*.mp4" -o -iname "*.gif" -o -iname "*.png" -o -iname "*.jpg" \) \
  -size +200k -delete 2>/dev/null || true

echo "  [vendor] 预置 nltk 数据(g2p-en 英文音素需要,随包搬迁)"
NLTK_DIR="$STAGE/python/nltk_data"
mkdir -p "$NLTK_DIR"
"$PY" -I -s - "$NLTK_DIR" <<'PYNLTK'
import sys, nltk
d = sys.argv[1]
for r in ("averaged_perceptron_tagger_eng", "averaged_perceptron_tagger", "cmudict"):
    if not nltk.download(r, download_dir=d, quiet=True):
        sys.exit(f"✗ nltk 资源下载失败: {r}")
print("    nltk_data ok")
PYNLTK

echo "  [vendor] 完成:$(du -sh "$VENDOR/index-tts" | awk '{print $1}') 源码 + nltk_data"
