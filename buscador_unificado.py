from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from bs4 import BeautifulSoup

try:
    from streamlit_local_storage import LocalStorage

    LOCAL_STORAGE_READY = True
except Exception:
    LocalStorage = None
    LOCAL_STORAGE_READY = False

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    PLAYWRIGHT_READY = True
except Exception:
    PlaywrightTimeout = Exception
    sync_playwright = None
    PLAYWRIGHT_READY = False


APP_TITLE = "🕵️ Schenkel Startup Search"
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
solfacil idwall unico cobli piposaude reclameaqui agenciacriativa agrosearch alice amcom ceisc cielo cora crown deloitte flutterbrazil fretadao infotecbrasil magalu milvus nomadglobal olist openlabs orizon paytrack premiersoft radix shareprime sylvamo sympla talentx tripla unimar v360 v4company vitru warren zig contabilizei kiwify bancotoyota adelcoco solutis programmers gruposabe dbservices grupojra proselect elsys frete sidia gpcorpbr talentetech contaazul oliveiraeantunes svninvestimentos
""".split()

QUICKIN_COMPANIES = """
assefaz ats base2 beltis biomedspharma cadmus coders creditsbrasil devos dibconsultoria dmpessoas dommainc evtit gamestation gbase globalconsultoria greentalents groove gruponunchi hardware henriquebaiao idgengenharia infovagas integraltrust jetbov kalendae leansales m2consult opencircle pessoalizerh peoplemeet prestorh proesc quilleconsultoria rbrasset refrisat registradores reply reviewall rhshopping rmaish sapiens seekerh sinqia sklep smarthospital solupeople startse tagna talentorh tecnocomp texian topmind umanerhecarreira uniao unimedinconfidentes vagas vagasconsultoria weemais workestagios workingcenter zbrasolutions startse topmind registradores devos networksecure solupeople vagas infovagas qintess sinqia
""".split()

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
    location_terms: list[str]
    include_unknown_locations: bool
    only_remote: bool
    greenhouse_companies: list[str]
    inhire_companies: list[str]
    quickin_companies: list[str]
    gupy_pages: int
    inhire_timeout_ms: int


@dataclass
class SearchRuntime:
    search_id: str
    total_steps: int
    running: bool = True
    finished: bool = False
    stopped: bool = False
    completed_steps: int = 0
    status: str = "Preparando busca..."
    rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    thread: Thread | None = None
    stop_event: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text).strip().lower()


def parse_terms(raw: str) -> list[str]:
    items = [norm(x) for x in re.split(r"[\n,;]+", raw or "")]
    return list(dict.fromkeys([x for x in items if x]))


def merge_company_selection(selected: list[str], additions_raw: str) -> list[str]:
    additions = parse_terms(additions_raw)
    merged = list(dict.fromkeys([*selected, *additions]))
    return [item for item in merged if item]


def cleaned_company_options(items: list[str]) -> list[str]:
    return sorted(set(item.strip().lower() for item in items if item and item.strip()))


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
    normalized_location = "N/A" if not location or norm(location) in {"nao informado", "n/a"} else location
    normalized_modal = "N/A" if not modal or norm(modal) in {"nao informado", "n/a"} else modal
    return {
        "Fonte": source,
        "Origem da coleta": origin,
        "Empresa": company,
        "Vaga": title,
        "Localizacao": normalized_location,
        "Modalidade": normalized_modal,
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


def apply_display_filters(df: pd.DataFrame, location_terms: list[str], include_unknown_locations: bool) -> pd.DataFrame:
    if df.empty:
        return df

    filtered = df.copy()
    hidden_links = st.session_state.get("hidden_links", set())
    saved_links = st.session_state.get("saved_links", set())
    show_only_saved = bool(st.session_state.get("show_only_saved", False))
    show_hidden = bool(st.session_state.get("show_hidden_jobs", False))

    if not show_hidden and hidden_links:
        filtered = filtered[~filtered["Link"].isin(hidden_links)]
    if show_only_saved:
        filtered = filtered[filtered["Link"].isin(saved_links)]

    if location_terms:
        normalized_locations = filtered["Localizacao"].fillna("N/A").map(norm)
        mask = normalized_locations.apply(lambda value: any(term in value for term in location_terms))
        if include_unknown_locations:
            mask = mask | filtered["Localizacao"].fillna("N/A").isin(["N/A", "Nao informado"])
        filtered = filtered[mask]

    return filtered.reset_index(drop=True)


def share_urls(item: dict[str, Any]) -> dict[str, str]:
    share_text = f"{item['Vaga']} | {item['Empresa']} | {item['Fonte']} | {item['Link']}"
    encoded_text = urllib.parse.quote(share_text)
    encoded_link = urllib.parse.quote(item["Link"])
    return {
        "WhatsApp": f"https://wa.me/?text={encoded_text}",
        "Telegram": f"https://t.me/share/url?url={encoded_link}&text={urllib.parse.quote(item['Vaga'])}",
        "LinkedIn": f"https://www.linkedin.com/sharing/share-offsite/?url={encoded_link}",
    }


def ensure_session_state() -> None:
    st.session_state.setdefault("saved_links", set())
    st.session_state.setdefault("hidden_links", set())
    st.session_state.setdefault("show_only_saved", False)
    st.session_state.setdefault("show_hidden_jobs", False)
    st.session_state.setdefault("active_runtime", None)
    st.session_state.setdefault("saved_searches", {})
    st.session_state.setdefault("saved_search_choice", "")
    st.session_state.setdefault("saved_search_name", "")
    st.session_state.setdefault("form_state_initialized", False)
    st.session_state.setdefault("search_history", [])
    st.session_state.setdefault("browser_storage_loaded", False)
    st.session_state.setdefault("cookie_notice_accepted", False)
    st.session_state.setdefault("storage_sync_counter", 0)
    st.session_state.setdefault("storage_last_synced", -1)


def form_state_defaults() -> dict[str, Any]:
    greenhouse_defaults = cleaned_company_options(GREENHOUSE_COMPANIES + load_extra_greenhouse_companies())
    quickin_defaults = cleaned_company_options(QUICKIN_COMPANIES)
    inhire_defaults = cleaned_company_options(INHIRE_COMPANIES)
    return {
        "sources_widget": ["Greenhouse", "Gupy", "Quickin", "InHire"],
        "only_remote_widget": False,
        "gupy_pages_widget": 4,
        "inhire_timeout_widget": 12000,
        "include_unknown_locations_widget": False,
        "include_raw_widget": ", ".join(INCLUDE_DEFAULTS),
        "exclude_raw_widget": ", ".join(EXCLUDE_DEFAULTS),
        "location_raw_widget": "",
        "greenhouse_selected_widget": greenhouse_defaults,
        "greenhouse_add_raw_widget": "",
        "quickin_selected_widget": quickin_defaults,
        "quickin_add_raw_widget": "",
        "inhire_selected_widget": inhire_defaults,
        "inhire_add_raw_widget": "",
    }


def parse_query_csv(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        value = ",".join(value)
    return parse_terms(value)


def load_form_state_from_query_params() -> dict[str, Any]:
    defaults = form_state_defaults()
    params = st.query_params

    def get_bool(key: str, default: bool) -> bool:
        value = params.get(key)
        if value is None:
            return default
        return str(value).lower() in {"1", "true", "yes", "on"}

    def get_int(key: str, default: int) -> int:
        value = params.get(key)
        try:
            return int(str(value)) if value is not None else default
        except Exception:
            return default

    state = defaults.copy()
    if params.get("sources"):
        state["sources_widget"] = [item for item in str(params.get("sources")).split(",") if item]
    if params.get("include"):
        state["include_raw_widget"] = str(params.get("include")).replace(",", ", ")
    if params.get("exclude"):
        state["exclude_raw_widget"] = str(params.get("exclude")).replace(",", ", ")
    if params.get("location"):
        state["location_raw_widget"] = str(params.get("location")).replace(",", ", ")

    state["only_remote_widget"] = get_bool("only_remote", defaults["only_remote_widget"])
    state["include_unknown_locations_widget"] = get_bool("include_unknown_locations", defaults["include_unknown_locations_widget"])
    state["gupy_pages_widget"] = max(1, min(8, get_int("gupy_pages", defaults["gupy_pages_widget"])))
    state["inhire_timeout_widget"] = max(5000, min(30000, get_int("inhire_timeout", defaults["inhire_timeout_widget"])))

    for key, param_name in [
        ("greenhouse_selected_widget", "gh"),
        ("quickin_selected_widget", "quickin"),
        ("inhire_selected_widget", "inhire"),
    ]:
        parsed = parse_query_csv(params.get(param_name))
        if parsed:
            state[key] = parsed

    return state


def hydrate_form_state_from_query() -> None:
    if st.session_state.get("form_state_initialized"):
        return
    state = load_form_state_from_query_params()
    for key, value in state.items():
        st.session_state[key] = value
    st.session_state.form_state_initialized = True


def current_form_state() -> dict[str, Any]:
    return {
        "sources_widget": st.session_state.get("sources_widget", []),
        "only_remote_widget": st.session_state.get("only_remote_widget", False),
        "gupy_pages_widget": st.session_state.get("gupy_pages_widget", 4),
        "inhire_timeout_widget": st.session_state.get("inhire_timeout_widget", 12000),
        "include_unknown_locations_widget": st.session_state.get("include_unknown_locations_widget", False),
        "include_raw_widget": st.session_state.get("include_raw_widget", ""),
        "exclude_raw_widget": st.session_state.get("exclude_raw_widget", ""),
        "location_raw_widget": st.session_state.get("location_raw_widget", ""),
        "greenhouse_selected_widget": st.session_state.get("greenhouse_selected_widget", []),
        "greenhouse_add_raw_widget": st.session_state.get("greenhouse_add_raw_widget", ""),
        "quickin_selected_widget": st.session_state.get("quickin_selected_widget", []),
        "quickin_add_raw_widget": st.session_state.get("quickin_add_raw_widget", ""),
        "inhire_selected_widget": st.session_state.get("inhire_selected_widget", []),
        "inhire_add_raw_widget": st.session_state.get("inhire_add_raw_widget", ""),
    }


def apply_form_state(state: dict[str, Any]) -> None:
    for key, value in state.items():
        st.session_state[key] = value


def sync_query_params_from_form_state(state: dict[str, Any]) -> None:
    st.query_params.clear()
    st.query_params.update(
        {
            "sources": ",".join(state["sources_widget"]),
            "include": ",".join(parse_terms(state["include_raw_widget"])),
            "exclude": ",".join(parse_terms(state["exclude_raw_widget"])),
            "location": ",".join(parse_terms(state["location_raw_widget"])),
            "gh": ",".join(state["greenhouse_selected_widget"]),
            "quickin": ",".join(state["quickin_selected_widget"]),
            "inhire": ",".join(state["inhire_selected_widget"]),
            "only_remote": "1" if state["only_remote_widget"] else "0",
            "include_unknown_locations": "1" if state["include_unknown_locations_widget"] else "0",
            "gupy_pages": str(state["gupy_pages_widget"]),
            "inhire_timeout": str(state["inhire_timeout_widget"]),
        }
    )


def save_current_search(name: str, state: dict[str, Any]) -> None:
    cleaned_name = name.strip()
    if not cleaned_name:
        return
    saved_searches = dict(st.session_state.get("saved_searches", {}))
    saved_searches[cleaned_name] = state
    st.session_state.saved_searches = saved_searches
    st.session_state.saved_search_choice = cleaned_name
    mark_storage_dirty()


def register_search_history(name: str, config: SearchConfig) -> None:
    history = list(st.session_state.get("search_history", []))
    history.insert(
        0,
        {
            "nome": name or "Busca sem nome",
            "quando": pd.Timestamp.now().strftime("%d/%m %H:%M"),
            "fontes": ", ".join(config.sources),
            "termos": ", ".join(config.include_terms[:4]),
            "localidade": ", ".join(config.location_terms) if config.location_terms else "Sem filtro",
        },
    )
    st.session_state.search_history = history[:8]
    mark_storage_dirty()


def local_storage_manager() -> LocalStorage | None:
    if not LOCAL_STORAGE_READY or LocalStorage is None:
        return None
    return LocalStorage()


def mark_storage_dirty() -> None:
    st.session_state.storage_sync_counter = int(st.session_state.get("storage_sync_counter", 0)) + 1


def serializable_storage_payload() -> dict[str, Any]:
    return {
        "saved_links": sorted(st.session_state.get("saved_links", set())),
        "hidden_links": sorted(st.session_state.get("hidden_links", set())),
        "saved_searches": st.session_state.get("saved_searches", {}),
        "search_history": st.session_state.get("search_history", []),
        "cookie_notice_accepted": bool(st.session_state.get("cookie_notice_accepted", False)),
    }


def hydrate_storage_payload(payload: dict[str, Any]) -> None:
    st.session_state.saved_links = set(payload.get("saved_links", []))
    st.session_state.hidden_links = set(payload.get("hidden_links", []))
    st.session_state.saved_searches = payload.get("saved_searches", {})
    st.session_state.search_history = payload.get("search_history", [])
    st.session_state.cookie_notice_accepted = bool(payload.get("cookie_notice_accepted", False))


def sync_browser_storage() -> None:
    local_storage = local_storage_manager()
    if local_storage is None:
        return

    storage_key = "schenkel_startup_search_app_state"
    cookie_key = "schenkel_cookie_notice_accepted"
    sync_counter = int(st.session_state.get("storage_sync_counter", 0))

    cookie_value = local_storage.getItem(cookie_key, key=f"load_cookie_notice_{sync_counter}")
    if str(cookie_value).lower() in {"1", "true", "yes"}:
        st.session_state.cookie_notice_accepted = True

    if st.session_state.get("cookie_notice_accepted", False):
        stored_value = local_storage.getItem(storage_key, key=f"load_browser_storage_{sync_counter}")
        if stored_value and not st.session_state.get("browser_storage_loaded", False):
            try:
                payload = json.loads(stored_value)
                if isinstance(payload, dict):
                    hydrate_storage_payload(payload)
                    st.session_state.browser_storage_loaded = True
            except Exception:
                pass
    if not st.session_state.get("cookie_notice_accepted", False):
        return

    if int(st.session_state.get("storage_last_synced", -1)) == sync_counter:
        return

    local_storage.setItem(
        storage_key,
        json.dumps(serializable_storage_payload(), ensure_ascii=True),
        key=f"save_browser_storage_{sync_counter}",
    )
    local_storage.setItem(
        cookie_key,
        "1",
        key=f"save_cookie_notice_{sync_counter}",
    )
    st.session_state.storage_last_synced = sync_counter


def render_cookie_notice() -> None:
    if st.session_state.get("cookie_notice_accepted", False):
        return

    components.html(
        """
        <div id="schenkel-cookie-banner" style="
            position: fixed;
            left: 20px;
            right: 20px;
            bottom: 20px;
            z-index: 9999;
            background: rgba(24, 50, 75, 0.96);
            color: #f7f6f1;
            border-radius: 18px;
            padding: 14px 18px;
            box-shadow: 0 18px 40px rgba(0,0,0,0.22);
            font-family: sans-serif;
        ">
            <div style="display:flex; gap:16px; align-items:center; justify-content:space-between; flex-wrap:wrap;">
                <div style="line-height:1.5; font-size:14px;">
                    Este app usa cookies/local storage do navegador para salvar buscas, favoritos, vagas ocultas e preferencias no seu proprio dispositivo.
                </div>
                <button onclick="
                    try {
                        window.parent.localStorage.setItem('schenkel_cookie_notice_accepted', '1');
                        window.parent.location.reload();
                    } catch (error) {
                        window.localStorage.setItem('schenkel_cookie_notice_accepted', '1');
                        window.location.reload();
                    }
                " style="
                    background:#f0d9b7;
                    color:#173047;
                    border:none;
                    border-radius:999px;
                    padding:10px 16px;
                    font-weight:700;
                    cursor:pointer;
                ">Entendi</button>
            </div>
        </div>
        """,
        height=0,
    )


def total_steps(config: SearchConfig) -> int:
    return max(
        (len(config.greenhouse_companies) if "Greenhouse" in config.sources else 0)
        + (max(1, len(config.include_terms)) if "Gupy" in config.sources else 0)
        + (len(config.quickin_companies) if "Quickin" in config.sources else 0)
        + (len(config.inhire_companies) if "InHire" in config.sources else 0),
        1,
    )


def runtime_snapshot(runtime: SearchRuntime | None) -> dict[str, Any] | None:
    if runtime is None:
        return None
    with runtime.lock:
        return {
            "search_id": runtime.search_id,
            "running": runtime.running,
            "finished": runtime.finished,
            "stopped": runtime.stopped,
            "completed_steps": runtime.completed_steps,
            "total_steps": runtime.total_steps,
            "status": runtime.status,
            "rows": list(runtime.rows),
            "warnings": list(runtime.warnings),
            "error": runtime.error,
        }


def set_runtime_status(runtime: SearchRuntime, message: str, tick: bool = False) -> None:
    with runtime.lock:
        runtime.status = message
        if tick:
            runtime.completed_steps += 1


def extend_runtime_results(runtime: SearchRuntime, rows: list[dict[str, Any]]) -> None:
    with runtime.lock:
        runtime.rows = build_results_df(rows).to_dict("records")


def extend_runtime_warnings(runtime: SearchRuntime, warnings: list[str]) -> None:
    if not warnings:
        return
    with runtime.lock:
        runtime.warnings.extend(warnings)


def mark_runtime_finished(runtime: SearchRuntime, rows: list[dict[str, Any]], stopped: bool = False, error: str = "") -> None:
    with runtime.lock:
        runtime.rows = build_results_df(rows).to_dict("records")
        runtime.running = False
        runtime.finished = True
        runtime.stopped = stopped
        runtime.error = error
        runtime.status = "Busca interrompida" if stopped else "Busca concluida"


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


def search_quickin(config: SearchConfig, tick, on_partial=None, should_stop=None) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    for company in config.quickin_companies:
        if should_stop and should_stop():
            break
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
                if should_stop and should_stop():
                    break
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


def search_greenhouse(config: SearchConfig, tick, should_stop=None) -> tuple[list[dict[str, Any]], list[str]]:
    out, warnings = [], []
    remote_terms = [norm(x) for x in REMOTE_TERMS]
    for company in config.greenhouse_companies:
        if should_stop and should_stop():
            break
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


def search_gupy(config: SearchConfig, tick, should_stop=None) -> tuple[list[dict[str, Any]], list[str]]:
    out, warnings, seen = [], [], set()
    for term in config.include_terms or ["analista de dados"]:
        if should_stop and should_stop():
            break
        tick(f"Gupy: {term}")
        try:
            jobs = fetch_gupy(term, config.gupy_pages)
        except Exception as exc:
            warnings.append(f"Gupy falhou para '{term}': {exc}")
            continue
        for job in jobs:
            if should_stop and should_stop():
                break
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
            location = ", ".join([part.strip() for part in [job.get("city"), job.get("state")] if isinstance(part, str) and part.strip()]) or "N/A"
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


def search_inhire(config: SearchConfig, tick, on_partial=None, should_stop=None) -> tuple[list[dict[str, Any]], list[str]]:
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
                    if should_stop and should_stop():
                        break
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
                            if should_stop and should_stop():
                                break
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


def start_background_search(config: SearchConfig) -> None:
    runtime = SearchRuntime(
        search_id=str(pd.Timestamp.utcnow().value),
        total_steps=total_steps(config),
    )

    def worker() -> None:
        rows: list[dict[str, Any]] = []

        def should_stop() -> bool:
            return runtime.stop_event.is_set()

        def tick(message: str) -> None:
            set_runtime_status(runtime, f"Buscando em {message}...", tick=True)

        try:
            if "Greenhouse" in config.sources and not should_stop():
                result_rows, result_warnings = search_greenhouse(config, tick, should_stop=should_stop)
                rows += result_rows
                extend_runtime_results(runtime, rows)
                extend_runtime_warnings(runtime, result_warnings)

            if "Gupy" in config.sources and not should_stop():
                result_rows, result_warnings = search_gupy(config, tick, should_stop=should_stop)
                rows += result_rows
                extend_runtime_results(runtime, rows)
                extend_runtime_warnings(runtime, result_warnings)

            if "Quickin" in config.sources and not should_stop():
                def quickin_partial(partial_rows: list[dict[str, Any]], stage_label: str) -> None:
                    set_runtime_status(runtime, f"Quickin atualizou {stage_label}.")
                    extend_runtime_results(runtime, rows + partial_rows)

                result_rows, result_warnings = search_quickin(
                    config,
                    tick,
                    on_partial=quickin_partial,
                    should_stop=should_stop,
                )
                rows += result_rows
                extend_runtime_results(runtime, rows)
                extend_runtime_warnings(runtime, result_warnings)

            if "InHire" in config.sources and not should_stop():
                def inhire_partial(partial_rows: list[dict[str, Any]], stage_label: str) -> None:
                    set_runtime_status(runtime, f"InHire atualizou {stage_label}.")
                    extend_runtime_results(runtime, rows + partial_rows)

                result_rows, result_warnings = search_inhire(
                    config,
                    tick,
                    on_partial=inhire_partial,
                    should_stop=should_stop,
                )
                rows += result_rows
                extend_runtime_results(runtime, rows)
                extend_runtime_warnings(runtime, result_warnings)

            if should_stop():
                extend_runtime_warnings(runtime, ["Busca interrompida pelo usuario."])
                mark_runtime_finished(runtime, rows, stopped=True)
            else:
                mark_runtime_finished(runtime, rows)
        except Exception as exc:
            extend_runtime_warnings(runtime, [f"Erro inesperado na busca: {exc}"])
            mark_runtime_finished(runtime, rows, error=str(exc))

    runtime.thread = Thread(target=worker, daemon=True)
    st.session_state.active_runtime = runtime
    runtime.thread.start()


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


def toggle_saved(link: str) -> None:
    saved_links = set(st.session_state.get("saved_links", set()))
    if link in saved_links:
        saved_links.remove(link)
    else:
        saved_links.add(link)
    st.session_state.saved_links = saved_links
    mark_storage_dirty()


def toggle_hidden(link: str) -> None:
    hidden_links = set(st.session_state.get("hidden_links", set()))
    if link in hidden_links:
        hidden_links.remove(link)
    else:
        hidden_links.add(link)
    st.session_state.hidden_links = hidden_links
    mark_storage_dirty()


def show_cards(df: pd.DataFrame, runtime_id: str = "default") -> None:
    saved_links = st.session_state.get("saved_links", set())
    hidden_links = st.session_state.get("hidden_links", set())

    for index, item in enumerate(df.to_dict("records")):
        badge = "yes" if item["Remoto?"] == "Sim" else "no" if item["Remoto?"] == "Nao" else "na"
        saved_badge = '<span class="pill source">Salva</span>' if item["Link"] in saved_links else ""
        st.markdown(
            f"""
            <section class="job">
                <span class="pill source">{item['Fonte']}</span>
                <span class="pill {badge}">Remoto? {item['Remoto?']}</span>
                <span class="pill na">{item['Origem da coleta']}</span>
                {saved_badge}
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
        action_open, action_save, action_hide, action_share = st.columns([1.15, 0.8, 0.8, 1.1])
        with action_open:
            st.link_button("Abrir vaga", item["Link"], use_container_width=True)
        with action_save:
            if st.button("Salvar" if item["Link"] not in saved_links else "Remover", key=f"save_{runtime_id}_{index}", use_container_width=True):
                toggle_saved(item["Link"])
                st.rerun()
        with action_hide:
            if st.button("Ocultar" if item["Link"] not in hidden_links else "Reexibir", key=f"hide_{runtime_id}_{index}", use_container_width=True):
                toggle_hidden(item["Link"])
                st.rerun()
        with action_share:
            with st.popover("Compartilhar", use_container_width=True):
                urls = share_urls(item)
                st.markdown(f"[WhatsApp]({urls['WhatsApp']})")
                st.markdown(f"[Telegram]({urls['Telegram']})")
                st.markdown(f"[LinkedIn]({urls['LinkedIn']})")


