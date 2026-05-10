import openpyxl
import streamlit as st
import asyncio
import aiohttp
import re
import pandas as pd
from bs4 import BeautifulSoup
import io
import json
from urllib.parse import urlparse

# ── Config ──────────────────────────────────────────────
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

OBFUSCATION_PATTERNS = [
    (r'([a-zA-Z0-9._%+-]+)\s*\[at\]\s*([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', r'\1@\2'),
    (r'([a-zA-Z0-9._%+-]+)\s*\(at\)\s*([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', r'\1@\2'),
    (r'([a-zA-Z0-9._%+-]+)\s*\(arobase\)\s*([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', r'\1@\2'),
    (r'([a-zA-Z0-9._%+-]+)\s+AT\s+([a-zA-Z0-9.-]+)\s+DOT\s+([a-zA-Z]{2,})', r'\1@\2.\3'),
    (r'([a-zA-Z0-9._%+-]+)\s*@\s*([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', r'\1@\2'),
]

PAGES = ['/contact', '/contact-us', '/about', '/about-us', '/', '/a-propos',
         '/privacy-policy', '/terms-of-use', '/legal', '/mentions-legales',
         '/politique-de-confidentialite', '/mentions-lgales',
         '/mentions-legales-et-rgpd', '/mentions-legales-rgpd',
         '/conditions-generales-dutilisation', '/cgu', '/rgpd']

# ── Helpers ──────────────────────────────────────────────
def extract_domain_and_path(url):
    url = str(url).strip()
    if not url.startswith('http'):
        url = 'https://' + url
    parsed = urlparse(url)
    domain = re.sub(r'^www\.', '', parsed.netloc)
    path = parsed.path.rstrip('/')
    if path and path != '/':
        return domain, path
    return domain, None

def clean_domain(url):
    domain, _ = extract_domain_and_path(url)
    return domain

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

# ── Scraping ──────────────────────────────────────────────
async def scrape_site(session, domain_input):
    domain, custom_path = extract_domain_and_path(domain_input)
    pages_to_visit = ([custom_path] if custom_path else []) + PAGES

    emails = set()
    socials = {}
    title = None

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

    return {
        'domain': domain,
        'title': title,
        'emails': ', '.join(emails) if emails else None,
        **socials
    }

