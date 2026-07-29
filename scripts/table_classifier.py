#!/usr/bin/env python3
"""
v1.2.2 表格分类器 — 共享模块，post_process 和 evaluator 共用

分类规则：
  NUMERIC_DATA       — 数值型数据表 → 执行稀疏行列规则
  QUALITATIVE        — 纯定性表（时间线/情景/分歧/调研等）→ 不执行数值稀疏规则
  MIXED              — 混合型 → 仅对数值列执行稀疏规则

分类依据：表头列名的语义类别
"""
from dataclasses import dataclass
from enum import Enum
from typing import Any


class TableType(Enum):
    NUMERIC_DATA = "numeric_data"
    QUALITATIVE = "qualitative"
    MIXED = "mixed"


# ── Column semantic tags ──

NUMERIC_COLUMN_PATTERNS = [
    "营收", "收入", "利润", "净利", "毛利", "EPS", "ROE", "ROA",
    "市值", "估值", "PE", "PB", "PS", "EV", "P/EV", "股息率",
    "增速", "增长率", "同比", "YOY", "yoy", "占比", "份额",
    "毛利率", "净利率", "营业利润率", "资产负债率",
    "经营现金流", "资本开支", "总资产", "负债",
    "FY20", "FY19", "202", "Q1", "Q2", "Q3", "Q4", "H1", "H2",
    "实际", "预测", "预期", "一致预期", "目标",
    "产量", "销量", "产能", "单价", "成本",
    "金额", "亿元", "亿美元", "十亿", "百万",
    "FYP", "NBV", "VONB", "EV", "OPAT", "APE", "CSM",
]

QUALITATIVE_COLUMN_PATTERNS = [
    "时间", "事件", "影响", "催化剂",
    "情景", "核心假设", "概率",
    "多头", "空头", "看多", "看空", "观点", "分歧", "论点",
    "估值方法", "解读", "说明", "备注",
    "调研", "关注", "待办", "问题",
    "竞争关系", "可比业务", "行业地位", "商业模式",
    "风险", "风险因素", "影响程度",
    "来源", "发布日期", "API",
    "核心看点", "核心业务", "驱动因素",
    "目标客户", "核心产品", "需要观察", "验证点",
    "市场", "benchmark", "备注",
]


@dataclass
class TableClassification:
    table_type: TableType
    numeric_column_indices: list[int]   # 0-based indices of numeric columns
    qualitative_column_indices: list[int]
    header_labels: list[str]
    reason: str


def classify_table(header: list[str]) -> TableClassification:
    """根据表头列名分类表格类型。

    返回 TableClassification 包含每列的数值/定性标记。
    """
    header_clean = [h.strip() for h in header]
    numeric_indices = []
    qualitative_indices = []

    for i, col in enumerate(header_clean):
        is_numeric = False
        is_qual = False

        for pat in NUMERIC_COLUMN_PATTERNS:
            if pat.lower() in col.lower():
                is_numeric = True
                break

        for pat in QUALITATIVE_COLUMN_PATTERNS:
            if pat.lower() in col.lower():
                is_qual = True
                break

        if is_numeric and not is_qual:
            numeric_indices.append(i)
        elif is_qual:
            qualitative_indices.append(i)
        # If neither matched, column is unclassified (treated as qualitative)

    num_numeric = len(numeric_indices)
    num_qual = len(qualitative_indices)
    total = len(header_clean)

    # Classification logic
    if num_numeric >= 2 and num_qual == 0:
        ttype = TableType.NUMERIC_DATA
        reason = f"All {num_numeric}/{total} classified columns are numeric"
    elif num_qual >= 2 and num_numeric == 0:
        ttype = TableType.QUALITATIVE
        reason = f"All {num_qual}/{total} classified columns are qualitative"
    elif num_numeric > 0 and num_qual > 0:
        ttype = TableType.MIXED
        reason = f"{num_numeric} numeric + {num_qual} qualitative columns"
    elif num_numeric >= 1:
        ttype = TableType.NUMERIC_DATA
        reason = f"{num_numeric} numeric columns, no qualitative detected"
    else:
        ttype = TableType.QUALITATIVE
        reason = f"No numeric columns classified"

    return TableClassification(
        table_type=ttype,
        numeric_column_indices=numeric_indices,
        qualitative_column_indices=qualitative_indices,
        header_labels=header_clean,
        reason=reason,
    )


def should_apply_sparse_rules(classification: TableClassification) -> bool:
    """是否应对该表格执行数值型稀疏行列规则。"""
    return classification.table_type in (TableType.NUMERIC_DATA, TableType.MIXED)


def get_checkable_columns(classification: TableClassification) -> set[int]:
    """返回应该检查稀疏性的列索引集合（仅数值列）。"""
    if classification.table_type == TableType.QUALITATIVE:
        return set()
    if classification.table_type == TableType.MIXED:
        return set(classification.numeric_column_indices)
    # NUMERIC_DATA: check all columns except the first (label column)
    return set(range(1, len(classification.header_labels)))
