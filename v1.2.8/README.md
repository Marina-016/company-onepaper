# 公司一页纸 Skill v1.2.8

## 版本状态

**v1.2.8 标题后置生成版**——基于 v1.2.6，重构 A 股/港美股标题生成为"读全文后生成"，移除所有硬编码兜底，统一降级策略为关键词拼接。

## v1.2.8 变更（基于 v1.2.6）

| # | 变更 | 说明 |
|---|------|------|
| 1 | A 股标题后置生成 | 标题 LLM context 从 s1+s2(800字) → s1+s2+s5(1300字)，全文生成后基于跨章节内容凝练一句话结论 |
| 2 | 港美股标题后置生成 | 新增 `_gen_full_context_title`，收集 s12+s34+s57(≤1800字) 全文生成标题；删除 `_derive_title_conclusion` / `_repair_title_from_verified_sections` 等旧降级链 |
| 3 | 移除硬编码兜底 | A 股 `_fallback_title_conclusion` 清空中际旭创特例及通用兜底；港美股原已为空 |
| 4 | 统一降级策略 | 两边统一为"全文 LLM → `_build_deterministic_fallback_title` 关键词拼接"，不再有公司级特例 |
| 5 | 港美股引用全角修复 | `normalize_refs` 入口加 `re.sub(r'【(\d+)】', r'[\1]', text)`，修复 LLM 全角括号 `【N】`导致的重复引用 |
| 6 | A 股 `None` 防护 | `_compact_reports` / `gen_peer_table` 中 `r['abstract']` / `r['text']` 为 `None` 时加 `or ''` 兜底 |

## 文件结构

```
skills/v1.2.8/
├── SKILL.md                                  # 主 Skill 定义
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
| v1.2.6 | 修复与增强版 | 标题兜底/同业表误删 + 业务树状缩进 + §3/§9 增强 |
| **v1.2.8** | **标题后置生成版** | **本版本**——A股/港美股标题全文后置生成 + 移除硬编码 + 统一兜底 + 引用全角修复 |
| v1.3.0 | 规划中 | 下一阶段 |

## 验证状态

- [x] 三市场标题质量验证（茅台/宁德、腾讯/美团、Apple/Tesla，6/6 LLM 全文生成成功）

## 注意事项

- v1.2.5 当前脚本以实际文件结构为准，不保留已删除 post-repair/checker 占位。
- 港美股没有独立 post-repair/checker 阻断链路；质量控制集中在 writer 内置校验和 `generation_status.json`。
- 不修改 v1.2.0、v1.2.1、v1.2.2、v1.2.3 任何文件