async def run_all(domains, max_concurrent, progress_callback):
    results = []
    connector = aiohttp.TCPConnector(limit=max_concurrent, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [scrape_site(session, domain) for domain in domains]
        for i, coro in enumerate(asyncio.as_completed(tasks)):
            try:
                result = await asyncio.wait_for(coro, timeout=60)
            except Exception:
                result = {'domain': domains[i], 'title': None, 'emails': None}
            results.append(result)
            progress_callback(i + 1, len(domains))
    return results

# ── Interface Streamlit ──────────────────────────────────
st.set_page_config(page_title="Web Scraper", page_icon="🔍", layout="wide")
st.title("🔍 Scraper d'emails et réseaux sociaux")
st.markdown("Importe une liste de sites, lance le scraping, télécharge les résultats.")

st.markdown("### 📥 Comment veux-tu entrer les sites ?")
mode = st.radio("", ["✏️ Saisie manuelle (1 à 10 sites)", "📂 Importer un fichier (CSV, Excel, TXT)"], horizontal=True)

domains_input = []

if mode == "✏️ Saisie manuelle (1 à 10 sites)":
    single = st.text_input("Un seul site", placeholder="ex: google.com")
    multi = st.text_area("Ou jusqu'à 10 sites (un par ligne)", placeholder="google.com\nfacebook.com\ntwitter.com", height=150)
    if single.strip():
        domains_input = [single.strip()]
    elif multi.strip():
        lines = [l.strip() for l in multi.strip().split('\n') if l.strip()]
        if len(lines) > 10:
            st.warning("⚠️ Maximum 10 sites en saisie manuelle. Seuls les 10 premiers seront traités.")
            lines = lines[:10]
        domains_input = lines
else:
    uploaded_file = st.file_uploader("📂 Importe ton fichier", type=["csv", "xlsx", "xls", "txt"])
    if uploaded_file:
        if uploaded_file.name.endswith('.txt'):
            content = uploaded_file.read().decode('utf-8')
            domains_input = [l.strip() for l in content.split('\n') if l.strip()]
        elif uploaded_file.name.endswith('.csv'):
            df_input = pd.read_csv(uploaded_file)
            col_name = st.selectbox("Quelle colonne contient les URLs ?", df_input.columns.tolist())
            domains_input = df_input[col_name].dropna().tolist()
        else:
            df_input = pd.read_excel(uploaded_file)
            col_name = st.selectbox("Quelle colonne contient les URLs ?", df_input.columns.tolist())
            domains_input = df_input[col_name].dropna().tolist()
        st.success(f"✅ {len(domains_input)} sites chargés")

if domains_input:
    max_concurrent = 20

    if st.button("🚀 Lancer le scraping"):
        domains = pd.Series(domains_input).apply(clean_domain).drop_duplicates().tolist()
        st.info(f"🔄 {len(domains)} sites uniques à scraper...")

        progress_bar = st.progress(0)
        status_text = st.empty()

        def progress_callback(current, total):
            progress_bar.progress(current / total)
            status_text.text(f"⏳ {current} / {total} sites traités")

        results = asyncio.run(run_all(domains, max_concurrent, progress_callback))
        df_results = pd.DataFrame(results)

        social_cols = [c for c in ['facebook','instagram','linkedin','youtube','twitter','tiktok'] if c in df_results.columns]
        has_contact = df_results['emails'].notna()
        if social_cols:
            has_contact = has_contact | df_results[social_cols].notna().any(axis=1)
        df_results = df_results[has_contact].reset_index(drop=True)

        st.session_state['df_results'] = df_results

if 'df_results' in st.session_state:
    df_results = st.session_state['df_results']

    st.success(f"✅ {len(df_results)} sites avec contacts trouvés !")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("📧 Emails", df_results['emails'].notna().sum())
    col2.metric("💼 LinkedIn", df_results['linkedin'].notna().sum() if 'linkedin' in df_results.columns else 0)
    col3.metric("▶️ YouTube", df_results['youtube'].notna().sum() if 'youtube' in df_results.columns else 0)
    col4.metric("🐦 Twitter", df_results['twitter'].notna().sum() if 'twitter' in df_results.columns else 0)

    # ── Tableau des résultats ────────────────────────────
    st.markdown("### 📊 Résultats")
    st.dataframe(df_results, use_container_width=True)

    # ── Filtrage ─────────────────────────────────────────
    st.markdown("---")
    st.markdown("### 🎯 Filtrer les résultats")

    filter_choice = st.radio(
        "Que veux-tu télécharger ?",
        ["📥 Tout télécharger sans filtre", "🔍 Filtrer par thématique"],
        horizontal=True
    )

    if filter_choice == "🔍 Filtrer par thématique":
        from collections import Counter
        import string

        STOP_WORDS = {
            'the','and','for','with','your','our','all','from','this','that',
            'are','was','not','but','have','has','more','than','its','their',
            'can','will','you','we','de','la','le','les','des','du','en','un',
            'une','et','est','par','sur','dans','qui','que','pour','plus','avec',
            'au','aux','se','son','sa','ils','elle','il','on','si','ne','pas',
            'how','what','why','when','where','get','top','best','free','new',
            'about','news','latest','online','home','page','site','web','www'
        }

        all_titles = df_results['title'].dropna().tolist()
        words = []
        for title in all_titles:
            for word in title.lower().split():
                word = word.strip(string.punctuation)
                if len(word) > 3 and word not in STOP_WORDS:
                    words.append(word)

        top_words = [word for word, count in Counter(words).most_common(30)]

        st.markdown("**💡 Mots-clés suggérés** (extraits automatiquement des titres) :")
        selected_tags = st.multiselect(
            "Sélectionne un ou plusieurs mots-clés :",
            options=top_words,
            default=[]
        )

        manual_keywords = st.text_input(
            "➕ Ajoute tes propres mots-clés (séparés par des virgules)",
            placeholder="ex: igaming, casino, cbd, crypto"
        )

        all_keywords = list(selected_tags)
        if manual_keywords.strip():
            all_keywords += [k.strip().lower() for k in manual_keywords.split(',') if k.strip()]

        if all_keywords:
            def is_relevant(title):
                if not title or str(title) == 'nan':
                    return False
                return any(kw in title.lower() for kw in all_keywords)

            df_filtered = df_results[df_results['title'].apply(is_relevant)].reset_index(drop=True)
            st.info(f"🔍 {len(df_filtered)} sites correspondent à tes filtres")
            st.dataframe(df_filtered, use_container_width=True)
            df_to_export = df_filtered
        else:
            st.warning("Sélectionne au moins un mot-clé pour filtrer.")
            df_to_export = df_results
    else:
        df_to_export = df_results

    # ── Export avec nom personnalisé ─────────────────────
    st.markdown("---")
    file_name = st.text_input("📝 Nom du fichier à télécharger", value="resultats_scraping")
    file_name = file_name.strip().replace(" ", "_") or "resultats_scraping"

    csv_buffer = io.StringIO()
    df_to_export.to_csv(csv_buffer, index=False)
    st.download_button(
        label="⬇️ Télécharger les résultats CSV",
        data=csv_buffer.getvalue(),
        file_name=f"{file_name}.csv",
        mime="text/csv"
    )