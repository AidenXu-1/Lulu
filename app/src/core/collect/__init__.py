"""抖音作者主页批量采集(组合拳)。

见 docs/spec/douyin-author-bulk-collection.md、ADR-0013。
- models:采集条目数据模型(9 项数据)。
- export:采集结果导出(CSV/Excel),供用户导入飞书等表格工具。
- 签名/网络抓取层(a_bogus)单独成模块,过安全关后接入(本期 spec §9)。
"""
