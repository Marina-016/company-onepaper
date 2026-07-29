# 公司一页纸 Skill v1.2.4

## 版本状态

**自动修复增强版（Auto-Repair Pipeline）**——基于 v1.2.3 规则优化版，新增生成后自动修复 pipeline 和港美股自动化全链路。

## v1.2.4 新增能力

| # | 能力 | 说明 |
|---|------|------|
| 1 | A股生成后自动修复 | report_writer.py 自动扫描催化事件表和情景推演，不合格时调用 LLM 补写 |
| 2 | 港美股自动化 post-repair | hk_us_post_repair_v124.py 提供 8 项修复能力（ID 修复/稀疏行列/空表省略/催化补写/情景推演/同业比对/pipeline 清理/时效性检查） |
| 3 | 港美股自动化 Writer | hk_us_report_writer_v124.py 一键全流程：采集→LLM分章节写作→post-repair→质量检查→DOCX |
| 4 | 港美股 PIT 三表聚合 | hk_financials.py 从 PIT 三大报表聚合 FY2023-2025 主要指标 |
| 5 | 近况跟踪句首加粗规范 | §2 每条 bullet 以 `**加粗关键词**` 开头 + 具体数字 + `[N]` |
| 6 | 参考资料时效性约束 | 优先近1个月来源，超6个月须标注原因；估值/预测/催化过旧→P1 |
| 7 | §11.2 整节省略规则 | 空预测表→P1，应整节省略；post-repair 可自动删除 |
| 8 | 港美股特殊行业适配 | 保险/银行/科技/资源/REITs/生物医药指标体系已补齐 |
| 9 | 质量门禁重构 | check_report_quality_v124.py 采用 8 层 Gate 架构（Delivery→Structure→Citation→Data→Section→Table→Market→Hygiene） |

## 文件结构

```
skills/v1.2.4/
├── SKILL.md                                  # 主 Skill 定义（含 v1.2.3 全部规则 + v1.2.4 新增）
├── README.md                                 # 本文件
├── CHANGELOG.md                              # 版本变更记录
├── references/
│   ├── a-share-report-structure.md           # A股报告结构（含 v1.2.3 规则）
│   ├── a-share-quality-checklist.md          # A股质量自检清单（含 v1.2.4 新增项）
│   ├── a-share-api-interfaces.md             # A股 API 接口说明
│   ├── hk-us-report-structure.md             # 港美股报告结构（含 v1.2.4 加粗规范+行业适配）
│   ├── hk-us-quality-checklist.md            # 港美股质量自检清单（含 v1.2.4 新增项）
│   ├── hk-us-api-playbook.md                 # 港美股 API 编排手册
│   └── report-verification-prompt.md         # 报告核验提示词
└── scripts/
    ├── fetch_data.py                         # A股数据采集（含 resolve-only 模式）
    ├── report_writer.py                      # A股报告生成（含 v1.2.4 自动修复）
    ├── fetch_materials.py                    # 港美股素材采集
    ├── hk_us_report_writer_v124.py           # 🆕 港美股自动化全流程 Writer
    ├── hk_us_post_repair_v124.py             # 🆕 港美股报告后修复脚本
    ├── hk_financials.py                      # 🆕 港美股 PIT 三表聚合
    ├── check_report_quality_v124.py          # 🆕 质量门禁（8 层 Gate 架构）
    ├── check_report_quality_v123.py          # 旧版质量门禁（保留供 A股 pipeline 兼容）
    ├── build_docx.py                         # MD→DOCX 转换
    ├── markdown_to_docx.py                   # MD→DOCX 底层库
    ├── gen_charts.py                         # 图表生成（独立工具）
    ├── clean_final_markdown_v123.py          # MD 后处理清理
    ├── fetch_materials_v2.py                 # Materials V2 独立查询工具
    └── requirements.txt                      # Python 依赖
```

## 与其他版本的关系

| 版本 | 状态 | 说明 |
|------|------|------|
| v1.2.0 | 冻结 | 不修改 |
| v1.2.1 | 锁定 RC | 不修改 |
| v1.2.2 | Pipeline 实验版 | 保留原样 |
| v1.2.3 | 规则优化版 | 保留原样，规则层已固化为 v1.2.4 基础 |
| **v1.2.4** | **自动修复增强版** | **本版本**——pipeline 自动化 + 质量门禁重构 |
| v1.3.0 | 规划中 | 下一阶段 |

## 验证状态

- [ ] 港美股 fresh generation 验证（TSLA / NVDA / 00700）
- [ ] A股自动修复验证（贵州茅台 600519）
- [ ] 港美股 post-repair 独立运行验证

## 注意事项

- v1.2.4 新增脚本全部以 `_v124.py` 后缀命名，避免与其他版本混淆
- `check_report_quality_v123.py` 在 v1.2.4 中被增量修改（+check 37/38/39），A股 pipeline 仍引用此版本
- `check_report_quality_v124.py` 为全新 8 层 Gate 架构实现，港美股 pipeline 使用
- 不修改 v1.2.0、v1.2.1、v1.2.2、v1.2.3 任何文件
