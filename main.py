from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import aiohttp
import re
import json
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from typing import List
from urllib.parse import urlparse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Config ────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'

EMAIL_BLACKLIST = [
    'sentry.io', 'example.com', 'wixpress.com', 'domain.com',
    '.js', '.css', '.png', '.jpg', 'min.js', 'sentry.okg'
]

SOCIAL_PATTERNS = {
    'facebook':  r'(?:https?://)?(?:www\.)?facebook\.com/[\w.%-]+',
    'instagram': r'(?:https?://)?(?:www\.)?instagram\.com/[\w.%-]+',
    'twitter':   r'(?:https?://)?(?:www\.)?twitter\.com/[\w.%-]+',
    'linkedin':  r'(?:https?://)?(?:www\.)?linkedin\.com/(?:company|in)/[\w.%-]+',
    'tiktok':    r'(?:https?://)?(?:www\.)?tiktok\.com/@[\w.%-]+',
    'youtube':   r'(?:https?://)?(?:www\.)?youtube\.com/(?:c/|channel/|@)[\w.-]+',
}

PAGES = ['/contact', '/contact-us', '/about', '/about-us', '/',
         '/a-propos', '/privacy-policy', '/terms-of-use', '/legal']

OBFUSCATION_PATTERNS = [
    (r'([a-zA-Z0-9._%+-]+)\s*\[at\]\s*([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', r'\1@\2'),
    (r'([a-zA-Z0-9._%+-]+)\s*\(at\)\s*([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', r'\1@\2'),
    (r'([a-zA-Z0-9._%+-]+)\s*\(arobase\)\s*([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', r'\1@\2'),
    (r'([a-zA-Z0-9._%+-]+)\s+AT\s+([a-zA-Z0-9.-]+)\s+DOT\s+([a-zA-Z]{2,})', r'\1@\2.\3'),
    (r'([a-zA-Z0-9._%+-]+)\s*@\s*([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', r'\1@\2'),
]

# ── Helpers ───────────────────────────────────────────────────────
def extract_domain_and_path(url):
    """Retourne (domain, custom_path) si l'URL contient un chemin spécifique"""
    url = str(url).strip()
    if not url.startswith('http'):
        url = 'https://' + url
    parsed = urlparse(url)
    domain = re.sub(r'^www\.', '', parsed.netloc)
    path = parsed.path.rstrip('/')
    if path and path != '/':
        return domain, path
    return domain, None

def deobfuscate(text):
    for pattern, replacement in OBFUSCATION_PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text

def clean_emails(raw_emails):
    cleaned = set()
    for email in raw_emails:
        email = email.strip().lower().split('?')[0]
        if re.search(r'u003e|u003c|\\', email):
            continue
        if any(bl in email for bl in EMAIL_BLACKLIST):
            continue
        cleaned.add(email)
    return cleaned

def extract_from_html(html, soup):
    emails = set()
    socials = {}
    title = None

    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        if any(x in title for x in ['Just a moment', 'Access Denied', 'Attention Required']):
            title = None

    html_deob = deobfuscate(html)
    emails.update(clean_emails(re.findall(EMAIL_REGEX, html_deob)))

    for a in soup.find_all("a", href=True):
        if "mailto:" in a["href"]:
            email = a["href"].replace("mailto:", "").strip().lower().split('?')[0]
            emails.update(clean_emails([email]))

    footer = soup.find('footer')
    if footer:
        emails.update(clean_emails(re.findall(EMAIL_REGEX, deobfuscate(footer.get_text()))))

    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string)
            text = json.dumps(data)
            emails.update(clean_emails(re.findall(EMAIL_REGEX, text)))
        except Exception:
            pass

    for name, pattern in SOCIAL_PATTERNS.items():
        match = re.search(pattern, html)
        if match:
            link = match.group()
            if not link.startswith('http'):
                link = 'https://' + link
            socials[name] = link

    return title, emails, socials

# ── Pass 1 : Fast mode (aiohttp) ──────────────────────────────────
async def scrape_fast(session, domain, extra_pages=None):
    emails = set()
    socials = {}
    title = None

    pages_to_visit = (extra_pages or []) + PAGES

    for base in [f"https://{domain}", f"https://www.{domain}"]:
        for page in pages_to_visit:
            url = f"{base}{page}"
            try:
                async with session.get(url, headers=HEADERS,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        continue
                    html = await resp.text(errors='ignore')
                    soup = BeautifulSoup(html, 'lxml')
                    t, e, s = extract_from_html(html, soup)
                    if t and not title:
                        title = t
                    emails.update(e)
                    for k, v in s.items():
                        if k not in socials:
                            socials[k] = v
            except Exception:
                continue
        if emails or socials:
            break

    return title, emails, socials

# ── Pass 2 : Deep mode (Playwright) ──────────────────────────────
async def scrape_playwright(domain, extra_pages=None):
    emails = set()
    socials = {}
    title = None

    pages_to_visit = (extra_pages or []) + PAGES

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1920, "height": 1080}
            )
            page = await context.new_page()

            for base in [f"https://{domain}", f"https://www.{domain}"]:
                for pg in pages_to_visit:
                    url = f"{base}{pg}"
                    try:
                        await page.goto(url, timeout=20000, wait_until="networkidle")
                        html = await page.content()
                        soup = BeautifulSoup(html, 'lxml')
                        t, e, s = extract_from_html(html, soup)
                        if t and not title:
                            title = t
                        emails.update(e)
                        for k, v in s.items():
                            if k not in socials:
                                socials[k] = v
                    except Exception:
                        continue
                if emails or socials:
                    break

            await browser.close()
    except Exception:
        pass

    return title, emails, socials

# ── Scraper principal ─────────────────────────────────────────────
async def scrape_site(session, domain_input):
    domain, custom_path = extract_domain_and_path(domain_input)
    extra_pages = [custom_path] if custom_path else None

    title, emails, socials = await scrape_fast(session, domain, extra_pages)

    scrape_method = "fast"
    if not emails and not socials:
        title, emails, socials = await scrape_playwright(domain, extra_pages)
        scrape_method = "playwright"

    return {
        "domain": domain_input,
        "title": title,
        "emails": ", ".join(emails) if emails else None,
        "scrape_method": scrape_method,
        **socials
    }

# ── Endpoint API ──────────────────────────────────────────────────
class ScrapeRequest(BaseModel):
    domains: List[str]

@app.post("/api/scrape")
async def scrape(request: ScrapeRequest):
    batch_size = 50
    all_results = []

    connector = aiohttp.TCPConnector(limit=20, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        for i in range(0, len(request.domains), batch_size):
            batch = request.domains[i:i + batch_size]
            tasks = [scrape_site(session, domain) for domain in batch]
            results = await asyncio.gather(*tasks)
            all_results.extend(results)

    return {"results": all_results}

@app.get("/")
def health():
    return {"status": "FoxtScraper API running"}