@st.fragment(run_every="2s")
def render_live_results_fragment(location_terms: list[str], include_unknown_locations: bool) -> None:
    runtime = st.session_state.get("active_runtime")
    snapshot = runtime_snapshot(runtime)

    if snapshot is None:
        st.info("Rode uma busca para abrir o workspace de resultados.")
        return

    raw_df = build_results_df(snapshot["rows"])
    display_df = apply_display_filters(raw_df, location_terms, include_unknown_locations)

    progress_value = 0.0
    if snapshot["total_steps"]:
        progress_value = min(snapshot["completed_steps"] / snapshot["total_steps"], 1.0)

    head1, head2, head3 = st.columns([1.4, 1, 1])
    with head1:
        st.markdown(f"### {snapshot['status']}")
        st.caption(
            "Busca em andamento" if snapshot["running"] else
            ("Busca interrompida" if snapshot["stopped"] else "Busca finalizada")
        )
    with head2:
        st.progress(progress_value, text=f"{snapshot['completed_steps']} de {snapshot['total_steps']} etapas")
    with head3:
        if snapshot["running"]:
            if st.button("Interromper busca", key=f"stop_{snapshot['search_id']}", type="secondary", use_container_width=True):
                runtime.stop_event.set()
                set_runtime_status(runtime, "Interrompendo busca...")
                st.rerun()
        else:
            st.caption("Voce pode ajustar filtros de exibicao sem rodar tudo de novo.")

    if snapshot["warnings"]:
        for warning in snapshot["warnings"][-5:]:
            st.warning(warning)

    if display_df.empty:
        if raw_df.empty:
            st.info("Nenhuma vaga carregada ainda.")
        else:
            st.info("Nao ha vagas visiveis com os filtros atuais de exibicao.")
        return

    render_progress_results(display_df, snapshot["status"], final=not snapshot["running"])

    metrics_col1, metrics_col2, metrics_col3, metrics_col4 = st.columns(4)
    with metrics_col1:
        stat("Vagas visiveis", str(len(display_df)), "Depois dos filtros e ocultacoes")
    with metrics_col2:
        stat("Salvas", str(len(st.session_state.get("saved_links", set()))), "Guardadas neste navegador")
    with metrics_col3:
        stat("Ocultas", str(len(st.session_state.get("hidden_links", set()))), "Escondidas da visao principal")
    with metrics_col4:
        stat("Empresas", str(display_df["Empresa"].nunique()), "Com vagas no recorte atual")

    csv_bytes = display_df[["Fonte", "Origem da coleta", "Empresa", "Vaga", "Localizacao", "Modalidade", "Remoto?", "Data", "Link"]].to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    excel_buffer = BytesIO()
    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        display_df[["Fonte", "Origem da coleta", "Empresa", "Vaga", "Localizacao", "Modalidade", "Remoto?", "Data", "Link"]].to_excel(writer, index=False)

    download_col1, download_col2 = st.columns(2)
    with download_col1:
        st.download_button("Baixar CSV", csv_bytes, "vagas_dados_unificadas.csv", "text/csv", use_container_width=True)
    with download_col2:
        st.download_button("Baixar Excel", excel_buffer.getvalue(), "vagas_dados_unificadas.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

    feed_tab, table_tab = st.tabs(["Feed", "Tabela completa"])
    with feed_tab:
        show_cards(display_df, runtime_id=snapshot["search_id"])
    with table_tab:
        st.dataframe(
            display_df[["Fonte", "Origem da coleta", "Empresa", "Vaga", "Localizacao", "Modalidade", "Remoto?", "Data", "Link"]],
            hide_index=True,
            use_container_width=True,
            column_config={"Link": st.column_config.LinkColumn("Link", display_text="Abrir vaga")},
        )


def app() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    ensure_session_state()
    sync_browser_storage()
    hydrate_form_state_from_query()
    apply_theme()
    hero()

    greenhouse_options = cleaned_company_options(GREENHOUSE_COMPANIES + load_extra_greenhouse_companies() + st.session_state.get("greenhouse_selected_widget", []))
    quickin_options = cleaned_company_options(QUICKIN_COMPANIES + st.session_state.get("quickin_selected_widget", []))
    inhire_options = cleaned_company_options(INHIRE_COMPANIES + st.session_state.get("inhire_selected_widget", []))

    saved_searches = st.session_state.get("saved_searches", {})
    saved_names = [""] + sorted(saved_searches.keys())

    library_col1, library_col2, library_col3, library_col4 = st.columns([1.2, 0.9, 0.9, 1.2])
    with library_col1:
        st.selectbox("Buscas salvas", saved_names, key="saved_search_choice")
    with library_col2:
        if st.button("Carregar busca", use_container_width=True):
            choice = st.session_state.get("saved_search_choice", "")
            if choice and choice in saved_searches:
                apply_form_state(saved_searches[choice])
                sync_query_params_from_form_state(saved_searches[choice])
                st.rerun()
    with library_col3:
        if st.button("Remover busca", use_container_width=True):
            choice = st.session_state.get("saved_search_choice", "")
            if choice and choice in saved_searches:
                saved_searches.pop(choice, None)
                st.session_state.saved_searches = saved_searches
                st.session_state.saved_search_choice = ""
                mark_storage_dirty()
                st.rerun()
    with library_col4:
        st.text_input("Nome da busca", key="saved_search_name", placeholder="Ex.: Data remoto SP")

    history = st.session_state.get("search_history", [])
    if history:
        with st.expander("Buscas recentes neste navegador", expanded=False):
            for item in history:
                st.caption(f"{item['quando']} | {item['nome']} | {item['fontes']} | {item['localidade']}")
    if LOCAL_STORAGE_READY:
        if st.session_state.get("cookie_notice_accepted", False):
            st.caption("Favoritos, vagas ocultas, buscas salvas e historico recente ficam guardados apenas neste navegador.")
        else:
            st.caption("Ao aceitar o aviso no rodape, o app passa a guardar favoritos, vagas ocultas e buscas salvas apenas neste navegador.")
    else:
        st.caption("O modo de persistencia local nao esta disponivel neste ambiente. O app continua funcionando, mas sem salvar dados no navegador.")

    st.markdown('<section class="control-shell">', unsafe_allow_html=True)
    with st.form("search_form"):
        top_left, top_right = st.columns([1.1, 1.35])
        with top_left:
            sources = st.multiselect("Fontes", ["Greenhouse", "Gupy", "Quickin", "InHire"], key="sources_widget")
            only_remote = st.toggle("Apenas vagas remotas", key="only_remote_widget")
            gupy_pages = st.slider("Paginas por termo na Gupy", 1, 8, key="gupy_pages_widget")
            inhire_timeout_ms = st.slider("Timeout por empresa no InHire (ms)", 5000, 30000, step=1000, key="inhire_timeout_widget")
            include_unknown_locations = st.toggle("Incluir localizacao N/A no filtro", key="include_unknown_locations_widget")
            st.caption("A Gupy agora so considera vagas publicadas em 2026 ou depois.")
            st.caption("No InHire, vagas sem info de remoto continuam aparecendo com modalidade N/A.")
        with top_right:
            include_raw = st.text_area("Termos de inclusao", height=110, key="include_raw_widget")
            exclude_raw = st.text_area("Termos de exclusao", height=110, key="exclude_raw_widget")
            location_raw = st.text_input("Filtro de localidade", key="location_raw_widget", help="Ex.: sao paulo, remoto, brasilia, rio de janeiro")

        boards_tab, quickin_tab, inhire_tab = st.tabs(["Boards Greenhouse", "Empresas Quickin", "Empresas InHire"])
        with boards_tab:
            greenhouse_selected = st.multiselect("Selecione os boards", greenhouse_options, key="greenhouse_selected_widget")
            st.caption("Para adicionar mais empresas, use o alias/slug do board, nao apenas o nome da empresa. Pode separar por virgula, ponto e virgula ou quebra de linha.")
            greenhouse_add_raw = st.text_area("Adicionar boards manualmente", height=80, help="Ex.: nubank, ifoodcarreiras, stone", key="greenhouse_add_raw_widget")
        with quickin_tab:
            quickin_selected = st.multiselect("Selecione as empresas", quickin_options, key="quickin_selected_widget")
            st.caption("Use o alias da empresa no Quickin, como aparece na URL do board. Pode separar por virgula, ponto e virgula ou quebra de linha.")
            quickin_add_raw = st.text_area("Adicionar empresas Quickin", height=80, help="Ex.: startse, topmind, registradores", key="quickin_add_raw_widget")
        with inhire_tab:
            inhire_selected = st.multiselect("Selecione as empresas", inhire_options, key="inhire_selected_widget")
            st.caption("Use o alias da empresa no InHire, igual ao subdominio do board. Pode separar por virgula, ponto e virgula ou quebra de linha.")
            inhire_add_raw = st.text_area("Adicionar empresas InHire", height=80, help="Ex.: olist, sympla, contabilizei", key="inhire_add_raw_widget")

        left, mid, right = st.columns([1, 1, 2])
        with left:
            clicked = st.form_submit_button("Buscar vagas agora", type="primary", use_container_width=True)
        with mid:
            save_search_clicked = st.form_submit_button("Salvar esta busca", use_container_width=True)
        with right:
            share_clicked = st.form_submit_button("Atualizar link compartilhavel", use_container_width=True)
            st.caption("Os resultados entram em tela conforme cada fonte termina. No InHire, a alimentacao acontece empresa por empresa.")
    st.markdown("</section>", unsafe_allow_html=True)

    form_state = current_form_state()
    if save_search_clicked:
        save_current_search(st.session_state.get("saved_search_name", ""), form_state)
        st.success("Busca salva neste navegador.")
    if share_clicked:
        sync_query_params_from_form_state(form_state)
        st.success("URL atualizada com os filtros atuais. Agora e so copiar o link do navegador.")

    greenhouse_companies = merge_company_selection(greenhouse_selected, greenhouse_add_raw)
    quickin_companies = merge_company_selection(quickin_selected, quickin_add_raw)
    inhire_companies = merge_company_selection(inhire_selected, inhire_add_raw)

    config = SearchConfig(
        sources,
        parse_terms(include_raw),
        parse_terms(exclude_raw),
        parse_terms(location_raw),
        include_unknown_locations,
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

    display_col1, display_col2, display_col3 = st.columns([1.2, 1, 1.4])
    with display_col1:
        st.toggle("Mostrar apenas vagas salvas", key="show_only_saved")
    with display_col2:
        st.toggle("Mostrar vagas ocultas", key="show_hidden_jobs")
    with display_col3:
        st.caption("Use salvar, ocultar e compartilhar direto no feed para transformar a busca em rotina de acompanhamento.")

    if problems:
        for problem in problems:
            st.error(problem)
        return
    active_runtime = st.session_state.get("active_runtime")
    active_snapshot = runtime_snapshot(active_runtime)

    if clicked:
        if active_snapshot and active_snapshot["running"]:
            st.warning("Ja existe uma busca em andamento. Interrompa a atual antes de iniciar outra.")
        else:
            register_search_history(st.session_state.get("saved_search_name", "").strip(), config)
            start_background_search(config)
            st.rerun()

    render_live_results_fragment(config.location_terms, config.include_unknown_locations)
    render_cookie_notice()


if __name__ == "__main__":
    app()
