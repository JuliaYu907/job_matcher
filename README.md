# LinkedIn Job Matcher

自动化 LinkedIn 职位搜索、简历匹配与求职时间线规划工具。

Automated LinkedIn job search, resume matching & application timeline advisor.

## Features

- **LinkedIn 职位抓取** — 根据关键词和地点自动抓取 LinkedIn 公开职位
- **TF-IDF 语义匹配** — 基于 TF-IDF + 余弦相似度的简历-职位智能匹配评分
- **多维度评分** — 标题匹配、关键词命中、公司偏好、职位资历、语义相似度等 8 维打分
- **多简历支持** — 同时加载多份简历（如 TPM/PMO/AI PM 版本），每个职位自动选最佳匹配
- **JD 详情抓取** — 对高分职位自动抓取完整 JD，二次评分提升精度
- **求职时间线** — 根据目标入职日期和各公司招聘周期，推荐最佳投递窗口
- **面试流程参考** — 内置 30+ 家知名公司面试流程和建议
- **邮件报告** — 支持 SMTP 邮件发送匹配报告
- **Markdown 报告** — 生成结构化 Markdown 报告，按 S/A/B/C 分级

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/your-username/job_matcher.git
cd job_matcher
pip install -r requirements.txt
```

### 2. Prepare Your Resume

将你的简历保存为 Markdown 文件，放入 `source/` 目录：

```bash
cp source/resume_example.md source/my_resume.md
# Edit source/my_resume.md with your real resume content
```

支持多简历：将多份 `.md` 简历放入 `source/` 目录，工具会自动加载并对每个职位选择最佳匹配。

### 3. Configure

```bash
cp scripts/config.example.json scripts/config.json
# Edit scripts/config.json — customize search keywords, location, companies, etc.
```

> **Note:** 默认配置 (`config.example.json`) 适用于**偏好外企的 PM（项目/项目集经理）岗位**，搜索关键词、优先公司列表、评分权重等均围绕该方向预设。如果你的目标岗位不同（如研发、设计、运营等），请根据自身情况调整 `keywords`、`priority_keywords`、`preferred_companies` 等字段。

**Key settings to customize:**

| Field | Description |
|---|---|
| `search.location` / `locations` | 搜索地点（如 `"Shanghai, China"`） |
| `search.keywords` | 核心搜索关键词 |
| `search.extended_keywords` | 扩展方向关键词 |
| `matching.priority_keywords` | 评分优先关键词（根据你的技能调整） |
| `matching.preferred_companies` | 偏好公司列表（命中加分） |
| `timeline.target_start_date` | 目标入职日期 |
| `email.*` | 邮件发送配置（可选） |
| `analysis.*` | 匹配分析文案（可选，见下文） |

#### Customizing Match Analysis (Optional)

报告中每个职位的"匹配分析"文案默认面向 PM 岗位。如果你的目标岗位不同，可以在 config 中添加 `analysis` 来覆盖：

```json
{
  "analysis": {
    "keyword_insights": {
      "data scientist": "数据科学职位与简历中的数据分析和建模经验匹配",
      "machine learning": "ML 方向与简历中的机器学习项目经验对标",
      "python": "Python 技术栈与简历中的编程经验一致"
    },
    "company_insights": {
      "google": "Google 重视算法能力和大规模数据处理经验",
      "bytedance": "字节跳动推荐算法团队对 ML 工程能力要求高"
    },
    "gap_patterns": [
      {
        "keywords": ["biotech", "pharmaceutical"],
        "gap": "生物医药行业经验可能缺失",
        "suggestion": "强调数据分析方法论的跨行业可迁移性"
      }
    ],
    "fallback_strength": "核心数据分析能力可迁移",
    "no_title_hit_gap": "⚠️ 职位标题未直接命中目标关键词——投递时在 Cover Letter 中明确对标相关经验"
  }
}
```

| Field | Description |
|---|---|
| `keyword_insights` | 关键词 → 匹配说明。key 应与 `priority_keywords` 中的词对应 |
| `company_insights` | 公司名(小写) → 公司特点分析 |
| `gap_patterns` | 行业差距识别：JD 中出现这些关键词时提示可能的短板和建议 |
| `fallback_strength` | 当没有任何关键词命中时的兜底文案 |
| `no_title_hit_gap` | 当职位标题未命中 boost 关键词时的提示文案 |

所有字段均为可选——省略整个 `analysis` 则使用内置的 PM 默认文案。

### 4. Run

```bash
cd scripts
python job_matcher.py                       # Default run
python job_matcher.py --config my.json      # Custom config
python job_matcher.py --send-email          # Run and send email report
python job_matcher.py --dry-run             # Scrape only, no file output
```

Reports are saved to `reports/YYYYMMDD/`.

### 5. Email Setup (Optional)

To receive reports via email:

1. Enable SMTP in your email provider (e.g., 163.com → Settings → POP3/SMTP/IMAP)
2. Generate an authorization code
3. Set the environment variable:
   ```bash
   # Windows
   set EMAIL_AUTH_CODE=your_auth_code

   # Linux/Mac
   export EMAIL_AUTH_CODE=your_auth_code
   ```
4. Update `email` section in `config.json`
5. Run with `--send-email`

### 6. Scheduled Runs (Optional)

**Windows Task Scheduler:**
```
schtasks /create /tn "JobMatcher" /tr "python C:\path\to\scripts\job_matcher.py --send-email" /sc weekly /d MON /st 09:00
```

**Linux/Mac cron:**
```
0 9 * * 1 cd /path/to/job_matcher/scripts && python job_matcher.py --send-email
```

## Project Structure

```
job_matcher/
├── scripts/
│   ├── job_matcher.py          # Main script
│   ├── config.json             # Your config (gitignored)
│   └── config.example.json     # Example config (committed)
├── source/
│   ├── resume_example.md       # Example resume template
│   └── *.md                    # Your resumes (gitignored)
├── reports/                    # Generated reports (gitignored)
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## Scoring System

Each job is scored across 8 dimensions (max ~131 points):

| Dimension | Max Points | Description |
|---|---|---|
| Title Match | 25 | 职位标题关键词命中 |
| Priority Keywords | 30 | JD 中优先关键词匹配数 |
| Resume Bold Phrases | 16 | 简历加粗关键词在 JD 中命中 |
| Preferred Company | 12 | 偏好公司列表命中 |
| Actively Hiring | 5 | LinkedIn "积极招聘" 标记 |
| Recency | 5 | 发布时间新鲜度 |
| Seniority | 8 | 资历级别匹配 |
| TF-IDF Similarity | 30 | 简历-JD 语义相似度 |

Tier classification: **S** >= 75, **A** >= 60, **B** >= 45, **C** < 45

## Requirements

- Python 3.10+
- Dependencies: `requests`, `beautifulsoup4`, `scikit-learn`

## License

MIT
