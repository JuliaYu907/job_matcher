"""
LinkedIn Job Matcher — Automated job search, resume matching & application timeline advisor.

Features:
  - TF-IDF semantic matching between resume and job descriptions
  - JD detail fetching for deeper scoring on qualifying jobs
  - Foreign-company (外企) preferred list with broad coverage
  - Unified HW/SW PM weighting

Usage:
    python job_matcher.py                   # Run with default config.json
    python job_matcher.py --config my.json  # Custom config
    python job_matcher.py --send-email      # Run and send email report
    python job_matcher.py --dry-run         # Scrape only, no file/email output

Scheduling (Windows Task Scheduler):
    schtasks /create /tn "JobMatcher" /tr "python C:\\path\\to\\job_matcher.py --send-email" /sc weekly /d MON /st 09:00

Scheduling (cron on Linux/Mac):
    0 9 * * 1 cd /path/to && python job_matcher.py --send-email
"""

import argparse
import copy
import json
import os
import re
import sys
import smtplib
import ssl
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "config.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
LINKEDIN_SEARCH_URL = "https://www.linkedin.com/jobs/search"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def load_config(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve_path(cfg_path: str, base: Path) -> Path:
    """Resolve a path that may be relative to the config file directory."""
    p = Path(cfg_path)
    if not p.is_absolute():
        p = (base / p).resolve()
    return p


# ---------------------------------------------------------------------------
# Resume parsing
# ---------------------------------------------------------------------------
def load_resume(path: Path) -> dict:
    """Parse a markdown resume and extract structured data for matching."""
    text = path.read_text(encoding="utf-8")
    lower = text.lower()

    # Extract all meaningful words (>= 2 chars)
    words = set(re.findall(r"[a-z]{2,}", lower))
    # Also keep multi-word phrases from bold text
    bold_phrases = re.findall(r"\*\*(.+?)\*\*", text)

    # Extract specific sections
    sections = {}
    current = None
    for line in text.splitlines():
        heading = re.match(r"^#{1,3}\s+(.+)", line)
        if heading:
            current = heading.group(1).strip()
            sections[current] = []
        elif current:
            sections[current] = sections.get(current, [])
            sections[current].append(line)

    # Build a clean text corpus for TF-IDF (strip markdown formatting)
    clean = re.sub(r"[#*\[\]()|\-_>]", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()

    return {
        "raw": text,
        "clean": clean,
        "words": words,
        "bold_phrases": [p.lower() for p in bold_phrases],
        "sections": sections,
    }


# ---------------------------------------------------------------------------
# TF-IDF Semantic Matching
# ---------------------------------------------------------------------------
class SemanticMatcher:
    """TF-IDF + cosine similarity matcher for resume-to-JD scoring."""

    def __init__(self, resume_clean: str):
        self._vectorizer = TfidfVectorizer(
            stop_words="english",
            max_features=5000,
            ngram_range=(1, 2),
            sublinear_tf=True,
        )
        # Pre-fit with resume so the vocabulary includes resume terms
        self._resume_text = resume_clean
        self._fitted = False

    def score(self, job_text: str) -> float:
        """Return cosine similarity [0, 1] between resume and job text."""
        if not job_text or len(job_text.split()) < 3:
            return 0.0
        try:
            corpus = [self._resume_text, job_text]
            tfidf_matrix = self._vectorizer.fit_transform(corpus)
            sim = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:2])[0][0]
            return float(sim)
        except Exception:
            return 0.0


