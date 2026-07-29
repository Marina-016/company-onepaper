# 公司一页纸深度研究报告 Skill

面向A股买方机构基金经理的深度研究报告生成工具。输入股票名称或代码，自动调用通联数据（Datayes）接口，一键生成包含公司近况、核心逻辑、盈利预测与估值等10个章节的机构级研报，同时输出 Markdown 和排版规范的 Word 文件。

---

## 安装步骤

### 1. 安装 Python 依赖

```bash
pip install requests python-docx
```

> **系统要求**：Python 3.8+

### 2. （推荐）保存 Datayes Token

将 token 写入配置文件，避免每次手动输入：

```bash
# macOS / Linux
echo "你的token字符串" > ~/token.txt

# Windows（cmd）
echo 你的token字符串 > %USERPROFILE%\token.txt
```

如不保存，每次使用时 Skill 会提示输入 token。不要把真实 token 写入 Skill 源码、README 或示例文件，也不要提交 `.env` 文件。

---

## 使用方法

在输入框中直接输入股票名称或代码：

```
帮我生成贵州茅台的一页纸报告
```

或指定关注点：

```
生成 600519 的一页纸，重点关注海外扩张进展
```

---

## 输出文件

报告直接写入磁盘，不在对话窗口输出正文；对话窗口只显示进度和最终文件路径。

| 类型 | 说明 |
|:-----|:-----|
| **Markdown（.md）** | 含完整图表和引用标注，写入当前工作目录 |
| **Word（.docx）** | 规范排版，写入当前工作目录 |

Word 文档规格：微软雅黑/Calibri，正文 10.5pt，1.2 倍行距，页边距 0.71"/0.83"。

---

## 报告标题格式

```
{股票简称}（{股票代码}）公司一页纸：{一句话核心结论}
```

示例：
```
贵州茅台（600519）公司一页纸：直销渗透加速叠加批价修复预期，高股东回报
```

结论部分约 20 字，须在语义完整处自然结尾，体现最核心驱动力或投资判断。

---

## 报告章节

1. 公司近况跟踪（⭐⭐⭐最重要）
2. 核心投资逻辑（⭐⭐⭐最重要）
3. 催化事件时间表
4. 公司业务拆分（含核心竞争力）
5. 产销链分析
6. 公司财务数据分析
7. 公司调研大纲
8. 行业分析及同业对比
9. 一致预期、盈利预测与估值（含情景推演）
10. 风险提示
11. 参考资料

---

## 平台兼容性

`report_writer.py` 采用纯 HTTP 请求，支持 OpenAI-compatible 和 Anthropic Messages API 端点，**不依赖 anthropic / openai SDK**，可在 Claude Code、Workbuddy、Codex、Qoder 等平台运行。脚本只会读取你显式提供的命令行参数、环境变量、脚本同目录 `.env`，或常见本机模型配置文件；不会也不应该从智能平台内部会话中提取不可见凭据。

### LLM 配置方式（按优先级）

**方式 1：命令行参数**
```bash
python report_writer.py --api-key your_key --base-url https://your-endpoint/v1 --data xxx.json ...
```

**方式 2：脚本同目录 `.env` 文件**
```ini
OPENAI_API_KEY=your_key
OPENAI_BASE_URL=https://your-endpoint/v1
OPENAI_MODEL_NAME=gpt-4o
```

`.env` 已被 `.gitignore` 忽略，适合放在本机使用；`.env.example` 只能保留占位符。

**方式 3：环境变量**
```bash
# OpenAI-compatible 平台
export OPENAI_API_KEY=...
export OPENAI_BASE_URL=...   # 不填则默认 api.openai.com

# Anthropic 直连
export ANTHROPIC_API_KEY=sk-ant-...
```

脚本还会识别 Qoder、Workbuddy、Codex、OpenClaw / datayesclaw 等常见变量别名，例如 `QODER_API_KEY`、`WORKBUDDY_API_KEY`、`CODEX_API_KEY`、`OPENCLAW_API_KEY`、`DATAYESCLAW_API_KEY`，以及对应的 `*_BASE_URL` / `*_ENDPOINT`。

**方式 4：本机模型配置文件**

脚本会尝试读取以下常见位置的 `models.json`，并仅打印配置来源、端点和模型名，不打印完整 key：

```text
~/.datayesclaw/agents/main/agent/models.json
~/.openclaw/agents/main/agent/models.json
~/.qoder/agents/main/agent/models.json
~/.workbuddy/agents/main/agent/models.json
~/.codex/models.json
~/.config/codex/models.json
~/.claude/models.json
~/.config/claude/models.json
```

如果某个平台没有把 API key 暴露为环境变量，也没有本机可读的模型配置文件，则需要由用户显式提供独立的 LLM API key。不要将平台登录态或内部 token 写入仓库。

### 端点自动推断规则

| 条件 | 使用端点 | 调用格式 |
|:-----|:---------|:---------|
| 有 `OPENAI_API_KEY`、`QODER_API_KEY`、`WORKBUDDY_API_KEY`、`CODEX_API_KEY` 等 OpenAI-compatible key | 对应 `*_BASE_URL`（默认 api.openai.com） | OpenAI chat/completions |
| 有 `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN` | `ANTHROPIC_BASE_URL`（默认 api.anthropic.com） | Anthropic Messages API |
| `--api-key sk-ant-...` | api.anthropic.com/v1 | Anthropic Messages API |

---

## 数据来源

所有数据通过 [通联数据（Datayes）](https://ai.datayes.com) API 获取，需要有效的 Datayes token。

> 如需申请 token，请登录 [https://ai.datayes.com](https://ai.datayes.com) 注册获取。

---

## 字体说明

| 平台 | 中文字体 | 英文字体 |
|:-----|:---------|:---------|
| Windows | 微软雅黑 | Calibri |
| macOS | PingFang SC | Calibri |
| Linux | Noto Sans CJK SC | Calibri |

Linux 用户如遇字体缺失，可安装：`sudo apt install fonts-noto-cjk`

---

## 常见问题

**Q: 运行脚本时报错 `python-docx not installed`**  
A: 执行 `pip install python-docx`

**Q: Token 无效或返回 401**  
A: 检查 token 是否完整，确认通联数据账号有效期

**Q: Word 文档中文显示为方块**  
A: 系统缺少对应中文字体，参考上方字体说明安装

**Q: 某接口返回空数据**  
A: 报告末尾会列出所有失败接口的 curl 命令，可手动验证

**Q: report_writer.py 提示"未找到 API Key"**  
A: 按上方"LLM 配置方式"设置环境变量，或在脚本同目录创建 `.env` 文件；脚本会打印当前环境中检测到的相关变量供排查。脚本不会读取平台内部不可见登录凭据。

**Q: 在非 Claude Code 平台（Workbuddy/Codex 等）运行**  
A: 设置 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`，或对应平台变量如 `QODER_API_KEY` / `QODER_BASE_URL`、`WORKBUDDY_API_KEY` / `WORKBUDDY_BASE_URL`、`CODEX_API_KEY` / `CODEX_BASE_URL` 即可，无需安装任何额外 SDK。若平台没有提供可调用的 LLM API key，则需要单独配置一个可计费、可控的 API key。

**Q: Word 里没有嵌入 Markdown 图表 URL 对应的图片**
A: `markdown_to_docx.py` 会下载 `http(s)` 图片并插入 Word。下载时会带浏览器式请求头，保留远程图片后缀，并在企业证书链导致 Python 校验失败时仅对图片下载进行一次证书降级重试；失败时会输出 warning 并保留图片占位文本。
