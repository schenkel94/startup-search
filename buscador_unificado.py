from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    PLAYWRIGHT_READY = True
except Exception:
    PlaywrightTimeout = Exception
    sync_playwright = None
    PLAYWRIGHT_READY = False


APP_TITLE = "🕵️Schenkel Startup Search"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)
GUPY_MIN_DATE = pd.Timestamp("2026-01-01", tz="UTC")
GREENHOUSE_COMPANIES = sorted(
    set(
        """
        99 afya agibank aircompany arcoeducacao bancopan belvo blip braskem btgpactual c6bank
        ciandt clara clearsale cobli contaazul creditas deel dock ebanx enforce exactsales flash
        fretebras getatende gympass hashdex hotmartcareersbr ifoodcarreiras ilia inter isaac
        jusbrasil kickoff linx loft magazineluiza marketdata mercadopago meliuz movile neon
        nubank oliverbrazil pagarme picpay pipefy pismo quintoandar rdstation remote rippling
        softplan solides stake stone tivit unico voltz vtex wildlifestudios xpinc yulife
        zenvia zupinnovation
        """.split()
    )
)
INHIRE_COMPANIES = """
olist openlabs orizon paytrack premiersoft radix shareprime sylvamo sympla talentx tripla
unimar v360 v4company vitru warren zig contabilizei kiwify bancotoyota adelcoco solutis
programmers gruposabe dbservices grupojra proselect elsys frete sidia gpcorpbr talentetech
contaazul oliveiraeantunes svninvestimentos
""".split()
QUICKIN_COMPANIES = [
    "startse",
    "topmind",
    "registradores",
    "devos",
    "networksecure",
    "solupeople",
    "vagas",
    "infovagas",
    "qintess",
    "sinqia"
]
INCLUDE_DEFAULTS = [
    "analista de dados",
    "data analyst",
    "analista de bi",
    "bi analyst",
    "business intelligence",
    "analista de negocios",
    "business analyst",
    "analytics",
    "dataviz",
    "visualizacao de dados",
    "inteligencia de mercado",
]
EXCLUDE_DEFAULTS = [
    "engenharia",
    "engineer",
    "ciencia de dados",
    "data science",
    "scientist",
    "cientista",
    "estagio",
    "banco de talentos",
]
REMOTE_TERMS = ["remoto", "remota", "home office", "home-office", "homeoffice", "teletrabalho"]
JOB_LINK_PATTERN = re.compile(r"/vagas/[a-z0-9-]+", re.IGNORECASE)
TITLE_KEYS = ("title", "name", "jobTitle", "job_title", "position")
URL_KEYS = ("url", "href", "link", "jobUrl", "job_url", "absoluteUrl")
PATH_KEYS = ("path", "slug", "uri", "permalink")


@dataclass
class SearchConfig:
    sources: list[str]
    include_terms: list[str]
    exclude_terms: list[str]
    only_remote: bool
    greenhouse_companies: list[str]
    inhire_companies: list[str]
    quickin_companies: list[str]
    gupy_pages: int
    inhire_timeout_ms: int


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text).strip().lower()


def parse_terms(raw: str) -> list[str]:
    items = [norm(x) for x in re.split(r"[\n,;]+", raw or "")]
    return list(dict.fromkeys([x for x in items if x]))


def has_term(text: str, terms: list[str]) -> bool:
    normalized = norm(text)
    return any(term in normalized for term in terms)


def keep_title(title: str, include_terms: list[str], exclude_terms: list[str]) -> bool:
    normalized = norm(title)
    if any(term in normalized for term in exclude_terms):
        return False
    return any(term in normalized for term in include_terms) if include_terms else True


def parse_date(value: str | None) -> pd.Timestamp:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    return ts if not pd.isna(ts) else pd.NaT


def fmt_date(value: pd.Timestamp) -> str:
    if pd.isna(value):
        return ""
    try:
        return value.tz_convert(None).strftime("%d/%m/%Y")
    except Exception:
        return value.strftime("%d/%m/%Y")


