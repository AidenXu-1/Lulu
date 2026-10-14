# Lulu production 授权本机管理工具

## 使用

日常从项目根 `授权管理/Lulu授权码管理.command` 启动。上一级 `打开授权码本机管理工具.command` 仅保留为工程内部兼容入口。

- 工具只监听 `127.0.0.1`。
- 已运行时再次双击只打开现有页面。
- 默认端口被占用时会自动选择下一个安全空闲端口。
- 使用完请点击页面右下角“关闭管理工具”;页面关闭成功后服务、启动器和终端会话会在 5 秒内自然结束,无需手动结束进程。

## Keychain 前置

工具读取现有 production admin endpoint/token 的 Keychain service 候选,不读取聊天或仓库中的明文配置。缺项或值不符合 HTTPS/production 形态时失败关闭。

## 台账

一次性 CSV 默认写入项目根 `授权管理/授权码台账/`。入口与默认路径都相对当前项目定位,项目移动后不会回退 Documents。`授权管理/` 与台账目录权限固定为 `0700`,文件权限固定为 `0600`,使用排他创建防止覆盖旧台账;项目 `.gitignore` 缺少专用规则时失败关闭。

## 永久删除

- 工作台不设归档。不确定是否还会使用时只做“停用”,以后仍可恢复。
- `active` 授权不能永久删除;必须先停用并逐人核对。
- 永久删除只允许单条操作。页面先读取服务器实时清点结果,展示设备、凭证和设备操作数量,再要求输入目标 ID 后缀确认短语。
- 预检凭证 5 分钟有效并绑定目标、状态和数量;期间数据变化时删除失败,必须重新预检。
- 第一版 production D1 没有设备操作表时按旧结构兼容;只有明确的缺表错误会按 0 处理,其他数据库异常继续失败关闭。
- 成功后目标的 license/device/lease/device-deactivation 服务器记录被清除,去敏审计与本机 CSV 保留。
- 删除某一授权不会影响其他授权。目标离线设备仍可能在既有 7 天宽限期内继续使用。

## 安全边界

- production endpoint/token 只存在于本机 Node 后端内存,不进入浏览器、URL、CSV、日志或错误文案。
- 启动器把高熵、单次使用的 bootstrap capability 放在 URL fragment 中；未认证的 `GET /admin` 只返回引导壳，不发放 session 或 CSRF。
- 引导壳经同源 POST 一次性换取随机 HttpOnly session；重放、错误 Host/Origin 和缺少 CSRF 均失败关闭。
- 页面及 API 使用 no-store 和 CSP，bootstrap fragment 不随 HTTP 请求发送，成功换取前会从地址栏清除。
- 浏览器页面只返回授权安全字段,不返回 code hash、device hash 或 lease token。
- 工具不进入 Lulu 用户 DMG,不启用 Cloudflare Access,不修改 Worker/D1/Secrets。
- 永久删除不会自动扫描或删除本机 CSV,避免扩大明文授权码的读取和误删范围。
- 本机账号或 Keychain 已被攻破时,该工具不能提供额外隔离。
