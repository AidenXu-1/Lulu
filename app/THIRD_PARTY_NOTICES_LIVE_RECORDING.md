# 直播录制第三方来源

StreamGet v4.0.10，https://github.com/ihmily/streamget/tree/v4.0.10 ，作者 Hmily，MIT，Copyright (c) 2025 Hmily。

`src/core/live_recording/ab_sign.py` 原样采用纯 Python 签名实现，SHA256 `ab5ccbe8b95a29fc80ffbdf098395703ce4a83141b729a68ac1b01ed0b07a655`，许可原文在同目录 `STREAMGET_LICENSE.txt`。抖音 web enter 请求参数和 stream_data / FLV / HLS 字段规则参考该版本 `streamget/platforms/douyin/live_stream.py`，重写为 requests 同步适配器；未复制完整包、固定 Cookie、sessionid、verifyFp、msToken、示例账号、execjs、httpx 或日志输出。

Lulu 修改：仅接收抖音公开身份链接，校验 TLS、重定向和流域名；匿名开始，显式凭据由安全存储提供；分别报告离线、受限、登录、网络和解析错误。签名流地址仅在内存中，不写索引或用户可见错误。FFmpeg 沿用 Lulu 随包 imageio-ffmpeg 和原有许可。
