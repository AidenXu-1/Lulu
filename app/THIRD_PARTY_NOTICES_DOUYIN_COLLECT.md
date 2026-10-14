# 第三方声明 —— 抖音作者主页采集

## F2（a_bogus 签名算法）

`app/src/core/collect/_abogus.py` 改编自 **F2** 项目的 `f2/utils/abogus.py`。

- 项目：F2 — https://github.com/Johnserf-Seed/f2
- 作者：JohnserfSeed
- 许可：**Apache License 2.0**

### 本地改动

- 用自写纯 Python SM3（`app/src/core/collect/_sm3.py`）替换原实现对 `gmssl` 的依赖；
  自写 SM3 已用国标测试向量校验，且与 gmssl 输出逐字节一致 → 本模块**零 pip 依赖**。
- 保留原文件的 Apache-2.0 署名头。

### Apache-2.0 要求

保留版权与许可声明（见 `_abogus.py` 文件头与本文件）。Apache-2.0 全文见
https://www.apache.org/licenses/LICENSE-2.0 。

> 说明：a_bogus 为逆向抖音 web 接口的产物。其引入与维护风险见 ADR-0013，
> **须经安全/许可关确认后方可随产品正式发布**。

## DrissionPage（路 B 登录态全量采集所用浏览器驱动）

`app/src/core/collect/douyin_browser.py` 在 **路 B（用户显式 opt-in 的登录态全量采集）**
惰性导入 **DrissionPage** 驱动本机浏览器。

- 项目：DrissionPage — https://DrissionPage.cn
- 作者：g1879
- 许可：**自定义许可（非 OSI 标准开源协议）**，Copyright (c) 2020, g1879

### ⚠️ 许可风险（高，须许可关裁定）

DrissionPage 4.1.1.4 随包 `LICENSE` 文件**不是** BSD/MIT/Apache 等宽松开源协议，而是一份
**自定义限制性许可**，关键限制原文如下：

> 「允许任何人以个人身份使用或分发本项目源代码，但**仅限于学习和合法非盈利目的**。
> 个人或组织如未获得版权持有人授权，**不得将本项目以源代码或二进制形式用于商业行为**。」
>
> 其余强制条款：禁止用于违法/有损他人利益/攻击骚扰的项目；
> 「**遵守 Robots 协议，禁止用于采集法律或系统 Robots 协议不允许的数据**」；
> **违反任一条款授权自动失效**。

### 许可结论（2026-06-28，据 Lulu 实际定位）

Lulu 定位经确认为：**免费、无会员/无充值、无广告、无云端、用户数据全部本地、
不上架应用商店、不上线公开平台；仅开发者个人使用，并免费分享给圈内朋友。**

据此对照 DrissionPage 许可：

- 该用途属 **个人、非盈利的使用与免费分发**，**不构成许可所禁止的「商业行为」** →
  其「非商业」条款下,路 B 打包使用 DrissionPage **在许可上成立**,无需降级或更换。
- 剩余客观风险(非许可层、属用户自担的风险判断,已知情接受用于个人小范围):
  「遵守 Robots 协议」条款 + 「采集抖音」本身触及抖音服务条款的灰色地带
  (与 a_bogus 逆向同源风险)。对个人/朋友圈自用实际风险低,但客观存在。
- 财务侧:DrissionPage 从「商业受限依赖」降为「个人免费工具依赖」,备案登记即可。

> 完整许可全文见 DrissionPage 4.1.1.4 包内 `LICENSE`（中英双语）。
> 若未来 Lulu 定位变更(如商业化/上架),本结论失效,须重新走许可关。
