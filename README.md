# 公司一页纸 Skill v1.2.6

## 版本状态

**v1.2.6 修复与增强版**——基于 v1.2.5 港美股 Writer 整理版，修复标题兜底、同业表误删、业务树状缩进、§3 投资逻辑深度、§9 peer 校验降级。

## v1.2.6 变更（基于 v1.2.5）

| # | 变更 | 说明 |
|---|------|------|
| 1 | 标题兜底修复 | DeepSeek-V4 thinking 吃光 token → 无文本输出 → 落硬编码兜底；payload 加 `"thinking": {"type": "disabled"}` |
| 2 | 同业表误删修复 | `_is_numeric_cell` 把"2026年第一季度…"误判为数值 → `_sparse_cleanup` 删全表；排除含中文单元格 |
| 3 | 业务树状缩进 | §4.2 分板块业务数据一/二/三级用 `├`/`│ ├` 前缀，层级一目了然 |
| 4 | 港美股 §3 投资逻辑增强 | 固定 3 点→2-4 点，每点 80-150 字→140-220 字，max_tokens 2200→3500 |
| 5 | §9 peer 校验降级 | `peer_row_company_not_in_evidence` 从 block 改非阻塞 warning |

## 文件结构

```
skills/v1.2.6/
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
| v1.2.5 | 港美股 Writer 整理版 | 冻结——内置校验 + fail closed + 连续重编号 |
| **v1.2.6** | **修复与增强版** | **本版本**——修复标题兜底/同业表误删 + 业务树状缩进 + §3/§9 增强 |
| v1.3.0 | 规划中 | 下一阶段 |

## 验证状态

- [ ] 港美股 fresh generation 验证
- [ ] A股自动修复验证（贵州茅台 600519）
- [ ] 港美股 fail closed 与连续重编号验证

## 注意事项

- v1.2.5 当前脚本以实际文件结构为准，不保留已删除 post-repair/checker 占位。
- 港美股没有独立 post-repair/checker 阻断链路；质量控制集中在 writer 内置校验和 `generation_status.json`。
- 不修改 v1.2.0、v1.2.1、v1.2.2、v1.2.3 任何文件