# ---------------------------------------------------------------------------
# LinkedIn scraper
# ---------------------------------------------------------------------------
def scrape_linkedin_jobs(keyword: str, location: str, max_results: int = 50) -> list[dict]:
    """Scrape LinkedIn public job search results."""
    jobs = []
    params = {
        "keywords": keyword,
        "location": location,
        "trk": "public_jobs_jobs-search-bar_search-submit",
        "position": 1,
        "pageNum": 0,
    }
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}

    try:
        resp = requests.get(LINKEDIN_SEARCH_URL, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [WARN] Failed to fetch '{keyword}': {e}")
        return jobs

    soup = BeautifulSoup(resp.text, "html.parser")

    # LinkedIn public search uses <ul class="jobs-search__results-list"> and <li> items
    cards = soup.select("li")
    for card in cards:
        # Title
        title_el = card.select_one("h3")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not title:
            continue

        # Company
        company_el = card.select_one("h4") or card.select_one("a[data-tracking-control-name*='company']")
        company = company_el.get_text(strip=True) if company_el else "Unknown"

        # Location
        loc_el = card.select_one("span.job-search-card__location")
        loc = loc_el.get_text(strip=True) if loc_el else location

        # Link
        link_el = card.select_one("a[href*='/jobs/view/']") or card.select_one("a[href*='linkedin.com/jobs']")
        link = link_el["href"].split("?")[0] if link_el and link_el.get("href") else ""

        # Time
        time_el = card.select_one("time")
        posted = time_el.get_text(strip=True) if time_el else ""
        posted_dt = time_el.get("datetime", "") if time_el else ""

        # Status (Actively Hiring, etc.) — try multiple selectors for LinkedIn HTML changes
        status_el = (
            card.select_one("span.job-posting-benefits__text")
            or card.select_one("span.result-benefits__text")
        )
        status = status_el.get_text(strip=True) if status_el else ""

        jobs.append({
            "title": title,
            "company": company,
            "location": loc,
            "link": link,
            "posted": posted,
            "posted_dt": posted_dt,
            "status": status,
            "keyword": keyword,
            "jd_text": "",  # will be filled by JD fetcher
        })

        if len(jobs) >= max_results:
            break

    return jobs


def _is_too_old(job: dict, max_age_days: int) -> bool:
    """Return True if the job posting is older than max_age_days."""
    dt_str = job.get("posted_dt", "")
    if not dt_str:
        return False  # keep jobs without date info
    try:
        posted_date = datetime.strptime(dt_str[:10], "%Y-%m-%d")
        return (datetime.now() - posted_date).days > max_age_days
    except (ValueError, TypeError):
        return False


def _location_matches(job_location: str, allowed_patterns: list[str]) -> bool:
    """Check if a job's location matches any of the allowed patterns."""
    loc_lower = job_location.lower()
    for pat in allowed_patterns:
        if pat.lower() in loc_lower:
            return True
    return False


def fetch_all_jobs(config: dict, keyword_key: str = "keywords") -> list[dict]:
    """Fetch jobs for all configured keywords and deduplicate.

    Args:
        config: Full config dict.
        keyword_key: Which key under "search" to read keywords from
                     ("keywords" for core PM, "extended_keywords" for new directions).
    """
    search = config["search"]
    kw_list = search.get(keyword_key, [])
    # Support multiple locations: use "locations" list if present, else fall back to single "location"
    locations = search.get("locations", [search.get("location", "")])
    max_age = search.get("max_age_days", 0)
    # Location filter: only keep jobs whose actual location matches these patterns
    loc_filter = search.get("location_filter", [])
    # Title exclusion filter: drop irrelevant roles (construction, intern, etc.)
    exclude_titles = [kw.lower() for kw in search.get("exclude_title_keywords", [])]
    all_jobs = []
    seen_links = set()
    filtered_count = 0
    loc_filtered_count = 0
    title_filtered_count = 0

    for location in locations:
        for kw in kw_list:
            print(f"  Searching: '{kw}' in {location}...")
            jobs = scrape_linkedin_jobs(kw, location, search.get("max_results_per_keyword", 50))
            for j in jobs:
                key = j["link"] or f"{j['title']}|{j['company']}"
                if key not in seen_links:
                    seen_links.add(key)
                    if max_age and _is_too_old(j, max_age):
                        filtered_count += 1
                        continue
                    if loc_filter and not _location_matches(j.get("location", ""), loc_filter):
                        loc_filtered_count += 1
                        continue
                    if exclude_titles and any(ex in j["title"].lower() for ex in exclude_titles):
                        title_filtered_count += 1
                        continue
                    all_jobs.append(j)
            print(f"    Found {len(jobs)} results ({len(all_jobs)} unique total)")

    if filtered_count:
        print(f"    Filtered out {filtered_count} jobs older than {max_age} days")
    if loc_filtered_count:
        print(f"    Filtered out {loc_filtered_count} jobs outside allowed locations ({', '.join(loc_filter)})")
    if title_filtered_count:
        print(f"    Filtered out {title_filtered_count} jobs by title exclusion keywords")

    return all_jobs


# ---------------------------------------------------------------------------
# JD detail fetcher
# ---------------------------------------------------------------------------
def _normalize_job_url(url: str) -> str:
    """Convert cn.linkedin.com URLs to www.linkedin.com to avoid 451 redirects."""
    # Extract job ID and use the guest API endpoint (lightweight, no auth needed)
    m = re.search(r"/jobs/view/[^/]*?(\d{8,})", url)
    if m:
        job_id = m.group(1)
        return f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    # Fallback: just swap cn. to www.
    return url.replace("://cn.linkedin.com/", "://www.linkedin.com/")


def fetch_jd_detail(url: str) -> str:
    """Fetch the full job description from a LinkedIn job page."""
    if not url:
        return ""
    api_url = _normalize_job_url(url)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    try:
        resp = requests.get(api_url, headers=headers, timeout=20, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            # Follow redirect only if it stays on linkedin.com
            loc = resp.headers.get("Location", "")
            if "linkedin.com" in loc and "linkedin.cn" not in loc:
                resp = requests.get(loc, headers=headers, timeout=20)
            else:
                return ""
        if resp.status_code != 200:
            return ""
    except requests.RequestException:
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")

    # LinkedIn public job pages put JD in a specific section
    jd_section = (
        soup.select_one("div.show-more-less-html__markup")
        or soup.select_one("div.description__text")
        or soup.select_one("section.description")
    )
    if jd_section:
        return jd_section.get_text(separator=" ", strip=True)

    # Fallback: grab all paragraph text from the page
    paragraphs = soup.find_all("p")
    text = " ".join(p.get_text(strip=True) for p in paragraphs)
    return text[:3000] if text else ""


def fetch_jd_for_qualifying_jobs(jobs: list[dict], config: dict):
    """Fetch full JD text for jobs that meet the minimum score threshold."""
    jd_cfg = config.get("jd_fetch", {})
    if not jd_cfg.get("enabled", False):
        return

    min_score = jd_cfg.get("min_score_to_fetch", 35)
    max_fetch = jd_cfg.get("max_fetch", 30)
    delay = jd_cfg.get("request_delay_sec", 1.5)

    qualifying = [j for j in jobs if j.get("score", 0) >= min_score and j.get("link")]
    qualifying = qualifying[:max_fetch]

    if not qualifying:
        print("    No jobs qualify for JD detail fetching.")
        return

    print(f"    Fetching JD details for {len(qualifying)} jobs...")
    fetched = 0
    for j in qualifying:
        jd = fetch_jd_detail(j["link"])
        if jd:
            j["jd_text"] = jd
            fetched += 1
        time.sleep(delay)
    print(f"    Successfully fetched {fetched}/{len(qualifying)} JD details.")


# ---------------------------------------------------------------------------
# Matching engine (TF-IDF + keyword hybrid)
# ---------------------------------------------------------------------------
def score_job(job: dict, resume: dict, matcher: SemanticMatcher, config: dict,
              boost_key: str = "boost_title_keywords") -> dict:
    """Score a job against the resume. Returns score breakdown dict.

    Args:
        boost_key: Config key for title boost keywords.
                   "boost_title_keywords" for core PM,
                   "extended_boost_title_keywords" for new directions.
    """
    matching = config["matching"]
    breakdown = {}
    title_lower = job["title"].lower()
    company = job["company"]

    # 1) Title keyword match (most important)
    title_match = 0
    title_hit = ""
    for kw in matching.get(boost_key, []):
        if kw.lower() in title_lower:
            title_match = 25
            title_hit = kw
            break
    breakdown["title_match"] = title_match
    breakdown["title_hit"] = title_hit

    # 2) Priority keyword match against title + company + location + JD
    job_text = f"{job['title']} {job['company']} {job['location']}".lower()
    jd_lower = (job.get("jd_text") or "").lower()
    kw_hits_list = []
    for kw in matching["priority_keywords"]:
        if kw.lower() in job_text or kw.lower() in jd_lower:
            kw_hits_list.append(kw)
    # Cap at 6 keywords (30 pts max from this source)
    breakdown["keyword_match"] = min(len(kw_hits_list), 6) * 5
    breakdown["keyword_hits"] = kw_hits_list

    # 3) Resume bold phrases match against job title
    bold_match = 0
    bold_hits = []
    for phrase in resume["bold_phrases"]:
        if len(phrase) > 3 and phrase in title_lower:
            bold_match += 8
            bold_hits.append(phrase)
    breakdown["bold_match"] = min(bold_match, 16)  # cap at 2 hits
    breakdown["bold_hits"] = bold_hits

    # 4) Preferred company bonus (foreign company / 外企)
    company_match = 0
    company_hit = ""
    for pc in matching["preferred_companies"]:
        if pc.lower() in company.lower():
            company_match = 12
            company_hit = pc
            break
    breakdown["company_match"] = company_match
    breakdown["company_hit"] = company_hit

    # 5) Actively Hiring bonus
    breakdown["actively_hiring"] = 5 if "actively hiring" in (job.get("status", "") or "").lower() else 0

    # 6) Recency bonus
    posted = job.get("posted", "").lower()
    if "hour" in posted or "day" in posted or "1 week" in posted:
        breakdown["recency"] = 5
    elif "2 week" in posted or "3 week" in posted:
        breakdown["recency"] = 2
    else:
        breakdown["recency"] = 0

    # 7) Seniority level match (Principal/Senior level)
    seniority_kws = ["senior", "sr.", "principal", "lead", "head", "director", "staff"]
    seniority_match = 0
    seniority_hit = ""
    for s in seniority_kws:
        if s in title_lower:
            seniority_match = 8
            seniority_hit = s
            break
    breakdown["seniority"] = seniority_match
    breakdown["seniority_hit"] = seniority_hit

    # 8) TF-IDF semantic similarity (title + company + JD if available)
    tfidf_input = f"{job['title']} {job['company']} {job['location']}"
    if job.get("jd_text"):
        tfidf_input += " " + job["jd_text"]
    tfidf_weight = matching.get("tfidf_weight", 30)
    tfidf_raw = matcher.score(tfidf_input)
    breakdown["tfidf_raw"] = round(tfidf_raw, 3)
    breakdown["tfidf_score"] = round(tfidf_raw * tfidf_weight, 1)

    # Total
    total = (
        breakdown["title_match"]
        + breakdown["keyword_match"]
        + breakdown["bold_match"]
        + breakdown["company_match"]
        + breakdown["actively_hiring"]
        + breakdown["recency"]
        + breakdown["seniority"]
        + breakdown["tfidf_score"]
    )
    breakdown["total"] = round(total, 1)
    return breakdown


# ---------------------------------------------------------------------------
# Match analysis generator (strengths + gaps)
# ---------------------------------------------------------------------------
# Default analysis data (PM-oriented). Override via config["analysis"].
_DEFAULT_KW_INSIGHTS = {
    "technical program manager": "TPM 职位与简历中的技术项目管理经验匹配",
    "TPM": "TPM 核心角色，与简历中的 TPM 经验对标",
    "engineering program manager": "Engineering PM 与简历中的工程项目管理经验一致",
    "hardware": "HW 方向命中简历中的硬件相关项目经验",
    "firmware": "FW 需求对应简历中的固件开发/管理经验",
    "software": "SW 方向与简历中的软件开发/管理经验互补",
    "system integration": "系统集成与简历中的跨系统整合经验匹配",
    "HW/SW": "HW/SW 集成与简历中的软硬件协同经验对标",
    "platform": "平台级产品管理与简历中的平台经验对标",
    "NPI": "NPI 与简历中的新产品导入经验匹配",
    "product release": "产品发布管理与简历中的发布交付经验对标",
    "multi-site": "多站点管理与简历中的跨地域协调经验匹配",
    "global": "全球化团队管理与简历中的跨国协作经验吻合",
    "cross-functional": "跨职能协调与简历中的多团队管理经验匹配",
    "agile": "敏捷方法论与简历中的 Agile/Scrum 实践经验对标",
    "scrum": "Scrum 经验与简历中的敏捷交付背景匹配",
    "PMP": "PMP 认证需求与简历中的项目管理认证匹配",
    "PMO": "PMO 方向与简历中的项目管理办公室经验对标",
    "AI": "AI 方向与简历中的人工智能相关经验契合",
    "automation": "自动化需求与简历中的自动化/工具开发经验对标",
    "digital transformation": "数字化转型与简历中的流程改进和技术创新经验对齐",
    "product lifecycle": "产品全生命周期管理与简历中的端到端交付经验一致",
    "release management": "发布管理与简历中的版本发布经验对标",
    "risk management": "风险管理与简历中的风险识别和控制经验匹配",
    "stakeholder": "利益相关者管理与简历中的多方协调经验对标",
    "dependency": "依赖管理与简历中的上下游协调经验匹配",
    "cross-team": "跨团队协调与简历中的多团队协作经验一致",
    "program lifecycle": "项目全生命周期管理与简历中的 E2E 交付经验对标",
}

_DEFAULT_COMPANY_INSIGHTS = {
    "nvidia": "NVIDIA 重视系统集成和 AI 领域 PM，技术深度是关键加分项",
    "amd": "半导体行业对 HW/FW 集成和多代产品管理经验需求强烈",
    "amazon": "Amazon TPM 看重强技术背景 + 跨团队协调 + Leadership Principles",
    "apple": "Apple 硬件团队需要 HW/SW/FW 跨团队交付和系统集成能力",
    "meta": "Meta 硬件团队对平台级产品化 + 系统集成 PM 需求旺盛",
    "tesla": "Tesla 工程 PM 需要 HW 产品化 + 跨职能协调，快节奏迭代",
    "cadence": "EDA 公司需要懂 SoC 和芯片流程的 TPM，系统集成经验可迁移",
    "synopsys": "EDA/IP TPM 需要理解芯片开发流程，HW 平台经验是切入点",
    "google": "Google TPM 看重技术深度 + 规模化管理能力，面试偏重系统设计",
    "microsoft": "微软 PM 需要技术 + 管理双重能力，技术背景 + PM 经验是稀缺组合",
    "siemens": "Siemens 重视流程和质量管理，ISO/质量体系经验是差异化优势",
    "medtronic": "医疗器械 PM 需要大规模发布管理和流程标准化能力",
    "shell": "Shell 项目管理需要跨职能协调和全球化经验",
    "morgan stanley": "金融科技 TPM 需要技术深度 + 项目管理双重能力",
    "bytedance": "字节 PMO 方法论通用，AI 相关经验在互联网公司是加分项",
    "intel": "芯片公司 PM 需要 HW/FW 集成和多代产品管理经验",
    "qualcomm": "芯片公司对 HW 平台 PM 的需求持续旺盛",
}

# (JD keywords, gap description, suggestion)
_DEFAULT_GAP_PATTERNS = [
    (["automotive", "autonomous driving", "adas", "av software", "electric vehicle", "ev platform"],
     "汽车/自动驾驶行业可能非核心领域",
     "投递时突出项目管理方法论的可迁移性，强调跨系统集成和测试经验"),
    (["tapeout", "foundry", "wafer", "silicon validation", "chip design", "rtl", "asic design"],
     "芯片设计/流片流程可能不是直接经验",
     "简历中强调硬件平台和系统集成经验来类比芯片产品化流程"),
    (["biotech", "pharmaceutical", "clinical trial", "drug development", "gmp", "fda approval", "medical device", "生物制药", "临床"],
     "生物医药/医疗器械行业经验可能缺失",
     "强调大型 R&D 项目管理方法论的可迁移性和质量管理经验"),
    (["investment bank", "trading system", "capital market", "hedge fund", "asset management", "wealth management"],
     "金融行业背景可能不足",
     "突出技术深度和复杂系统项目管理经验，强调技术 + 管理双重能力"),
    (["oil and gas", "petroleum", "upstream", "downstream", "lubricant", "drilling", "refinery"],
     "能源行业经验可能有限",
     "强调大型跨职能项目交付能力和 PMP 方法论的行业通用性"),
    (["director", "vp ", "vice president"],
     "向上拓展到 Director/VP 级别",
     "准备 Director 级叙事——强调团队管理幅度、项目规模和组织影响力"),
    (["head of project", "head of program", "head of pmo"],
     "向上拓展到部门负责人级别",
     "强调管理幅度和项目统筹规模，突出组织影响力和战略思维"),
    (["production line", "lean manufacturing", "six sigma", "shop floor", "factory automation"],
     "制造/工厂运营可能非日常经验",
     "突出 NPI 量产导入经验和质量管理背景，将产品化经验类比制造流程"),
    (["microservice", "kubernetes", "devops", "cloud native", "saas platform", "backend engineer"],
     "纯软件/云原生技术栈可能非核心技能",
     "强调技术背景和自动化能力，突出方法论转型和学习能力"),
    (["consulting firm", "management consulting", "advisory firm"],
     "咨询行业工作模式不同于甲方",
     "强调跨职能领导力和流程改进方法论，这些在咨询场景中是核心卖点"),
]

_DEFAULT_FALLBACK_STRENGTH = "核心专业能力可迁移"
_DEFAULT_NO_TITLE_HIT_GAP = "⚠️ 职位标题未直接命中目标关键词——投递时在 Cover Letter 中明确对标相关经验"


def _load_analysis_config(config: dict) -> dict:
    """Load analysis settings from config, falling back to built-in defaults."""
    analysis = config.get("analysis", {})
    kw_insights = analysis.get("keyword_insights", _DEFAULT_KW_INSIGHTS)
    company_insights = analysis.get("company_insights", _DEFAULT_COMPANY_INSIGHTS)

    # gap_patterns: config uses list-of-dicts for JSON compatibility
    gap_patterns_raw = analysis.get("gap_patterns")
    if gap_patterns_raw:
        gap_patterns = [
            (g["keywords"], g["gap"], g["suggestion"]) for g in gap_patterns_raw
        ]
    else:
        gap_patterns = _DEFAULT_GAP_PATTERNS

    fallback_strength = analysis.get("fallback_strength", _DEFAULT_FALLBACK_STRENGTH)
    no_title_hit_gap = analysis.get("no_title_hit_gap", _DEFAULT_NO_TITLE_HIT_GAP)

    return {
        "kw_insights": kw_insights,
        "company_insights": company_insights,
        "gap_patterns": gap_patterns,
        "fallback_strength": fallback_strength,
        "no_title_hit_gap": no_title_hit_gap,
    }


# ---------------------------------------------------------------------------
# Company hiring pipeline data
# ---------------------------------------------------------------------------
# process: interview flow description
# category: for grouping in report
# tips: key advice for candidates
_COMPANY_PIPELINE = {
    # ---- FAANG / 外资大厂 ----
    "Amazon": {
        "process": "HR筛选 → OA → Phone Screen → Loop面试(5-6轮+Bar Raiser) → HC审批 → Offer → 背调",
        "category": "FAANG / 外资大厂",
        "tips": "准备16条LP案例; Bar Raiser侧重文化匹配; TPM岗HC审批较严格",
    },
    "Google": {
        "process": "HR筛选 → 技术电话面(1-2轮) → Onsite(4-5轮) → Hiring Committee → Team Match → Offer → 背调",
        "category": "FAANG / 外资大厂",
        "tips": "HC审批耗时最长(2-3周); Onsite后需独立的Team Match环节",
    },
    "Meta": {
        "process": "Recruiter Call → 技术电话面 → Full Loop(4-5轮) → Hiring Committee → Team Match → Offer → 背调",
        "category": "FAANG / 外资大厂",
        "tips": "System Design和Behavioral权重各半; Team Match可能拖长周期",
    },
    "Apple": {
        "process": "HR筛选 → 技术电话面 → Onsite(3-4轮) → Hiring Manager终面 → Offer → 背调",
        "category": "FAANG / 外资大厂",
        "tips": "各团队流程独立,面试风格差异大; HW团队更看重系统集成经验",
    },
    "Microsoft": {
        "process": "HR筛选 → 技术面(3-4轮) → As Appropriate终面 → Offer审批 → 背调",
        "category": "FAANG / 外资大厂",
        "tips": "As Appropriate面由高级别经理把关; Azure相关岗位优先",
    },
    "Morgan Stanley": {
        "process": "HR筛选 → HackerRank → 技术面(2-3轮) → Director面 → Compliance审批 → Offer → 背调",
        "category": "FAANG / 外资大厂",
        "tips": "金融合规审批耗时较长; 技术深度要求高; 强调大规模系统经验",
    },
    # ---- 半导体 / 芯片 ----
    "NVIDIA": {
        "process": "HR筛选 → 技术面(2-3轮) → Hiring Manager面 → Director审批 → Offer → 背调",
        "category": "半导体 / 芯片",
        "tips": "上海偏Automotive和Networking方向; 强调HW平台+AI经验的结合",
    },
    "AMD": {
        "process": "HR筛选 → 技术面(2-3轮) → 管理层面试 → Offer → 背调",
        "category": "半导体 / 芯片",
        "tips": "上海有Xilinx(FPGA)团队; 芯片验证和平台集成经验是加分项",
    },
    "Intel": {
        "process": "HR筛选 → 技术电话面 → Panel面试(3-4轮) → 审批 → Offer → 背调",
        "category": "半导体 / 芯片",
        "tips": "上海有IDM全链团队; 审批链较长; FPGA/数据中心方向活跃",
    },
    "Qualcomm": {
        "process": "HR筛选 → 技术面(2-3轮) → 管理层面试 → Offer审批 → 背调",
        "category": "半导体 / 芯片",
        "tips": "上海以手机SoC和IoT为主; 芯片量产和项目管理经验加分",
    },
    "Broadcom": {
        "process": "HR筛选 → 技术面(2-3轮) → VP级面试 → Offer → 背调",
        "category": "半导体 / 芯片",
        "tips": "网络芯片和存储控制器方向; 流程相对精简; 技术深度要求高",
    },
    "Cadence": {
        "process": "HR筛选 → 技术面(2-3轮) → Hiring Manager面 → Offer → 背调",
        "category": "半导体 / 芯片",
        "tips": "EDA领域TPM需懂芯片设计流程; 强调HW平台系统集成的可迁移性",
    },
    "Synopsys": {
        "process": "HR筛选 → 技术面(2-3轮) → 管理层面试 → Offer → 背调",
        "category": "半导体 / 芯片",
        "tips": "EDA/IP验证方向; 理解RTL到GDSII流程是加分项",
    },
    "Marvell": {
        "process": "HR筛选 → 技术面(2-3轮) → 管理层面试 → Offer → 背调",
        "category": "半导体 / 芯片",
        "tips": "存储控制器和网络芯片方向; 上海团队规模适中; 流程较快",
    },
    "MediaTek": {
        "process": "HR筛选 → 技术面(2-3轮) → 主管面 → Offer → 背调",
        "category": "半导体 / 芯片",
        "tips": "手机SoC和AIoT方向; 台企风格,面试务实; 上海有完整研发团队",
    },
    # ---- 国产芯片 / AI芯片 ----
    "Horizon Robotics": {
        "process": "HR筛选 → 技术面(2-3轮) → VP面 → Offer → 背调",
        "category": "国产芯片 / AI芯片",
        "tips": "自动驾驶芯片方向; 流程快(3-6周); 成长期公司,职级和薪资有弹性",
    },
    "Cambricon": {
        "process": "HR筛选 → 技术面(2-3轮) → 总监面 → Offer → 背调",
        "category": "国产芯片 / AI芯片",
        "tips": "AI推理芯片; 国产替代概念; 流程快但稳定性需关注",
    },
    "Enflame": {
        "process": "HR筛选 → 技术面(2-3轮) → CTO/VP面 → Offer → 背调",
        "category": "国产芯片 / AI芯片",
        "tips": "AI训练芯片; 上海本部; 快速成长期,管理体系在建设中",
    },
    "Biren": {
        "process": "HR筛选 → 技术面(2轮) → VP面 → Offer → 背调",
        "category": "国产芯片 / AI芯片",
        "tips": "通用GPU方向; 流程最快; 关注公司资金和业务稳定性",
    },
    # ---- 中国科技 ----
    "ByteDance": {
        "process": "HR筛选 → 技术面(2-3轮) → HR面 → 交叉面(可选) → Offer审批 → 背调",
        "category": "中国科技",
        "tips": "节奏最快的大厂之一; PMO方法论通用; AI方向活跃; OKR驱动",
    },
    "Alibaba": {
        "process": "HR筛选 → 技术面(2-3轮) → 交叉面 → HR终面 → Offer审批 → 背调",
        "category": "中国科技",
        "tips": "阿里云基础设施方向; P7/P8定级面试; 交叉面环节是特色",
    },
    "Baidu": {
        "process": "HR筛选 → 技术面(2-3轮) → 总监面 → HR面 → Offer → 背调",
        "category": "中国科技",
        "tips": "AI和自动驾驶是核心方向; 上海有智能云和Apollo团队",
    },
    "Tencent": {
        "process": "HR筛选 → 技术面(2-3轮) → GM面 → HR面 → Offer → 背调",
        "category": "中国科技",
        "tips": "上海以游戏和云为主; BG间差异大; 关注具体部门方向",
    },
    # ---- 汽车 / 新能源 ----
    "Tesla": {
        "process": "HR筛选 → 技术面(2-3轮) → Hiring Manager面 → Offer → 背调",
        "category": "汽车 / 新能源",
        "tips": "上海超级工厂方向; 工程PM需HW产品化经验; 强调跨职能协调能力",
    },
    "NIO": {
        "process": "HR筛选 → 技术面(2-3轮) → 总监面 → HR面 → Offer → 背调",
        "category": "汽车 / 新能源",
        "tips": "智能驾驶和智能座舱方向; 上海总部; 科技公司文化",
    },
    "XPeng": {
        "process": "HR筛选 → 技术面(2-3轮) → VP面 → Offer → 背调",
        "category": "汽车 / 新能源",
        "tips": "智能驾驶和AI方向; 上海有研发中心; 流程较灵活",
    },
    # ---- 基础设施 / 存储 ----
    "Dell": {
        "process": "HR筛选 → 技术面(2-3轮) → Director面 → Offer审批 → 背调",
        "category": "基础设施 / 存储",
        "tips": "老牌基础设施企业; 流程成熟; 内推渠道有优势",
    },
    "HPE": {
        "process": "HR筛选 → 技术面(2-3轮) → VP级面试 → Offer审批 → 背调",
        "category": "基础设施 / 存储",
        "tips": "服务器和存储方向; 基础设施PM经验高度相关; 上海有研发团队",
    },
    "Pure Storage": {
        "process": "HR筛选 → 技术面(2-3轮) → 管理层面试 → Offer → 背调",
        "category": "基础设施 / 存储",
        "tips": "全闪存存储; 高增长公司; 上海有工程团队",
    },
    "NetApp": {
        "process": "HR筛选 → 技术面(2-3轮) → Director面 → Offer → 背调",
        "category": "基础设施 / 存储",
        "tips": "混合云存储方向; 流程规范; 存储基础设施经验互通",
    },
    "Nutanix": {
        "process": "HR筛选 → 技术面(2-3轮) → VP面 → Offer → 背调",
        "category": "基础设施 / 存储",
        "tips": "HCI超融合方向——超融合基础设施经验是强卖点",
    },
    # ---- 工业 / 其他外企 ----
    "Siemens": {
        "process": "HR筛选 → 技术面(2-3轮) → 部门负责人面 → Assessment Center(可选) → Offer → 背调",
        "category": "工业 / 其他外企",
        "tips": "流程和质量管理是核心; ISO审计经验和DQA模型是差异化",
    },
    "BioNTech": {
        "process": "HR筛选 → 技术面(2-3轮) → 科学家/总监面 → Global面 → Offer → 背调",
        "category": "工业 / 其他外企",
        "tips": "生物制药PM; Global面环节可能增加周期; 强调R&D项目管理方法论的可迁移性",
    },
    "Shell": {
        "process": "HR筛选 → 技术面(2轮) → Assessment Day → Offer → 背调",
        "category": "工业 / 其他外企",
        "tips": "能源数字化转型方向; Assessment Day含小组讨论; 强调PMP和跨职能经验",
    },
}

# Category display order
_PIPELINE_CATEGORY_ORDER = [
    "FAANG / 外资大厂",
    "半导体 / 芯片",
    "国产芯片 / AI芯片",
    "中国科技",
    "汽车 / 新能源",
    "基础设施 / 存储",
    "工业 / 其他外企",
]


def generate_match_summary(job: dict, config: dict) -> str:
    """Generate match analysis: strengths + gaps + improvement suggestions."""
    ac = _load_analysis_config(config)
    kw_insights = ac["kw_insights"]
    company_insights = ac["company_insights"]
    gap_patterns = ac["gap_patterns"]

    bd = job.get("score_breakdown", {})
    company = job["company"].lower()
    title_lower = job["title"].lower()
    kw_hits = bd.get("keyword_hits", [])
    title_hit = bd.get("title_hit", "")
    jd_lower = (job.get("jd_text") or "").lower()
    scan_text = f"{title_lower} {jd_lower}"

    # --- Strengths (pick best 2) ---
    strength_parts = []

    if title_hit:
        resume_point = kw_insights.get(title_hit, "")
        if resume_point:
            strength_parts.append(resume_point)

    for co_key, insight in company_insights.items():
        if co_key in company:
            strength_parts.append(insight)
            break

    remaining_kws = [k for k in kw_hits if k != title_hit]
    for kw in remaining_kws:
        if kw in kw_insights and len(strength_parts) < 3:
            strength_parts.append(kw_insights[kw])

    if not strength_parts:
        seniority_hit = bd.get("seniority_hit", "")
        if seniority_hit:
            strength_parts.append(f"**{seniority_hit.title()}** 级别与简历资历匹配")
        if kw_hits:
            strength_parts.append(f"关键词 {', '.join(kw_hits[:3])} 与简历核心能力对齐")
        if not strength_parts:
            strength_parts.append(ac["fallback_strength"])

    strengths = "。".join(strength_parts[:2])

    # --- Gaps & suggestions (scan JD for mismatch patterns) ---
    gap_text = ""
    for keywords, gap_desc, suggestion in gap_patterns:
        for kw in keywords:
            # Use word-boundary matching to avoid false positives like "social" → "soc"
            if re.search(r'\b' + re.escape(kw) + r'\b', scan_text):
                gap_text = f"⚠️ {gap_desc}——{suggestion}"
                break
        if gap_text:
            break

    # Additional gap checks based on score dimensions
    if not gap_text:
        if not bd.get("title_hit"):
            gap_text = ac["no_title_hit_gap"]
        elif bd.get("tfidf_raw", 0) < 0.05:
            gap_text = "⚠️ JD 内容与简历语义重合度偏低——建议仔细研读 JD 后在简历中补充对应关键词"
        elif not bd.get("company_hit"):
            gap_text = "⚠️ 非知名外企，建议先调研公司背景和团队规模再决定是否投递"

    # Compose
    result = f"**匹配：**{strengths}。"
    if gap_text:
        result += f" {gap_text}。"
    return result


def match_jobs(jobs: list[dict], resume: dict, matcher: SemanticMatcher, config: dict,
               boost_key: str = "boost_title_keywords") -> list[dict]:
    """Score all jobs and return sorted by score (desc), filtered by min_score."""
    min_score = config["matching"].get("min_score", 35)
    scored = []
    for j in jobs:
        bd = score_job(j, resume, matcher, config, boost_key=boost_key)
        j["score"] = bd["total"]
        j["score_breakdown"] = bd
        if bd["total"] >= min_score:
            scored.append(j)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def match_jobs_multi_resume(
    jobs: list[dict],
    resumes: dict[str, dict],
    matchers: dict[str, SemanticMatcher],
    config: dict,
    boost_key: str = "boost_title_keywords",
) -> list[dict]:
    """Score each job against ALL resumes and keep the best score.

    Args:
        jobs: List of job dicts.
        resumes: {resume_type: parsed_resume} mapping.
        matchers: {resume_type: SemanticMatcher} mapping.
        config: Full config dict.
        boost_key: Title boost keyword config key.

    Returns:
        Sorted list of jobs (desc by best score) above min_score.
        Each job gets 'best_resume' field indicating which resume matched best.
    """
    min_score = config["matching"].get("min_score", 35)
    scored = []
    for j in jobs:
        best_score = 0.0
        best_bd = {}
        best_type = ""
        for rtype, resume in resumes.items():
            bd = score_job(j, resume, matchers[rtype], config, boost_key=boost_key)
            if bd["total"] > best_score:
                best_score = bd["total"]
                best_bd = bd
                best_type = rtype
        j["score"] = best_score
        j["score_breakdown"] = best_bd
        j["best_resume"] = best_type
        if best_score >= min_score:
            scored.append(j)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def assign_tier(score: float) -> str:
    if score >= 75:
        return "S"
    elif score >= 60:
        return "A"
    elif score >= 45:
        return "B"
    else:
        return "C"


# ---------------------------------------------------------------------------
# Application timeline advisor
# ---------------------------------------------------------------------------
def compute_apply_window(company: str, config: dict) -> tuple[str, str]:
    """Compute the recommended application date window for a company."""
    tl = config["timeline"]
    target = datetime.strptime(tl["target_start_date"], "%Y-%m-%d")
    buffer = timedelta(weeks=tl["onboarding_buffer_weeks"])
    offer_deadline = target - buffer  # Need offer by this date

    hiring = tl["company_hiring_weeks"]
    # Find matching company
    weeks_range = hiring.get("default", [5, 9])
    for key, val in hiring.items():
        if key != "default" and key.lower() in company.lower():
            weeks_range = val
            break

    min_weeks, max_weeks = weeks_range
    apply_earliest = offer_deadline - timedelta(weeks=max_weeks)
    apply_latest = offer_deadline - timedelta(weeks=min_weeks)

    return apply_earliest.strftime("%Y-%m-%d"), apply_latest.strftime("%Y-%m-%d")


def get_apply_urgency(company: str, config: dict) -> str:
    """Return urgency label based on today's date vs apply window."""
    today = datetime.now()
    earliest_str, latest_str = compute_apply_window(company, config)
    earliest = datetime.strptime(earliest_str, "%Y-%m-%d")
    latest = datetime.strptime(latest_str, "%Y-%m-%d")

    if today > latest:
        return "⚠️ 已过最佳窗口，尽快投递"
    elif today >= earliest:
        return "🟢 当前在最佳投递窗口内"
    else:
        days_until = (earliest - today).days
        if days_until <= 14:
            return f"🟡 {days_until} 天后进入窗口"
        else:
            return f"⏳ 建议 {earliest_str} 后投递"


def _lookup_pipeline(company: str) -> dict | None:
    """Find matching pipeline data for a company (substring match)."""
    for key, val in _COMPANY_PIPELINE.items():
        if key.lower() in company.lower():
            return val
    return None


def _get_hiring_weeks(company: str, config: dict) -> tuple[int, int]:
    """Get [min, max] hiring weeks for a company from config."""
    hiring = config["timeline"]["company_hiring_weeks"]
    for key, val in hiring.items():
        if key != "default" and key.lower() in company.lower():
            return val[0], val[1]
    default = hiring.get("default", [5, 9])
    return default[0], default[1]


def generate_timeline_section(companies: list[str], config: dict, *,
                              compact: bool = False) -> list[str]:
    """Generate the interview pipeline & timeline section for a list of companies.

    Args:
        companies: List of company names to include.
        config: Full config dict.
        compact: If True, only produce the summary table (for listing report).

    Returns:
        List of markdown lines.
    """
    tl = config["timeline"]
    target_str = tl["target_start_date"]
    target = datetime.strptime(target_str, "%Y-%m-%d")
    buffer_weeks = tl["onboarding_buffer_weeks"]
    offer_deadline = target - timedelta(weeks=buffer_weeks)

    lines: list[str] = []
    lines.append(f"## 面试入职流程与投递建议")
    lines.append(f"")
    lines.append(f"> 目标入职：**{target_str}** | "
                 f"Offer Deadline：**{offer_deadline.strftime('%Y-%m-%d')}**（入职准备期 {buffer_weeks} 周）")
    lines.append(f"")

    # --- Summary table grouped by category ---
    # Build list of (company, category, weeks, window, urgency)
    rows: list[dict] = []
    for c in companies:
        pipeline = _lookup_pipeline(c)
        category = pipeline["category"] if pipeline else "其他"
        min_w, max_w = _get_hiring_weeks(c, config)
        earliest, latest = compute_apply_window(c, config)
        urgency = get_apply_urgency(c, config)

        # Milestone dates: interview window and expected offer
        apply_mid = datetime.strptime(earliest, "%Y-%m-%d") + timedelta(days=7)
        interview_start = apply_mid + timedelta(weeks=round(min_w * 0.2))
        interview_end = datetime.strptime(latest, "%Y-%m-%d") + timedelta(weeks=round(max_w * 0.55))
        offer_est = datetime.strptime(latest, "%Y-%m-%d") + timedelta(weeks=round(max_w * 0.75))
        # Cap offer estimate at offer_deadline
        if offer_est > offer_deadline:
            offer_est = offer_deadline

        rows.append({
            "company": c,
            "category": category,
            "min_w": min_w,
            "max_w": max_w,
            "apply_earliest": earliest,
            "apply_latest": latest,
            "interview_start": interview_start.strftime("%m-%d"),
            "interview_end": interview_end.strftime("%m-%d"),
            "offer_est": offer_est.strftime("%m-%d"),
            "urgency": urgency,
            "pipeline": _lookup_pipeline(c),
        })

    # Group by category (ordered)
    from collections import OrderedDict
    cat_groups: OrderedDict[str, list[dict]] = OrderedDict()
    for cat in _PIPELINE_CATEGORY_ORDER:
        cat_groups[cat] = []
    cat_groups["其他"] = []
    for r in rows:
        cat_groups.setdefault(r["category"], []).append(r)

    # Emit summary table
    lines.append("### 投递时间线总览")
    lines.append("")
    lines.append("| 公司 | 类别 | 流程周期 | 建议投递窗口 | 预计面试期 | 预计出Offer | 状态 |")
    lines.append("|------|------|---------|-------------|-----------|------------|------|")
    for cat, cat_rows in cat_groups.items():
        for r in sorted(cat_rows, key=lambda x: x["apply_earliest"]):
            lines.append(
                f"| {r['company']} | {r['category']} | {r['min_w']}-{r['max_w']}周 "
                f"| {r['apply_earliest']} ~ {r['apply_latest']} "
                f"| {r['interview_start']} ~ {r['interview_end']} "
                f"| ~{r['offer_est']} "
                f"| {r['urgency']} |"
            )
    lines.append("")

    if compact:
        return lines

    # --- Detailed per-category breakdown ---
    lines.append("### 各公司面试流程详情")
    lines.append("")

    for cat, cat_rows in cat_groups.items():
        if not cat_rows:
            continue
        lines.append(f"#### {cat}")
        lines.append("")
        for r in sorted(cat_rows, key=lambda x: x["apply_earliest"]):
            p = r["pipeline"]
            if p:
                process = p["process"]
                tips = p["tips"]
            else:
                process = "HR筛选 → 技术面(2-3轮) → 管理层面试 → Offer → 背调"
                tips = "流程参考行业通用标准"

            status_icon = r["urgency"].split(" ")[0]  # emoji only
            lines.append(
                f"**{r['company']}** `{r['min_w']}-{r['max_w']}周` {status_icon}"
            )
            lines.append(f"> 流程：{process}")
            lines.append(
                f"> 时间：投递 {r['apply_earliest']} ~ {r['apply_latest']} → "
                f"面试 ~{r['interview_start']} ~ {r['interview_end']} → "
                f"Offer ~{r['offer_est']}"
            )
            lines.append(f"> 提示：{tips}")
            lines.append("")

    return lines


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def generate_report(matched_jobs: list[dict], config: dict) -> str:
    """Generate a markdown report."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    target = config["timeline"]["target_start_date"]

    lines = [
        f"# LinkedIn 职位匹配报告",
        f"",
        f"> 生成时间：{now}",
        f"> 目标入职日期：{target}",
        f"> 搜索地点：{', '.join(config['search'].get('locations', [config['search']['location']]))}",
        f"> 关键词：{', '.join(config['search']['keywords'])}",
        f"> 匹配引擎：TF-IDF 语义匹配 + 关键词混合评分",
        f"> 匹配到 {len(matched_jobs)} 个职位（最低分 {config['matching']['min_score']}）",
        f"",
        f"---",
        f"",
    ]

    # Group by tier
    tiers = {"S": [], "A": [], "B": [], "C": []}
    for j in matched_jobs:
        t = assign_tier(j["score"])
        tiers[t].append(j)

    tier_labels = {
        "S": "S 级：完美匹配（强烈推荐）",
        "A": "A 级：强匹配（值得投递）",
        "B": "B 级：良好匹配（可考虑）",
    }

    for tier_key in ["S", "A", "B"]:
        tier_jobs = tiers[tier_key]
        if not tier_jobs:
            continue

        lines.append(f"## {tier_labels[tier_key]}")
        lines.append(f"")
        # Show "推荐简历" column only when multi-resume data is present
        has_resume_col = any(j.get("best_resume") for j in tier_jobs)
        if has_resume_col:
            lines.append(f"| # | 职位 | 公司 | 匹配分 | 推荐简历 | 匹配分析 | 投递建议 |")
            lines.append(f"|---|------|------|--------|----------|----------|----------|")
        else:
            lines.append(f"| # | 职位 | 公司 | 匹配分 | 匹配分析 | 投递建议 |")
            lines.append(f"|---|------|------|--------|----------|----------|")

        for i, j in enumerate(tier_jobs, 1):
            title = j["title"]
            if j["link"]:
                title = f"[{j['title']}]({j['link']})"
            urgency = get_apply_urgency(j["company"], config)
            status_tag = f" `{j['status']}`" if j.get("status") else ""
            analysis = generate_match_summary(j, config)
            if has_resume_col:
                best_r = j.get("best_resume", "").replace("_", " ")
                lines.append(
                    f"| {i} | {title} | {j['company']}{status_tag} | {j['score']} | {best_r} | {analysis} | {urgency} |"
                )
            else:
                lines.append(
                    f"| {i} | {title} | {j['company']}{status_tag} | {j['score']} | {analysis} | {urgency} |"
                )

        lines.append(f"")

        # For S-tier jobs, append collapsible full JD text
        if tier_key == "S":
            for i, j in enumerate(tier_jobs, 1):
                link_md = f"[{j['title']}]({j['link']})" if j["link"] else j["title"]
                lines.append(f"### {i}. {j['title']} — {j['company']}")
                lines.append(f"")
                lines.append(f"> {link_md} | 匹配分 {j['score']}")
                lines.append(f"")

                # --- Full JD ---
                jd = j.get("jd_text", "")
                if jd:
                    lines.append(f"<details><summary>完整 JD（点击展开）</summary>")
                    lines.append(f"")
                    lines.append(jd)
                    lines.append(f"")
                    lines.append(f"</details>")
                    lines.append(f"")

                lines.append(f"---")
                lines.append(f"")

    # Timeline summary — enhanced with interview pipeline analysis
    lines.append(f"---")
    lines.append(f"")

    # Collect unique companies from ALL tiers (S/A/B)
    key_companies = set()
    for tier_key in ["S", "A", "B"]:
        for j in tiers.get(tier_key, []):
            key_companies.add(j["company"])

    if key_companies:
        timeline_lines = generate_timeline_section(sorted(key_companies), config)
        lines.extend(timeline_lines)

    # Score breakdown legend
    lines.append(f"---")
    lines.append(f"")
    lines.append(f"## 评分维度说明")
    lines.append(f"")
    lines.append(f"| 维度 | 最高分 | 说明 |")
    lines.append(f"|------|--------|------|")
    lines.append(f"| 标题匹配 | 25 | 职位标题含 TPM/Program Manager 等关键词 |")
    lines.append(f"| 关键词 | 30 | 优先技能关键词命中数 ×5（上限6个） |")
    lines.append(f"| 简历短语 | 16 | 简历加粗短语在标题中出现 |")
    lines.append(f"| 外企偏好 | 12 | 知名外企 / 跨国公司加分 |")
    lines.append(f"| 主动招聘 | 5 | LinkedIn 标注 Actively Hiring |")
    lines.append(f"| 时效性 | 5 | 近期发布的职位加分 |")
    lines.append(f"| 资深度 | 8 | Senior/Principal/Lead/Director 等 |")
    tfidf_w = config['matching'].get('tfidf_weight', 30)
    lines.append(f"| TF-IDF | {tfidf_w} | 简历与职位的语义相似度 ×{tfidf_w} |")
    lines.append(f"")

    # Search links
    lines.append(f"---")
    lines.append(f"")
    lines.append(f"## 快捷搜索链接")
    lines.append(f"")
    loc_encoded = config["search"]["location"].replace(" ", "+").replace(",", "%2C")
    for kw in config["search"]["keywords"]:
        kw_encoded = kw.replace(" ", "+")
        url = f"https://www.linkedin.com/jobs/search?keywords={kw_encoded}&location={loc_encoded}"
        lines.append(f"- [{kw}]({url})")
    lines.append(f"")

    lines.append(f"---")
    lines.append(f"*由 job_matcher.py 自动生成（TF-IDF + 关键词混合引擎） | 配置文件: config.json*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Comprehensive listing report (all scraped jobs, organized by keyword)
# ---------------------------------------------------------------------------
def generate_listing_report(all_jobs: list[dict], config: dict) -> str:
    """Generate a comprehensive job listing report for ALL scraped jobs,
    organized by search keyword category — similar to the manual LinkedIn
    Shanghai PM Jobs summary.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    location = ', '.join(config["search"].get("locations", [config["search"]["location"]]))
    keywords = config["search"]["keywords"]

    # Filter out jobs posted 1+ year ago
    all_jobs = [j for j in all_jobs if "year" not in (j.get("posted") or "").lower()]

    lines = [
        "# LinkedIn PM / Program Manager / PMO 职位汇总",
        "",
        f"> 抓取时间：{now}",
        "> 数据来源：LinkedIn 公开职位搜索（job_matcher.py 自动生成）",
        f"> 筛选条件：工作地点 = {location}，排除发布超过一年的职位",
        f"> 搜索关键词：{' | '.join(keywords)}",
        f"> 抓取总量：{len(all_jobs)} 条去重后职位",
        "",
        "---",
        "",
    ]

    # Keyword display names (preserve proper casing for acronyms like PMO)
    section_names = {
        "project manager": "Project Manager",
        "program manager": "Program Manager",
        "PMO": "PMO",
        "technical program manager": "Technical Program Manager",
    }

    def _kw_display(kw: str) -> str:
        return section_names.get(kw, kw.title())

    # Quick search links
    lines.append("## 快捷搜索链接")
    lines.append("")
    lines.append("| 关键词 | 链接 |")
    lines.append("|--------|------|")
    for kw in keywords:
        encoded = kw.replace(" ", "+")
        url = f"https://www.linkedin.com/jobs/search?keywords={encoded}&location=Shanghai%2C+China"
        lines.append(f"| {_kw_display(kw)} | {url} |")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Group jobs by keyword
    kw_groups: dict[str, list[dict]] = {}
    for kw in keywords:
        kw_groups[kw] = []
    for j in all_jobs:
        kw = j.get("keyword", "")
        if kw in kw_groups:
            kw_groups[kw].append(j)
        else:
            # Job may have been deduped from a later keyword; put into first group
            kw_groups.setdefault(kw, []).append(j)

    # Preferred companies for highlighting
    preferred = {c.lower() for c in config["matching"].get("preferred_companies", [])}

    section_num = 0
    cn_nums = ["一", "二", "三", "四", "五", "六", "七", "八"]
    for kw in keywords:
        jobs_in_group = kw_groups.get(kw, [])
        if not jobs_in_group:
            continue
        label = section_names.get(kw, kw.title())
        cn = cn_nums[section_num] if section_num < len(cn_nums) else str(section_num + 1)
        section_num += 1

        # Sort: Actively Hiring first, then by recency (posted_dt desc)
        def _sort_key(j):
            is_active = 1 if "actively hiring" in (j.get("status") or "").lower() else 0
            dt = j.get("posted_dt", "") or "1970-01-01"
            return (-is_active, dt)  # desc active, desc date (later is bigger string)

        jobs_in_group.sort(key=_sort_key, reverse=True)

        lines.append(f"## {cn}、{label} 精选职位（共 {len(jobs_in_group)} 条）")
        lines.append("")
        lines.append("| # | 职位 | 公司 | 发布时间 | 状态 |")
        lines.append("|---|------|------|----------|------|")

        for i, j in enumerate(jobs_in_group, 1):
            title = j["title"]
            if j.get("link"):
                title = f"[{j['title']}]({j['link']})"
            company = j["company"]
            # Bold preferred companies
            if any(pc in company.lower() for pc in preferred):
                company = f"**{company}**"
            posted = j.get("posted", "") or "—"
            status = j.get("status", "") or "—"
            lines.append(f"| {i} | {title} | {company} | {posted} | {status} |")

        lines.append("")
        lines.append("---")
        lines.append("")

    # --- Company distribution analysis ---
    lines.append("## 重点公司分布（高频招聘方）")
    lines.append("")

    # Count jobs per company
    company_counts: dict[str, int] = {}
    company_canonical: dict[str, str] = {}  # lowercase → original casing
    for j in all_jobs:
        c = j["company"]
        cl = c.lower()
        company_counts[cl] = company_counts.get(cl, 0) + 1
        if cl not in company_canonical:
            company_canonical[cl] = c

    # Industry classifier (best-effort)
    _INDUSTRY_MAP = {
        "apple": "科技/消费电子", "amazon": "科技/电商/云计算", "aws": "科技/云计算",
        "meta": "科技/VR", "oculus": "科技/VR", "google": "科技",
        "microsoft": "科技", "bytedance": "科技/互联网",
        "amd": "半导体", "nvidia": "半导体/AI", "intel": "半导体",
        "qualcomm": "半导体", "broadcom": "半导体", "asml": "半导体设备",
        "cadence": "EDA", "synopsys": "EDA",
        "tesla": "汽车/新能源", "general motors": "汽车", "bmw": "汽车",
        "volkswagen": "汽车", "volvo": "汽车", "ford": "汽车",
        "bosch": "汽车零部件", "continental": "汽车零部件",
        "siemens": "工业/制造", "schneider": "能源/工业", "abb": "工业/自动化",
        "honeywell": "工业/航空", "emerson": "工业/自动化", "ge": "工业",
        "sanofi": "医药", "astrazeneca": "医药", "novartis": "医药",
        "roche": "医药", "pfizer": "医药", "medtronic": "医疗器械",
        "johnson": "医药/消费", "bayer": "医药/化工", "biontech": "生物科技",
        "morgan stanley": "金融", "goldman": "金融", "jpmorgan": "金融",
        "hsbc": "金融", "deutsche bank": "金融",
        "disney": "娱乐", "netflix": "娱乐",
        "shell": "能源", "bp": "能源", "total": "能源",
        "rio tinto": "矿业", "bhp": "矿业",
        "dell": "科技/服务器", "hp": "科技", "lenovo": "科技",
        "cisco": "网络/通信", "vmware": "虚拟化/云",
        "samsung": "电子", "sony": "电子", "panasonic": "电子",
        "chanel": "奢侈品", "lvmh": "奢侈品", "lululemon": "零售/运动",
        "nike": "运动品牌", "adidas": "运动品牌",
        "unilever": "快消", "p&g": "快消", "l'oreal": "美妆",
        "pepsico": "快消/食品",
        "mckinsey": "咨询", "deloitte": "咨询", "accenture": "咨询",
        "pwc": "咨询", "ey": "咨询", "kpmg": "咨询",
        "boeing": "航空", "airbus": "航空", "gkn": "航空",
    }

    def _guess_industry(company_lower: str) -> str:
        for key, ind in _INDUSTRY_MAP.items():
            if key in company_lower:
                return ind
        return "—"

    # Show companies with 2+ jobs, or preferred companies with 1+ job
    notable = []
    for cl, count in sorted(company_counts.items(), key=lambda x: -x[1]):
        is_preferred = any(pc in cl for pc in preferred)
        if count >= 2 or is_preferred:
            notable.append((company_canonical[cl], count, _guess_industry(cl)))

    if notable:
        lines.append("| 公司 | 职位数 | 行业 |")
        lines.append("|------|--------|------|")
        for name, cnt, industry in notable[:30]:
            lines.append(f"| **{name}** | {cnt}+ | {industry} |")
        lines.append("")
    else:
        lines.append("*未检测到高频招聘公司*")
        lines.append("")

    lines.append("---")
    lines.append("")

    # --- Timeline section (compact) for known companies ---
    preferred = [pc.lower() for pc in config["matching"]["preferred_companies"]]
    timeline_companies = set()
    for j in all_jobs:
        if any(pc in j["company"].lower() for pc in preferred):
            timeline_companies.add(j["company"])
    if timeline_companies:
        timeline_lines = generate_timeline_section(
            sorted(timeline_companies), config, compact=True
        )
        lines.extend(timeline_lines)
        lines.append("---")
        lines.append("")

    # --- Market insights ---
    lines.append("## 市场洞察")
    lines.append("")

    # Job count per keyword
    lines.append("### 各类别职位数量")
    lines.append("")
    lines.append("| 关键词 | 抓取数量 |")
    lines.append("|--------|----------|")
    for kw in keywords:
        cnt = len(kw_groups.get(kw, []))
        lines.append(f"| {_kw_display(kw)} | {cnt} |")
    lines.append("")

    # Status distribution
    active_count = sum(1 for j in all_jobs if "actively hiring" in (j.get("status") or "").lower())
    early_count = sum(1 for j in all_jobs if "early applicant" in (j.get("status") or "").lower())
    other_count = len(all_jobs) - active_count - early_count
    lines.append("### 职位状态分布")
    lines.append("")
    lines.append("| 状态 | 数量 | 占比 |")
    lines.append("|------|------|------|")
    total = len(all_jobs) or 1
    lines.append(f"| Actively Hiring | {active_count} | {active_count*100//total}% |")
    lines.append(f"| Early Applicant | {early_count} | {early_count*100//total}% |")
    lines.append(f"| 其他 | {other_count} | {other_count*100//total}% |")
    lines.append("")

    # Recency distribution
    recent_24h = 0
    recent_1w = 0
    recent_1m = 0
    for j in all_jobs:
        p = (j.get("posted") or "").lower()
        if "hour" in p or "minute" in p:
            recent_24h += 1
            recent_1w += 1
            recent_1m += 1
        elif "day" in p:
            recent_1w += 1
            recent_1m += 1
        elif "1 week" in p or "2 week" in p or "3 week" in p:
            recent_1m += 1
            if "1 week" in p:
                recent_1w += 1

    lines.append("### 时效性分析")
    lines.append("")
    lines.append("| 时间段 | 数量 |")
    lines.append("|--------|------|")
    lines.append(f"| 过去 24 小时 | {recent_24h} |")
    lines.append(f"| 过去一周 | {recent_1w} |")
    lines.append(f"| 过去一个月 | {recent_1m} |")
    lines.append(f"| 全部 | {len(all_jobs)} |")
    lines.append("")

    # Preferred company presence
    preferred_jobs = [j for j in all_jobs if any(pc in j["company"].lower() for pc in preferred)]
    lines.append("### 外企/知名公司覆盖")
    lines.append("")
    lines.append(f"- 知名外企/公司职位占比：**{len(preferred_jobs)}/{len(all_jobs)}** ({len(preferred_jobs)*100//total}%)")
    lines.append("")

    # Industry trend observations
    lines.append("### 行业趋势观察")
    lines.append("")

    # Count by rough industry buckets
    industry_counts: dict[str, int] = {}
    for j in all_jobs:
        ind = _guess_industry(j["company"].lower())
        if ind != "—":
            bucket = ind.split("/")[0]
            industry_counts[bucket] = industry_counts.get(bucket, 0) + 1

    for ind, cnt in sorted(industry_counts.items(), key=lambda x: -x[1])[:6]:
        lines.append(f"- **{ind}** 行业：{cnt} 条职位")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*由 job_matcher.py 自动生成 | 抓取时间：{now}*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Extended roles report (AI PM, Solutions Architect, DevOps, QA, etc.)
# ---------------------------------------------------------------------------
# Direction labels for extended keywords
_EXTENDED_DIRECTIONS = {
    "AI product manager":            "AI 产品经理",
    "product manager AI ML":         "AI/ML 产品经理",
    "solutions architect":           "解决方案架构师",
    "technical solutions manager":   "技术解决方案经理",
    "digital transformation manager":"数字化转型经理",
    "release engineering manager":   "发布工程经理",
    "DevOps manager":                "DevOps 经理",
    "quality engineering manager":   "质量工程经理",
    "QA director":                   "QA 总监",
    "technical account manager":     "技术客户经理",
}


def generate_extended_match_report(matched_jobs: list[dict], all_jobs: list[dict],
                                   config: dict) -> str:
    """Generate a combined matching + listing report for extended role directions."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    target = config["timeline"]["target_start_date"]
    location = ', '.join(config["search"].get("locations", [config["search"]["location"]]))
    ext_keywords = config["search"].get("extended_keywords", [])

    # Filter out 1+ year old jobs for listing section
    listing_jobs = [j for j in all_jobs if "year" not in (j.get("posted") or "").lower()]

    lines = [
        "# LinkedIn 拓展方向职位报告（PM 之外）",
        "",
        f"> 生成时间：{now}",
        f"> 目标入职日期：{target}",
        f"> 搜索地点：{location}",
        f"> 搜索方向：AI 产品经理 | 解决方案架构师 | 数字化转型 | 发布/DevOps 工程 | 质量工程 | 技术客户经理",
        f"> 抓取总量：{len(listing_jobs)} 条去重后职位（排除发布超过一年）",
        f"> 匹配到 {len(matched_jobs)} 个职位（最低分 {config['matching']['min_score']}）",
        "",
        "---",
        "",
    ]

    # ===== Part 1: Matched jobs by tier =====
    lines.append("# 一、匹配评分结果")
    lines.append("")

    tiers = {"S": [], "A": [], "B": [], "C": []}
    for j in matched_jobs:
        t = assign_tier(j["score"])
        tiers[t].append(j)

    tier_labels = {
        "S": "S 级：完美匹配（强烈推荐）",
        "A": "A 级：强匹配（值得投递）",
        "B": "B 级：良好匹配（可考虑）",
    }

    for tier_key in ["S", "A", "B"]:
        tier_jobs = tiers[tier_key]
        if not tier_jobs:
            continue

        lines.append(f"## {tier_labels[tier_key]}")
        lines.append("")
        has_resume_col = any(j.get("best_resume") for j in tier_jobs)
        if has_resume_col:
            lines.append("| # | 职位 | 公司 | 匹配分 | 推荐简历 | 搜索方向 | 匹配分析 | 投递建议 |")
            lines.append("|---|------|------|--------|----------|----------|----------|----------|")
        else:
            lines.append("| # | 职位 | 公司 | 匹配分 | 搜索方向 | 匹配分析 | 投递建议 |")
            lines.append("|---|------|------|--------|----------|----------|----------|")

        for i, j in enumerate(tier_jobs, 1):
            title = j["title"]
            if j["link"]:
                title = f"[{j['title']}]({j['link']})"
            urgency = get_apply_urgency(j["company"], config)
            status_tag = f" `{j['status']}`" if j.get("status") else ""
            analysis = generate_match_summary(j, config)
            direction = _EXTENDED_DIRECTIONS.get(j.get("keyword", ""), j.get("keyword", ""))
            if has_resume_col:
                best_r = j.get("best_resume", "").replace("_", " ")
                lines.append(
                    f"| {i} | {title} | {j['company']}{status_tag} | {j['score']} | {best_r} | {direction} | {analysis} | {urgency} |"
                )
            else:
                lines.append(
                    f"| {i} | {title} | {j['company']}{status_tag} | {j['score']} | {direction} | {analysis} | {urgency} |"
                )

        lines.append("")

        # S-tier: collapsible full JD
        if tier_key == "S":
            for i, j in enumerate(tier_jobs, 1):
                link_md = f"[{j['title']}]({j['link']})" if j["link"] else j["title"]
                lines.append(f"### {i}. {j['title']} — {j['company']}")
                lines.append("")
                lines.append(f"> {link_md} | 匹配分 {j['score']}")
                lines.append("")
                jd = j.get("jd_text", "")
                if jd:
                    lines.append("<details><summary>完整 JD（点击展开）</summary>")
                    lines.append("")
                    lines.append(jd)
                    lines.append("")
                    lines.append("</details>")
                    lines.append("")
                lines.append("---")
                lines.append("")

    if not any(tiers[k] for k in ["S", "A", "B"]):
        lines.append("*本次搜索未匹配到符合最低分的职位。*")
        lines.append("")

    # ===== Part 2: All jobs listing by keyword =====
    lines.append("---")
    lines.append("")
    lines.append("# 二、全量职位列表（按搜索方向分类）")
    lines.append("")

    # Group by keyword
    kw_groups: dict[str, list[dict]] = {}
    for kw in ext_keywords:
        kw_groups[kw] = []
    for j in listing_jobs:
        kw = j.get("keyword", "")
        if kw in kw_groups:
            kw_groups[kw].append(j)

    preferred = {c.lower() for c in config["matching"].get("preferred_companies", [])}

    section_num = 0
    cn_nums = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]
    for kw in ext_keywords:
        jobs_in_group = kw_groups.get(kw, [])
        if not jobs_in_group:
            continue
        label = _EXTENDED_DIRECTIONS.get(kw, kw.title())
        cn = cn_nums[section_num] if section_num < len(cn_nums) else str(section_num + 1)
        section_num += 1

        def _sort_key(j):
            is_active = 1 if "actively hiring" in (j.get("status") or "").lower() else 0
            dt = j.get("posted_dt", "") or "1970-01-01"
            return (-is_active, dt)

        jobs_in_group.sort(key=_sort_key, reverse=True)

        lines.append(f"### {cn}、{label}（共 {len(jobs_in_group)} 条，关键词：{kw}）")
        lines.append("")
        lines.append("| # | 职位 | 公司 | 发布时间 | 状态 |")
        lines.append("|---|------|------|----------|------|")

        for i, j in enumerate(jobs_in_group, 1):
            title = j["title"]
            if j.get("link"):
                title = f"[{j['title']}]({j['link']})"
            company = j["company"]
            if any(pc in company.lower() for pc in preferred):
                company = f"**{company}**"
            posted = j.get("posted", "") or "—"
            status = j.get("status", "") or "—"
            lines.append(f"| {i} | {title} | {company} | {posted} | {status} |")

        lines.append("")

    # ===== Part 3: Summary stats =====
    lines.append("---")
    lines.append("")
    lines.append("# 三、市场概览")
    lines.append("")
    lines.append("### 各方向职位数量")
    lines.append("")
    lines.append("| 搜索方向 | 关键词 | 抓取数量 |")
    lines.append("|----------|--------|----------|")
    for kw in ext_keywords:
        cnt = len(kw_groups.get(kw, []))
        label = _EXTENDED_DIRECTIONS.get(kw, kw.title())
        lines.append(f"| {label} | {kw} | {cnt} |")
    lines.append("")

    # Preferred company jobs
    preferred_jobs = [j for j in listing_jobs if any(pc in j["company"].lower() for pc in preferred)]
    total = len(listing_jobs) or 1
    lines.append(f"- 知名外企/公司职位占比：**{len(preferred_jobs)}/{len(listing_jobs)}** ({len(preferred_jobs)*100//total}%)")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*由 job_matcher.py 自动生成（拓展方向引擎） | 抓取时间：{now}*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email sender
# ---------------------------------------------------------------------------
def send_email(report: str, config: dict):
    """Send the report via SMTP (163.com)."""
    email_cfg = config["email"]
    if not email_cfg.get("enabled"):
        print("  [INFO] Email disabled in config. Skipping.")
        return

    auth_code = os.environ.get(email_cfg["password_env"], "")
    if not auth_code:
        print(f"  [WARN] Env var '{email_cfg['password_env']}' not set. Skipping email.")
        return

    now_str = datetime.now().strftime("%Y-%m-%d")
    subject = f"{email_cfg['subject_prefix']} LinkedIn 职位匹配报告 {now_str}"

    msg = MIMEMultipart("alternative")
    msg["From"] = email_cfg["sender"]
    msg["To"] = email_cfg["recipient"]
    msg["Subject"] = subject

    # Plain text version
    msg.attach(MIMEText(report, "plain", "utf-8"))

    # Simple HTML version (convert markdown tables to HTML)
    html = markdown_to_simple_html(report)
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(email_cfg["smtp_server"], email_cfg["smtp_port"], context=context) as server:
            server.login(email_cfg["sender"], auth_code)
            server.sendmail(email_cfg["sender"], email_cfg["recipient"], msg.as_string())
        print(f"  [OK] Email sent to {email_cfg['recipient']}")
    except Exception as e:
        print(f"  [ERROR] Failed to send email: {e}")


def markdown_to_simple_html(md: str) -> str:
    """Minimal markdown-to-HTML for email rendering."""
    lines = md.split("\n")
    html_lines = [
        "<html><body style='font-family: -apple-system, Arial, sans-serif; max-width: 900px; margin: auto;'>"
    ]
    in_table = False

    for line in lines:
        # Skip separator lines in tables
        if re.match(r"^\|[-\s|:]+\|$", line):
            continue

        if line.startswith("# "):
            html_lines.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("## "):
            html_lines.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("### "):
            html_lines.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("> "):
            html_lines.append(f"<blockquote style='color:#666;border-left:3px solid #ccc;padding-left:10px;'>{line[2:]}</blockquote>")
        elif line.startswith("---"):
            html_lines.append("<hr>")
        elif line.startswith("- "):
            content = line[2:]
            # Convert markdown links
            content = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', content)
            html_lines.append(f"<li>{content}</li>")
        elif line.startswith("|"):
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if not in_table:
                html_lines.append("<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;width:100%;font-size:13px;'>")
                tag = "th"
                in_table = True
            else:
                tag = "td"
            row = "".join("<{0}>{1}</{0}>".format(tag, re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', c)) for c in cells)
            html_lines.append(f"<tr>{row}</tr>")
        else:
            if in_table:
                html_lines.append("</table>")
                in_table = False
            if line.strip():
                content = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
                content = re.sub(r"`(.+?)`", r"<code>\1</code>", content)
                content = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', content)
                html_lines.append(f"<p>{content}</p>")

    if in_table:
        html_lines.append("</table>")
    html_lines.append("</body></html>")
    return "\n".join(html_lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="LinkedIn Job Matcher (TF-IDF + Keyword Hybrid)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config JSON")
    parser.add_argument("--send-email", action="store_true", help="Send report via email")
    parser.add_argument("--dry-run", action="store_true", help="Scrape only, no file/email output")
    parser.add_argument("--output", help="Override output file path")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)

    print("=" * 60)
    print("LinkedIn Job Matcher (TF-IDF + Keyword Hybrid Engine)")
    print("=" * 60)

    print(f"[1] Config loaded: {config_path}")

    # Override email setting
    if args.send_email:
        config["email"]["enabled"] = True

    # ===================================================================
    # Load all resumes
    # ===================================================================
    source_dir_cfg = config.get("source_dir", "")
    source_dir = resolve_path(source_dir_cfg, SCRIPT_DIR) if source_dir_cfg else None
    if source_dir and source_dir.is_dir():
        resume_files = sorted(source_dir.glob("*.md"))
    else:
        resume_files = [resolve_path(config["resume_path"], SCRIPT_DIR)]

    if not resume_files:
        print("[ERROR] No resume files found.")
        return

    # Build {resume_type: parsed_resume} and {resume_type: matcher} dicts
    resumes: dict[str, dict] = {}
    matchers: dict[str, SemanticMatcher] = {}
    for rf in resume_files:
        suffix_match = re.search(r"resume[_\-](.+)", rf.stem, re.IGNORECASE)
        rtype = suffix_match.group(1) if suffix_match else rf.stem
        resumes[rtype] = load_resume(rf)
        matchers[rtype] = SemanticMatcher(resumes[rtype]["clean"])

    print(f"[2] Loaded {len(resumes)} resume(s): {', '.join(resumes.keys())}")
    for rtype, r in resumes.items():
        print(f"       - {rtype}: {len(r['bold_phrases'])} key phrases")

    # Resolve locations for display
    locations = config["search"].get("locations", [config["search"].get("location", "")])
    print(f"       Search locations: {', '.join(locations)}")

    # ===================================================================
    # Scrape LinkedIn ONCE
    # ===================================================================
    print(f"[3] Fetching core PM jobs...")
    jobs = fetch_all_jobs(config, keyword_key="keywords")
    print(f"       Total unique jobs: {len(jobs)}")

    if not jobs:
        print("[WARN] No jobs found. LinkedIn may be rate-limiting. Try again later.")
        return

    ext_jobs = []
    ext_keywords = config["search"].get("extended_keywords", [])
    if ext_keywords:
        print(f"[4] Fetching extended direction jobs...")
        ext_jobs = fetch_all_jobs(config, keyword_key="extended_keywords")
        print(f"       Total unique extended jobs: {len(ext_jobs)}")

    # ===================================================================
    # Core PM: multi-resume matching
    # ===================================================================
    print(f"[5] Multi-resume matching (core PM, {len(resumes)} resumes x {len(jobs)} jobs)...")
    matched = match_jobs_multi_resume(jobs, resumes, matchers, config)
    print(f"       First-pass matched: {len(matched)} jobs")

    print(f"[6] Fetching JD details for deeper matching...")
    fetch_jd_for_qualifying_jobs(matched, config)

    # Re-score JD-enriched jobs against all resumes
    jd_enriched = [j for j in matched if j.get("jd_text")]
    if jd_enriched:
        print(f"       Re-scoring {len(jd_enriched)} jobs with JD details...")
        for j in jd_enriched:
            best_score = 0.0
            best_bd = {}
            best_type = ""
            for rtype, resume in resumes.items():
                bd = score_job(j, resume, matchers[rtype], config)
                if bd["total"] > best_score:
                    best_score = bd["total"]
                    best_bd = bd
                    best_type = rtype
            j["score"] = best_score
            j["score_breakdown"] = best_bd
            j["best_resume"] = best_type
        matched.sort(key=lambda x: x["score"], reverse=True)

    tiers = {}
    for j in matched:
        t = assign_tier(j["score"])
        tiers[t] = tiers.get(t, 0) + 1
    print(f"       Final: {len(matched)} jobs (S:{tiers.get('S',0)} A:{tiers.get('A',0)} B:{tiers.get('B',0)})")

    print(f"[7] Generating reports...")
    report = generate_report(matched, config)
    listing_report = generate_listing_report(jobs, config)

    # ===================================================================
    # Extended directions: multi-resume matching
    # ===================================================================
    ext_report = None
    if ext_keywords and ext_jobs:
        print(f"[8] Multi-resume matching (extended, {len(resumes)} resumes x {len(ext_jobs)} jobs)...")
        ext_matched = match_jobs_multi_resume(
            ext_jobs, resumes, matchers, config,
            boost_key="extended_boost_title_keywords",
        )
        print(f"       First-pass matched: {len(ext_matched)} jobs")

        fetch_jd_for_qualifying_jobs(ext_matched, config)

        jd_ext = [j for j in ext_matched if j.get("jd_text")]
        if jd_ext:
            print(f"       Re-scoring {len(jd_ext)} extended jobs with JD details...")
            for j in jd_ext:
                best_score = 0.0
                best_bd = {}
                best_type = ""
                for rtype, resume in resumes.items():
                    bd = score_job(j, resume, matchers[rtype], config,
                                   boost_key="extended_boost_title_keywords")
                    if bd["total"] > best_score:
                        best_score = bd["total"]
                        best_bd = bd
                        best_type = rtype
                j["score"] = best_score
                j["score_breakdown"] = best_bd
                j["best_resume"] = best_type
            ext_matched.sort(key=lambda x: x["score"], reverse=True)

        ext_tiers = {}
        for j in ext_matched:
            t = assign_tier(j["score"])
            ext_tiers[t] = ext_tiers.get(t, 0) + 1
        print(f"       Extended final: {len(ext_matched)} jobs "
              f"(S:{ext_tiers.get('S',0)} A:{ext_tiers.get('A',0)} B:{ext_tiers.get('B',0)})")

        print(f"[9] Generating extended direction report...")
        ext_report = generate_extended_match_report(ext_matched, ext_jobs, config)

    # ===================================================================
    # Output
    # ===================================================================
    if args.dry_run:
        print("\n" + report)
        if ext_report:
            print("\n" + "=" * 60 + "\n")
            print(ext_report)
        return

    date_str = datetime.now().strftime("%Y%m%d")
    output_dir = resolve_path(config["output_dir"], SCRIPT_DIR) / date_str
    output_dir.mkdir(parents=True, exist_ok=True)

    report_file = output_dir / f"LinkedIn_Job_Report_{date_str}.md"
    report_file.write_text(report, encoding="utf-8")
    print(f"       PM matching report saved:  {report_file}")

    listing_file = output_dir / f"LinkedIn_Job_Listing_{date_str}.md"
    listing_file.write_text(listing_report, encoding="utf-8")
    print(f"       PM listing report saved:   {listing_file}")

    if ext_report:
        ext_file = output_dir / f"LinkedIn_Extended_Roles_{date_str}.md"
        ext_file.write_text(ext_report, encoding="utf-8")
        print(f"       Extended roles report saved: {ext_file}")

    # Email
    if config["email"].get("enabled"):
        print(f"       Sending email...")
        send_email(report, config)

    print()
    print("=" * 60)
    print("Done!")
    print(f"Reports saved to: {output_dir}")


if __name__ == "__main__":
    main()
