#!/usr/bin/env bash
# Run inside the pinned CUDA base image with the deployment root mounted at /models.
set -euo pipefail
backend="${1:?backend required}"
export UV_CACHE_DIR=/models/cache/uv
export TMPDIR=/models/tmp
mkdir -p "$UV_CACHE_DIR" "$TMPDIR" /models/envs /models/locks
case "$backend" in
  teleocr)
    uv venv --python /usr/bin/python3 /models/envs/teleocr
    uv pip install --python /models/envs/teleocr/bin/python -e /models/source/teleocr
    ;;
  logics)
    uv venv --python /usr/bin/python3 /models/envs/logics
    uv pip install --python /models/envs/logics/bin/python 'vllm==0.22.1' 'transformers==5.6.0' Pillow PyMuPDF modelscope
    ;;
  mineru)
    # MinerU 4 supports this installed vLLM; reuse large CUDA packages only here.
    uv venv --system-site-packages --python /usr/bin/python3 /models/envs/mineru
    uv pip install --python /models/envs/mineru/bin/python -e '/models/source/mineru[full]'
    ;;
  *) echo 'unsupported backend' >&2; exit 2 ;;
esac
uv pip freeze --python "/models/envs/$backend/bin/python" > "/models/locks/$backend.requirements.txt"
git -C "/models/source/$backend" rev-parse HEAD > "/models/locks/$backend.commit.txt"
"/models/envs/$backend/bin/python" -c 'import torch; print("torch",torch.__version__)'
