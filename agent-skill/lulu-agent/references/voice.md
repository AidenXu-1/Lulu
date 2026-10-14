# Four local voice models

Read current capabilities and `tts status --output json` before generation. These commands use the
verified installed `/Applications/Lulu.app/Contents/MacOS/lulu-agent-cli` launcher. An older installation may expose only VoxCPM2.
Do not use source scripts or model workers to bypass unavailable installed capabilities.

The text file and JSON settings file must be absolute, owned regular files (not symlinks), readable
and not writable by other users. WAV destination must be new, inside an owned existing directory.
All models use `jobs show`, `jobs wait`, `jobs cancel` and `jobs receipt` after returning job_id.

## IndexTTS 2.5

Engine: `local_indextts2`. Saved voice required. Example options:

```json
{"emotion_mode":"template","emotion_preset":"gentle","emotion_intensity":"standard","language":"ZH","speed":1.0,"generation_options":{"top_k":30}}
```

Modes: follow, template, vector, emotext, emoaudio. Text description uses emotion_text;
audio reference uses emotion_audio_path (absolute path, copied into this task before inference).
Vector uses emotion_vector (8 finite numbers, total no more than 0.8). Advanced generation fields
and bounds are reported by status. Languages: ZH, EN, JA, ES, AR; speed: 0.5–2.0.

## IndexTTS 2

Engine: `local_indextts2_0`. Saved voice required. Same emotion modes and independently validated
generation settings. No speed, language or do_sample override; these are not used by this version.
The Core fixes model_version to 2.0 and never silently substitutes 2.5.

## VoxCPM2

Engine: `local_voxcpm2`. Modes: hifi (default, audio and reference text), directed (reference voice,
optional instruction), design (instruction required, no saved voice needed).

```json
{"mode":"directed","instruction":"自然、温暖地讲述","seed":1234,"inference_timesteps":10,"cfg_value":2.0}
```

Other existing GUI settings: normalize, pronunciations. Use status option_fields and the model's
validated settings; do not guess unsupported parameters.

## Qwen3-TTS

Engine: `local_qwen3_tts`. Each mode uses a separate model and readiness:
clone (Base), official (CustomVoice), design (VoiceDesign). Official and design need no voice-id.

```json
{"mode":"official","language":"Chinese","speaker":"Serena","instruction":"轻松自然地讲述","seed":1234}
```

For design, set mode to design and describe the voice in instruction. Clone requires a saved voice;
reference text is required unless x_vector_only_mode is true. Available languages and speakers are
returned by status. Also supports temperature and repetition_penalty.

## Command example

```sh
/Applications/Lulu.app/Contents/MacOS/lulu-agent-cli tts synthesize --engine local_indextts2 --text-file /absolute/script.txt --voice-id VOICE_ID --options-file /absolute/settings.json --destination /absolute/new-voice.wav --output json
```

Do not report audio generated until the task succeeded and the receipt's WAV exists, is readable,
and matches its hash. Cancellation or failed/recovery_required states do not authorize retry.
