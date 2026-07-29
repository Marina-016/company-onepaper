# 公司一页纸 Skill v1.2.5

## 版本状态

**港美股自动化 Writer 整理版**——基于 v1.2.3 规则优化版，保留 A 股生成后自动修复，港美股改为脚本内置校验、fail closed 和连续重编号。

## v1.2.5 新增能力

| # | 能力 | 说明 |
|---|------|------|
| 1 | A股生成后自动修复 | a_share_report_writer.py 自动扫描催化事件表和情景推演，不合格时调用 LLM 补写 |
| 2 | 港美股自动化 Writer | hk_us_report_writer.py 一键全流程：采集→LLM分章节写作→内置校验→连续重编号→DOCX |
| 3 | 港股 PIT 三表聚合 | hk_us_report_writer.py 内置使用 PIT 三大报表聚合主要指标 |
| 4 | 近况跟踪句首加粗规范 | §2 每条 bullet 以 `**加粗关键词**` 开头 + 具体数字 + `[N]` |
| 5 | 参考资料时效性约束 | 优先近1个月来源，超6个月须标注原因；估值/预测/催化过旧→记录降级 |
| 6 | §11.2 整节省略规则 | 空预测表由 writer 省略本节或整章并重编号 |
| 7 | 港美股特殊行业适配 | 保险/银行/科技/资源/REITs/生物医药指标体系已补齐 |
| 8 | 质量策略收敛 | 港美股 §9/§10 高风险兜底改为 fail closed，避免原文硬搬和张冠李戴 |

## 文件结构

```
skills/v1.2.5/
├── SKILL.md                                  # 主 Skill 定义（含 v1.2.3 全部规则 + v1.2.5 新增）
├── README.md                                 # 本文件
├── CHANGELOG.md                              # 版本变更记录
├── references/
│   ├── a-share-report-structure.md           # A股报告结构（含 v1.2.3 规则）
│   ├── a-share-quality-checklist.md          # A股质量自检清单（含 v1.2.5 新增项）
│   ├── a-share-api-interfaces.md             # A股 API 接口说明
│   ├── hk-us-report-structure.md             # 港美股报告结构（含 v1.2.5 加粗规范+行业适配）
│   ├── hk-us-quality-checklist.md            # 港美股质量自检清单（含 v1.2.5 新增项）
│   └── hk-us-api-playbook.md                 # 港美股 API 编排手册
└── scripts/
    ├── a_share_fetch_data.py                         # A股数据采集（含 resolve-only 模式）
    ├── a_share_report_writer.py                      # A股报告生成（含 v1.2.5 自动修复）
    ├── fetch_materials.py                    # 港美股素材采集
    ├── hk_us_report_writer.py                # 港美股自动化全流程 Writer
    ├── build_docx.py                         # MD→DOCX 转换
    ├── markdown_to_docx.py                   # MD→DOCX 底层库
    ├── gen_charts.py                         # 图表生成（独立工具）
    ├── fetch_materials_v2.py                 # Materials V2 独立查询工具
    └── requirements.txt                      # Python 依赖
```

## 与其他版本的关系

| 版本 | 状态 | 说明 |
|------|------|------|
| v1.2.0 | 冻结 | 不修改 |
| v1.2.1 | 锁定 RC | 不修改 |
| v1.2.2 | Pipeline 实验版 | 保留原样 |
| v1.2.3 | 规则优化版 | 保留原样，规则层已固化为 v1.2.5 基础 |
| **v1.2.5** | **港美股 Writer 整理版** | **本版本**——内置校验 + fail closed + 连续重编号 |
| v1.3.0 | 规划中 | 下一阶段 |

## 验证状态

- [ ] 港美股 fresh generation 验证
- [ ] A股自动修复验证（贵州茅台 600519）
- [ ] 港美股 fail closed 与连续重编号验证

## 注意事项

- v1.2.5 当前脚本以实际文件结构为准，不保留已删除 post-repair/checker 占位。
- 港美股没有独立 post-repair/checker 阻断链路；质量控制集中在 writer 内置校验和 `generation_status.json`。
- 不修改 v1.2.0、v1.2.1、v1.2.2、v1.2.3 任何文件
