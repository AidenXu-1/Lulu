# Lulu VoxCPM2 Phase 1 Third-Party Notices

Scope: VoxCPM2 first-model slice added on 2026-06-24. This file is packaged with Lulu so external test builds have a human-readable attribution entrypoint. Full license texts from installed Python distributions are also collected by PyInstaller where available via each package's `dist-info/licenses` files.

## Primary Components

| Component | Version / Source | License | Evidence |
|---|---:|---|---|
| VoxCPM Python package | `voxcpm==2.0.3` | Apache-2.0 | Local wheel includes `voxcpm-2.0.3.dist-info/licenses/LICENSE`; upstream repository: `https://github.com/OpenBMB/VoxCPM` |
| VoxCPM2 model weights | `openbmb/VoxCPM2` | Apache-2.0 | Hugging Face model card states Apache-2.0 and free commercial use: `https://huggingface.co/openbmb/VoxCPM2` |
| fsspec | `fsspec==2025.3.0` | BSD-3-Clause | Package metadata and license text |

## New Runtime Dependency Notice Checklist

| Package | Version observed | License source used for packaging review |
|---|---:|---|
| `datasets` | 3.6.0 | Package metadata: Apache-2.0 |
| `pyarrow` | 24.0.0 | Local `pyarrow-24.0.0.dist-info/licenses/LICENSE.txt` and `NOTICE.txt` |
| `matplotlib` | 3.11.0 | Package metadata/license text: Matplotlib license, plus bundled font notices |
| `spaces` | 0.50.4 | Local vendored license files under `spaces/_vendor/*/LICENSE`; package homepage `https://huggingface.co` |
| `torchcodec` | 0.14.0 | Local `torchcodec-0.14.0.dist-info/licenses/LICENSE` |
| `wetext` | 0.1.4 | Local `wetext-0.1.4.dist-info/licenses/LICENSE` |
| `librosa` | 0.11.0 | Package metadata: ISC |
| `soundfile` | 0.14.0 | Package metadata: BSD-3-Clause |
| `safetensors` | 0.8.0 | Package classifier: Apache Software License |
| `huggingface_hub` | 0.36.2 | Package metadata: Apache |
| `transformers` | 4.57.6 | Package metadata: Apache-2.0 |
| `tokenizers` | 0.22.2 | Package classifier: Apache Software License |
| `accelerate` | 1.12.0 | Package metadata: Apache |
| `inflect` | 7.5.0 | Package classifier: MIT |
| `modelscope` | 1.37.1 | Local `modelscope-1.37.1.dist-info/licenses/LICENSE` |

## Release Obligations

- Keep this notice file in the packaged app.
- Preserve package `dist-info/licenses` and `NOTICE` files collected into the frozen app.
- Do not bundle VoxCPM2 model weights in the DMG; model weights remain downloaded into the user-selected local model directory.
- Keep the in-app authorization reminder text before saving a voice or generating cloned speech.
- Run the safety gate again before any public release if dependency versions change.