def load_extra_greenhouse_companies() -> list[str]:
    file_path = Path(__file__).resolve().parent / "empresas.txt"
    if not file_path.exists():
        return []
    return [line.strip().lower() for line in file_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def row(source: str, company: str, title: str, link: str, location: str, modal: str, remote: str, origin: str, date: pd.Timestamp) -> dict[str, Any]:
    return {
        "Fonte": source,
        "Origem da coleta": origin,
        "Empresa": company,
        "Vaga": title,
        "Localizacao": location,
        "Modalidade": modal,
        "Remoto?": remote,
        "Data": fmt_date(date),
        "Link": link,
        "_sort_date": date,
    }


def build_results_df(rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(
        rows or [],
        columns=["Fonte", "Origem da coleta", "Empresa", "Vaga", "Localizacao", "Modalidade", "Remoto?", "Data", "Link", "_sort_date"],
    )
    if not df.empty:
        df = df.drop_duplicates(subset=["Fonte", "Link"]).sort_values(
            by=["_sort_date", "Empresa", "Vaga"],
            ascending=[False, True, True],
            na_position="last",
        ).reset_index(drop=True)
    return df


def requests_headers() -> dict[str, str]:
    return {"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/html,application/json"}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_greenhouse(company: str) -> list[dict[str, Any]]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs"
    response = requests.get(url, timeout=25)
    if response.status_code == 404:
        return []
    response.raise_for_status()
    return response.json().get("jobs", [])


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_quickin_board(slug: str) -> tuple[str, str]:
    url = f"https://jobs.quickin.io/{slug}/jobs"
    response = requests.get(url, headers=requests_headers(), timeout=25)
    response.raise_for_status()
    return url, response.text


def quickin_modality_and_remote(text: str) -> tuple[str, str]:
    normalized = norm(text)
    if "remote" in normalized or "remoto" in normalized or "remota" in normalized:
        return "Remoto", "Sim"
    if "hybrid" in normalized or "hibrido" in normalized or "hibrida" in normalized:
        return "Hibrido", "Nao"
    if "on-site" in normalized or "onsite" in normalized or "presencial" in normalized:
        return "Presencial", "Nao"
    return "Nao informado", "Nao informado"


def parse_quickin_job_card(card_text: str, title: str) -> tuple[str, str, str]:
    cleaned = re.sub(r"\s+", " ", card_text or "").strip()
    title = re.sub(r"\s+", " ", title or "").strip()
    remainder = cleaned.replace(title, "", 1).strip(" |")
    modality, remote = quickin_modality_and_remote(remainder)

    location = "Nao informado"
    if "|" in remainder:
        parts = [part.strip() for part in remainder.split("|") if part.strip()]
        if parts:
            location = parts[-1]
            if any(token.lower() in {"remote", "hybrid", "on-site"} for token in location.lower().split()):
                location = parts[0] if len(parts) > 1 else "Nao informado"
    else:
        match = re.search(r"(.+?)\s+(Remote|Hybrid|On-site)$", remainder, flags=re.IGNORECASE)
        if match:
            location = match.group(1).strip() or "Nao informado"
        elif remainder:
            location = remainder

    return location, modality, remote


def quickin_pagination_urls(base_url: str, soup: BeautifulSoup) -> list[str]:
    urls = [base_url]
    seen = {base_url}
    for anchor in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(base_url, anchor.get("href") or "")
        if "/jobs" not in href:
            continue
        if href == base_url:
            continue
        if "/jobs/" in href:
            continue
        if href not in seen:
            urls.append(href)
            seen.add(href)
    return urls


def extract_quickin_jobs_from_html(board_name: str, board_url: str, html: str, include_terms: list[str], exclude_terms: list[str], only_remote: bool) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []
    seen_links: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(board_url, anchor.get("href") or "")
        if "/jobs/" not in href:
            continue

        title = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
        if not title or not keep_title(title, include_terms, exclude_terms) or href in seen_links:
            continue

        container = anchor.find_parent(["li", "article", "div", "section"]) or anchor.parent
        card_text = re.sub(r"\s+", " ", container.get_text(" ", strip=True) if container else title).strip()
        location, modality, remote = parse_quickin_job_card(card_text, title)
        if only_remote and remote != "Sim":
            continue

        rows.append(
            row(
                "Quickin",
                board_name.upper(),
                title,
                href,
                location,
                modality,
                remote,
                "HTML Quickin",
                pd.NaT,
            )
        )
        seen_links.add(href)

    return rows


def search_quickin(config: SearchConfig, tick, on_partial=None) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    for company in config.quickin_companies:
        tick(f"Quickin: {company}")
        try:
            board_url, html = fetch_quickin_board(company)
            soup = BeautifulSoup(html, "html.parser")
            page_urls = quickin_pagination_urls(board_url, soup)
            company_rows = extract_quickin_jobs_from_html(
                board_name=company,
                board_url=board_url,
                html=html,
                include_terms=config.include_terms,
                exclude_terms=config.exclude_terms,
                only_remote=config.only_remote,
            )

            for page_url in page_urls[1:]:
                try:
                    response = requests.get(page_url, headers=requests_headers(), timeout=25)
                    response.raise_for_status()
                    company_rows.extend(
                        extract_quickin_jobs_from_html(
                            board_name=company,
                            board_url=page_url,
                            html=response.text,
                            include_terms=config.include_terms,
                            exclude_terms=config.exclude_terms,
                            only_remote=config.only_remote,
                        )
                    )
                except Exception:
                    continue

            deduped = build_results_df(company_rows).to_dict("records")
            if deduped:
                rows.extend(deduped)
                if on_partial:
                    on_partial(rows, company.upper())
        except Exception as exc:
            warnings.append(f"Quickin falhou para {company}: {exc}")

    return build_results_df(rows).to_dict("records"), warnings


def search_greenhouse(config: SearchConfig, tick) -> tuple[list[dict[str, Any]], list[str]]:
    out, warnings = [], []
    remote_terms = [norm(x) for x in REMOTE_TERMS]
    for company in config.greenhouse_companies:
        tick(f"Greenhouse: {company}")
        try:
            jobs = fetch_greenhouse(company)
        except Exception as exc:
            warnings.append(f"Greenhouse falhou para {company}: {exc}")
            continue
        for job in jobs:
            title = (job.get("title") or "").strip()
            if not keep_title(title, config.include_terms, config.exclude_terms):
                continue
            location = ((job.get("location") or {}).get("name") or "Nao informado").strip()
            is_remote = has_term(f"{title} {location}", remote_terms)
            if config.only_remote and not is_remote:
                continue
            out.append(
                row(
                    "Greenhouse",
                    company.upper(),
                    title,
                    (job.get("absolute_url") or "").strip(),
                    location,
                    "Remoto" if is_remote else "Nao identificado",
                    "Sim" if is_remote else "Nao",
                    "API Greenhouse",
                    parse_date(job.get("updated_at")),
                )
            )
    return out, warnings


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_gupy(term: str, pages: int) -> list[dict[str, Any]]:
    url = "https://employability-portal.gupy.io/api/v1/jobs"
    jobs = []
    for page in range(1, pages + 1):
        response = requests.get(
            url,
            params={"jobName": term, "offset": (page - 1) * 50, "limit": 50, "sortBy": "publishedDate", "sortOrder": "desc"},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=25,
        )
        if response.status_code != 200:
            continue
        chunk = response.json().get("data", [])
        if not chunk:
            break
        jobs.extend(chunk)
    return jobs


def gupy_modal(job: dict[str, Any]) -> tuple[str, str]:
    workplace = str(job.get("workplaceType") or "").upper()
    if workplace == "REMOTE" or job.get("isRemoteWork"):
        return "Remoto", "Sim"
    if workplace == "HYBRID":
        return "Hibrido", "Nao"
    if workplace in {"ONSITE", "ON-SITE"}:
        return "Presencial", "Nao"
    return "Indefinido", "Nao informado"


def search_gupy(config: SearchConfig, tick) -> tuple[list[dict[str, Any]], list[str]]:
    out, warnings, seen = [], [], set()
    for term in config.include_terms or ["analista de dados"]:
        tick(f"Gupy: {term}")
        try:
            jobs = fetch_gupy(term, config.gupy_pages)
        except Exception as exc:
            warnings.append(f"Gupy falhou para '{term}': {exc}")
            continue
        for job in jobs:
            title = (job.get("name") or "").strip()
            link = (job.get("jobUrl") or f"https://portal.gupy.io/jobs/{job.get('id')}").strip()
            if not title or not link or link in seen or not keep_title(title, config.include_terms, config.exclude_terms):
                continue
            published_date = parse_date(job.get("publishedDate"))
            if pd.isna(published_date) or published_date < GUPY_MIN_DATE:
                continue
            modal, remote = gupy_modal(job)
            if config.only_remote and remote != "Sim":
                continue
            seen.add(link)
            location = ", ".join([part.strip() for part in [job.get("city"), job.get("state")] if isinstance(part, str) and part.strip()]) or "Nao informado"
            out.append(row("Gupy", str(job.get("careerPageName") or "Gupy").upper(), title, link, location, modal, remote, "API Gupy", published_date))
    return out, warnings


def first_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def build_url(raw_url: str | None, raw_path: str | None, listing_url: str) -> str | None:
    if raw_url:
        candidate = urllib.parse.urljoin(listing_url, raw_url)
        if "/vagas/" in candidate:
            return candidate
    if raw_path and raw_path.strip():
        raw_path = raw_path.strip()
        if "/vagas/" in raw_path:
            return urllib.parse.urljoin(listing_url, raw_path)
        if re.fullmatch(r"[a-z0-9-]{8,}", raw_path, flags=re.IGNORECASE):
            return urllib.parse.urljoin(listing_url, f"/vagas/{raw_path}")
    return None


def payload_links(payload: Any, listing_url: str, include_terms: list[str]) -> list[dict[str, str]]:
    found = []
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            title = first_str(node, TITLE_KEYS)
            link = build_url(first_str(node, URL_KEYS), first_str(node, PATH_KEYS), listing_url)
            if title and link and has_term(title, include_terms):
                found.append({"title": re.sub(r"\s+", " ", title).strip(), "link": link, "origin": "json"})
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(payload)
    return found


def inhire_candidates(page, html: str, listing_url: str, include_terms: list[str], payloads: list[Any]) -> list[dict[str, str]]:
    found = []
    for payload in payloads:
        found.extend(payload_links(payload, listing_url, include_terms))
    try:
        dom_items = page.locator("a[href*='/vagas/']").evaluate_all(
            "els => els.map(el => ({href: el.href || el.getAttribute('href') || '', text: (el.innerText || el.textContent || '').trim()}))"
        )
    except Exception:
        dom_items = []
    for item in dom_items:
        title = re.sub(r"\s+", " ", item.get("text") or "").strip()
        link = urllib.parse.urljoin(listing_url, (item.get("href") or "").strip())
        if title and "/vagas/" in link and has_term(title, include_terms):
            found.append({"title": title, "link": link, "origin": "dom"})
    soup = BeautifulSoup(html, "html.parser")
    for link_tag in soup.find_all("a", href=JOB_LINK_PATTERN):
        title = re.sub(r"\s+", " ", link_tag.get_text(" ", strip=True)).strip()
        link = urllib.parse.urljoin(listing_url, (link_tag.get("href") or "").strip())
        if title and has_term(title, include_terms):
            found.append({"title": title, "link": link, "origin": "html"})
    next_data = soup.find("script", id="__NEXT_DATA__")
    json_chunks = [next_data.get_text(strip=True)] if next_data else []
    json_chunks += [script.get_text(strip=True) for script in soup.find_all("script", attrs={"type": "application/ld+json"})]
    for chunk in json_chunks:
        try:
            found.extend(payload_links(json.loads(chunk), listing_url, include_terms))
        except Exception:
            pass
    deduped, seen = [], set()
    for item in found:
        if item["link"] not in seen:
            deduped.append(item)
            seen.add(item["link"])
    return deduped


def search_inhire(config: SearchConfig, tick, on_partial=None) -> tuple[list[dict[str, Any]], list[str]]:
    if not config.inhire_companies:
        return [], []
    if not PLAYWRIGHT_READY or sync_playwright is None:
        return [], ["InHire indisponivel: Playwright nao esta instalado neste ambiente."]

    collected_rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-infobars",
                ],
            )
            context = browser.new_context(
                locale="pt-BR",
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1440, "height": 900},
            )
            context.route(
                "**/*",
                lambda route, request: route.abort()
                if request.resource_type in {"image", "font", "media"}
                else route.continue_(),
            )
            context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = window.chrome || { runtime: {} };
                Object.defineProperty(navigator, 'languages', { get: () => ['pt-BR', 'pt', 'en-US'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
                """
            )

            try:
                for company in config.inhire_companies:
                    tick(f"InHire: {company}")
                    page = context.new_page()
                    payloads: list[Any] = []

                    def capture(response) -> None:
                        try:
                            if response.request.resource_type not in {"xhr", "fetch"}:
                                return
                            if "json" not in response.headers.get("content-type", "").lower():
                                return
                            payloads.append(response.json())
                        except Exception:
                            return

                    page.on("response", capture)

                    try:
                        listing_url = f"https://{company}.inhire.app/vagas"
                        page.goto(listing_url, timeout=60000, wait_until="domcontentloaded")
                        try:
                            page.wait_for_load_state("networkidle", timeout=5000)
                        except PlaywrightTimeout:
                            pass

                        page.wait_for_timeout(500)
                        for _ in range(2):
                            page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.9))")
                            page.wait_for_timeout(300)
                        page.evaluate("window.scrollTo(0, 0)")
                        page.wait_for_timeout(250)

                        selectors = ["a[href*='/vagas/']", "[class*='job']", "[class*='vaga']", "[class*='card']", "main"]
                        for selector in selectors:
                            try:
                                page.wait_for_selector(selector, timeout=config.inhire_timeout_ms)
                                break
                            except PlaywrightTimeout:
                                continue

                        html = page.content()
                        company_rows: list[dict[str, Any]] = []
                        for item in inhire_candidates(page, html, listing_url, config.include_terms, payloads):
                            if has_term(item["title"], config.exclude_terms):
                                continue
                            company_rows.append(
                                row(
                                    "InHire",
                                    company.upper(),
                                    item["title"],
                                    item["link"],
                                    "Nao informado",
                                    "N/A",
                                    "N/A",
                                    f"InHire {item['origin']}",
                                    pd.NaT,
                                )
                            )
                        if company_rows:
                            collected_rows.extend(company_rows)
                            if on_partial:
                                on_partial(collected_rows, company.upper())
                    except Exception as exc:
                        warnings.append(f"InHire falhou para {company}: {exc}")
                    finally:
                        page.close()
            finally:
                browser.close()
    except Exception as exc:
        message = str(exc)
        if "Executable doesn't exist" in message or "browserType.launch" in message:
            warnings.append("InHire indisponivel: navegador do Playwright nao encontrado. Rode 'python -m playwright install chromium'.")
        else:
            warnings.append(f"InHire falhou ao iniciar: {exc}")

    return build_results_df(collected_rows).to_dict("records"), warnings


def render_progress_results(df: pd.DataFrame, stage_label: str, final: bool = False) -> None:
    if df.empty:
        if final:
            st.info("Nenhuma vaga encontrada com os filtros atuais.")
        return

    label = "Resultados finais" if final else f"Fluxo ao vivo apos {stage_label}"
    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        stat("Vagas", str(len(df)), "Carregadas ate agora")
    with col2:
        stat("Empresas", str(df["Empresa"].nunique()), "Com resultado parcial")
    with col3:
        st.markdown(f"### {label}")
        st.caption("A lista vai sendo enriquecida conforme cada fonte termina ou o InHire fecha uma empresa.")
    st.dataframe(
        df[["Fonte", "Empresa", "Vaga", "Data", "Remoto?", "Origem da coleta", "Link"]],
        hide_index=True,
        use_container_width=True,
        column_config={"Link": st.column_config.LinkColumn("Link", display_text="Abrir vaga")},
    )


def run_search(config: SearchConfig, live_render=None) -> tuple[pd.DataFrame, list[str]]:
    steps = max(
        (len(config.greenhouse_companies) if "Greenhouse" in config.sources else 0)
        + (max(1, len(config.include_terms)) if "Gupy" in config.sources else 0)
        + (len(config.quickin_companies) if "Quickin" in config.sources else 0)
        + (len(config.inhire_companies) if "InHire" in config.sources else 0),
        1,
    )
    progress, label, count = st.progress(0.0), st.empty(), 0
    def tick(message: str) -> None:
        nonlocal count
        count += 1
        label.info(f"Buscando em {message}...")
        progress.progress(min(count / steps, 1.0))
    rows, warnings = [], []
    if "Greenhouse" in config.sources:
        r, w = search_greenhouse(config, tick); rows += r; warnings += w
        if live_render and rows:
            live_render(build_results_df(rows), "Greenhouse")
    if "Gupy" in config.sources:
        r, w = search_gupy(config, tick); rows += r; warnings += w
        if live_render and rows:
            live_render(build_results_df(rows), "Gupy")
    if "Quickin" in config.sources:
        def render_quickin_partial(quickin_partial_rows: list[dict[str, Any]], stage_label: str) -> None:
            if live_render:
                live_render(build_results_df(rows + quickin_partial_rows), f"Quickin {stage_label}")
        r, w = search_quickin(config, tick, on_partial=render_quickin_partial); rows += r; warnings += w
        if live_render and rows:
            live_render(build_results_df(rows), "Quickin")
    if "InHire" in config.sources:
        def render_partial(inhire_partial_rows: list[dict[str, Any]], stage_label: str) -> None:
            if live_render:
                live_render(build_results_df(rows + inhire_partial_rows), f"InHire {stage_label}")
        r, w = search_inhire(config, tick, on_partial=render_partial); rows += r; warnings += w
        if live_render and rows:
            live_render(build_results_df(rows), "InHire")
    label.empty(); progress.empty()
    df = build_results_df(rows)
    return df, warnings


def apply_theme() -> None:
    st.markdown(
        """
        <style>
        .stApp { background: linear-gradient(180deg, #f3eee3 0%, #f7f6f1 45%, #f3efe7 100%); color: #173047; }
        .block-container { max-width: 1220px; padding-top: 1.25rem; padding-bottom: 2rem; }
        header[data-testid="stHeader"] { background: transparent; }
        #MainMenu { visibility: hidden; }
        h1, h2, h3 { font-family: "Palatino Linotype", Georgia, serif; letter-spacing: -.02em; }
        .hero, .card, .job, .control-shell { background: rgba(255,255,255,.82); border: 1px solid rgba(24,50,75,.08); box-shadow: 0 18px 44px rgba(36,55,76,.08); border-radius: 28px; }
        .hero { padding: 2rem 2.1rem; margin-bottom: 1rem; background: linear-gradient(135deg, rgba(255,255,255,.92), rgba(247,240,229,.92)); }
        .eyebrow { color: #8f532d; text-transform: uppercase; letter-spacing: .16em; font-size: .76rem; font-weight: 700; }
        .hero h1 { color: #18324b; margin: .35rem 0 .55rem; font-size: 2.45rem; }
        .hero p { color: #42566c; max-width: 760px; line-height: 1.65; font-size: 1rem; }
        .control-shell { padding: 1.25rem 1.35rem; margin-bottom: 1rem; }
        .card { padding: 1rem 1.1rem; min-height: 110px; }
        .label { color: #6c7b89; text-transform: uppercase; letter-spacing: .12em; font-size: .78rem; }
        .value { color: #18324b; font-size: 1.7rem; font-weight: 700; }
        .note { color: #6d7e8d; font-size: .92rem; margin-top: .35rem; }
        .job { padding: 1rem 1.1rem; margin-bottom: .9rem; }
        .job .title { color: #14293d; font-size: 1.05rem; font-weight: 700; margin: .45rem 0 .15rem; }
        .job .company { color: #516375; margin-bottom: .65rem; }
        .pill { display:inline-block; border-radius:999px; padding:.2rem .65rem; font-size:.76rem; font-weight:700; margin:0 .35rem .35rem 0; }
        .source { background:#e6eff8; color:#244968; } .yes { background:#e7f7ee; color:#1d6b3e; } .no { background:#f6eadf; color:#925227; } .na { background:#eef1f4; color:#556574; }
        .meta { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:.75rem; margin-top:.65rem; }
        .meta strong { display:block; color:#7a8794; text-transform:uppercase; font-size:.72rem; letter-spacing:.08em; margin-bottom:.1rem; }
        div[data-testid="stTextArea"], div[data-testid="stMultiSelect"], div[data-testid="stNumberInput"], div[data-testid="stSelectbox"], div[data-testid="stSlider"] { background: rgba(255,255,255,.55); border-radius: 18px; padding: .25rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def hero() -> None:
    st.markdown(
        """
        <section class="hero">
            <div class="eyebrow">Busca avancada para area de dados</div>
            <h1>🕵️Schenkel Startup Search</h1>
            <p>Um radar unico para encontrar vagas em startups com menos atrito, mais contexto e uma fila viva de oportunidades enquanto a busca ainda esta rodando.</p>
        </section>
        """,
        unsafe_allow_html=True,
    )


def stat(label: str, value: str, note: str) -> None:
    st.markdown(f'<div class="card"><div class="label">{label}</div><div class="value">{value}</div><div class="note">{note}</div></div>', unsafe_allow_html=True)


def show_cards(df: pd.DataFrame) -> None:
    for item in df.to_dict("records"):
        badge = "yes" if item["Remoto?"] == "Sim" else "no" if item["Remoto?"] == "Nao" else "na"
        st.markdown(
            f"""
            <section class="job">
                <span class="pill source">{item['Fonte']}</span>
                <span class="pill {badge}">Remoto? {item['Remoto?']}</span>
                <span class="pill na">{item['Origem da coleta']}</span>
                <div class="title">{item['Vaga']}</div>
                <div class="company">{item['Empresa']}</div>
                <div class="meta">
                    <div><strong>Localizacao</strong>{item['Localizacao']}</div>
                    <div><strong>Modalidade</strong>{item['Modalidade']}</div>
                    <div><strong>Data</strong>{item['Data'] or 'Nao informada'}</div>
                </div>
            </section>
            """,
            unsafe_allow_html=True,
        )
        st.link_button("Abrir vaga", item["Link"], use_container_width=True)


def app() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    apply_theme()
    hero()

    greenhouse_options = sorted(set(GREENHOUSE_COMPANIES + load_extra_greenhouse_companies()))
    st.markdown('<section class="control-shell">', unsafe_allow_html=True)
    with st.form("search_form"):
        top_left, top_right = st.columns([1.1, 1.35])
        with top_left:
            sources = st.multiselect("Fontes", ["Greenhouse", "Gupy", "Quickin", "InHire"], default=["Greenhouse", "Gupy", "Quickin", "InHire"])
            only_remote = st.toggle("Apenas vagas remotas", value=False)
            gupy_pages = st.slider("Paginas por termo na Gupy", 1, 8, 4)
            inhire_timeout_ms = st.slider("Timeout por empresa no InHire (ms)", 5000, 30000, 12000, step=1000)
            st.caption("A Gupy agora so considera vagas publicadas em 2026 ou depois.")
            st.caption("No InHire, vagas sem info de remoto continuam aparecendo com modalidade N/A.")
        with top_right:
            include_raw = st.text_area("Termos de inclusao", value=", ".join(INCLUDE_DEFAULTS), height=110)
            exclude_raw = st.text_area("Termos de exclusao", value=", ".join(EXCLUDE_DEFAULTS), height=110)

        boards_tab, quickin_tab, inhire_tab = st.tabs(["Boards Greenhouse", "Empresas Quickin", "Empresas InHire"])
        with boards_tab:
            greenhouse_companies = st.multiselect("Selecione os boards", greenhouse_options, default=greenhouse_options)
        with quickin_tab:
            quickin_companies = st.multiselect("Selecione as empresas", QUICKIN_COMPANIES, default=QUICKIN_COMPANIES)
        with inhire_tab:
            inhire_companies = st.multiselect("Selecione as empresas", INHIRE_COMPANIES, default=INHIRE_COMPANIES)

        left, right = st.columns([1, 2])
        with left:
            clicked = st.form_submit_button("Buscar vagas agora", type="primary", use_container_width=True)
        with right:
            st.caption("Os resultados entram em tela conforme cada fonte termina. No InHire, a alimentacao acontece empresa por empresa.")
    st.markdown("</section>", unsafe_allow_html=True)

    config = SearchConfig(
        sources,
        parse_terms(include_raw),
        parse_terms(exclude_raw),
        only_remote,
        greenhouse_companies,
        inhire_companies,
        quickin_companies,
        gupy_pages,
        inhire_timeout_ms,
    )
    problems = []
    if not config.sources:
        problems.append("Selecione pelo menos uma fonte.")
    if not config.include_terms:
        problems.append("Informe pelo menos um termo de inclusao.")
    if "Greenhouse" in config.sources and not config.greenhouse_companies:
        problems.append("Selecione ao menos um board do Greenhouse.")
    if "Quickin" in config.sources and not config.quickin_companies:
        problems.append("Selecione ao menos uma empresa do Quickin.")
    if "InHire" in config.sources and not config.inhire_companies:
        problems.append("Selecione ao menos uma empresa do InHire.")

    if problems:
        for problem in problems:
            st.error(problem)
        return
    if not clicked:
        st.info("Escolha as fontes, refine os termos e rode a busca. A lista vai sendo atualizada na tela sem precisar esperar tudo terminar.")
        return

    results_placeholder = st.empty()

    def live_render(df_partial: pd.DataFrame, stage_label: str) -> None:
        with results_placeholder.container():
            render_progress_results(df_partial, stage_label, final=False)

    with st.spinner("Escaneando fontes de vagas..."):
        df, warnings = run_search(config, live_render=live_render)
    for warning in warnings:
        st.warning(warning)
    with results_placeholder.container():
        if df.empty:
            st.info("Nenhuma vaga encontrada com os filtros atuais.")
            return

        flat_df = df[["Fonte", "Origem da coleta", "Empresa", "Vaga", "Localizacao", "Modalidade", "Remoto?", "Data", "Link"]].copy()
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            stat("Vagas", str(len(flat_df)), "Resultados consolidados")
        with c2:
            stat("Empresas", str(flat_df["Empresa"].nunique()), "Com pelo menos uma vaga")
        with c3:
            stat("Fontes", str(flat_df["Fonte"].nunique()), "Plataformas ativas na busca")
        with c4:
            stat("Remotas", str(int((flat_df["Remoto?"] == "Sim").sum())), "Somente vagas marcadas como remotas")

        csv_bytes = flat_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        excel_buffer = BytesIO()
        with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
            flat_df.to_excel(writer, index=False)
        d1, d2 = st.columns(2)
        with d1:
            st.download_button(
                "Baixar CSV",
                data=csv_bytes,
                file_name="vagas_dados_unificadas.csv",
                mime="text/csv",
                use_container_width=True,
            )
        with d2:
            st.download_button(
                "Baixar Excel",
                data=excel_buffer.getvalue(),
                file_name="vagas_dados_unificadas.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

        feed_tab, table_tab = st.tabs(["Feed", "Tabela completa"])
        with feed_tab:
            show_cards(flat_df)
        with table_tab:
            st.dataframe(flat_df, hide_index=True, use_container_width=True, column_config={"Link": st.column_config.LinkColumn("Link", display_text="Abrir vaga")})


if __name__ == "__main__":
    app()
