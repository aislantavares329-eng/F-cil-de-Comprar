# app.py — Comparador de preços (Centerbox + Mercadinhos São Luiz)
# - Trata /loja/XX (cookies, CEP, “CLIQUE E RETIRE”) → /clube (Centerbox) /ofertas (São Luiz)
# - Extrai preços de: HTML, XHR/JSON VTEX e JSON embutido (<script>)
# - Fallback: busca por item (/busca?ft=...) para cobrir os itens do catálogo

import re
import json
import time
import unicodedata
import subprocess
import urllib.parse
from urllib.parse import urlparse

import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ------------------- UI -------------------
st.set_page_config(page_title="Comparador (Centerbox x São Luiz)", layout="wide")
st.title("🛒 Comparador — cole os dois links e, se preciso, um CEP")

c1, c2 = st.columns(2)
with c1:
    url1 = st.text_input("🔗 URL do Supermercado #1", "")
with c2:
    url2 = st.text_input("🔗 URL do Supermercado #2", "")

cep = st.text_input("📍 CEP (opcional, p/ desbloquear loja)", "")
go = st.button("Comparar")

# ------------------- Utils de texto / dinheiro / tamanhos -------------------
def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s).lower())
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace() or ch in "xµ/.-")
    return " ".join(s.split())

def cleanup_money(txt: str):
    if not isinstance(txt, str):
        return None
    m = re.search(r"(\d{1,3}(?:\.\d{3})*|\d+)\s*[,\.](\d{2})", txt)
    if not m:
        return None
    inteiro = m.group(1).replace(".", "")
    cent = m.group(2)
    try:
        return float(f"{inteiro}.{cent}")
    except Exception:
        return None

BRAND_ALIASES = {
    "nestle": "nestle",
    "nestlé": "nestle",
    "ninho": "ninho",
    "danone": "danone",
    "omo": "omo",
    "ype": "ype",
    "ypê": "ype",
    "veja": "veja",
    "pinho sol": "pinho sol",
    "pinho": "pinho sol",
    "downy": "downy",
    "comfort": "comfort",
}

def extract_brands(text_norm: str):
    brands = set()
    if "pinho sol" in text_norm:
        brands.add("pinho sol")
    for b in BRAND_ALIASES.keys():
        if b != "pinho sol" and b in text_norm:
            brands.add(BRAND_ALIASES[b])
    return brands

SIZE_REGEX = re.compile(r"(?:(\d{1,3})\s*[xX]\s*)?(\d+(?:[\.,]\d+)?)\s*(kg|g|l|ml)\b")

def parse_size(text_norm: str):
    total_g, total_ml, pack = None, None, 1
    for m in SIZE_REGEX.finditer(text_norm):
        p = int(m.group(1)) if m.group(1) else 1
        val = float(m.group(2).replace(",", "."))
        unit = m.group(3)
        if unit == "kg":
            g = val * 1000 * p
            total_g = max(total_g or 0, g)
        elif unit == "g":
            g = val * p
            total_g = max(total_g or 0, g)
        elif unit == "l":
            ml = val * 1000 * p
            total_ml = max(total_ml or 0, ml)
        elif unit == "ml":
            ml = val * p
            total_ml = max(total_ml or 0, ml)
        pack = max(pack, p)
    return {"pack": pack, "effective_g": total_g, "effective_ml": total_ml}

def approx(val, tgt, tol):
    return (val is not None) and (tgt - tol) <= val <= (tgt + tol)

# ------------------- Catálogo por seção -------------------
CATALOG = {
    "ALIMENTOS": [
        {"key": "Arroz 5 kg", "must": ["arroz"], "size_g": 5000, "size_tol_g": 600, "q": "arroz 5kg"},
        {"key": "Feijão 1 kg", "must": ["feijao"], "size_g": 1000, "size_tol_g": 200, "q": "feijao 1kg"},
        {
            "key": "Leite em pó Ninho 380 g",
            "must": ["leite", "po"],
            "brand_any": ["ninho", "nestle"],
            "size_g": 380,
            "size_tol_g": 60,
            "q": "leite po ninho 380g",
        },
        {"key": "Macarrão 500 g", "must": ["macarrao"], "size_g": 500, "size_tol_g": 100, "q": "macarrao 500g"},
        {"key": "Açúcar 1 kg", "must": ["acucar"], "size_g": 1000, "size_tol_g": 200, "q": "acucar 1kg"},
        {"key": "Sal 1 kg", "must": ["sal"], "size_g": 1000, "size_tol_g": 200, "q": "sal 1kg"},
        {"key": "Café 500 g", "must": ["cafe"], "size_g": 500, "size_tol_g": 100, "q": "cafe 500g"},
        {"key": "Farinha de trigo 1 kg", "must": ["farinha", "trigo"], "size_g": 1000, "size_tol_g": 200, "q": "farinha trigo 1kg"},
        {"key": "Massa de milho (Fubá) 1 kg", "must": ["fuba"], "alt_any": [["massa", "milho"]], "size_g": 1000, "size_tol_g": 300, "q": "fuba 1kg"},
        {"key": "Carne bovina (kg)", "must": ["carne"], "alt_any": [["bovina"], ["patinho"], ["contrafile"], ["alcatra"], ["acem"], ["coxao"]], "perkg": True, "q": "carne bovina kg"},
    ],
    "FRUTAS": [
        {"key": "Mamão (kg)", "must": ["mamao"], "alt_any": [["papaya"], ["formosa"]], "perkg": True, "q": "mamao kg"},
        {"key": "Banana (kg)", "must": ["banana"], "perkg": True, "q": "banana kg"},
        {"key": "Pera (kg)", "must": ["pera"], "perkg": True, "q": "pera kg"},
        {"key": "Uva (kg)", "must": ["uva"], "perkg": True, "q": "uva kg"},
        {"key": "Tangerina (kg)", "must": ["tangerina"], "alt_any": [["mexerica"], ["bergamota"]], "perkg": True, "q": "tangerina kg"},
    ],
    "PRODUTO DE LIMPEZA": [
        {"key": "Sabão líquido OMO 3 L", "must": ["sabao", "liquido"], "brand_any": ["omo"], "size_ml": 3000, "size_tol_ml": 600, "q": "sabao liquido omo 3l"},
        {"key": "Amaciante Downy 1 L", "must": ["amaciante"], "brand_any": ["downy", "comfort"], "size_ml": 1000, "size_tol_ml": 300, "q": "amaciante downy 1l"},
        {"key": "Veja Multiuso 500 ml", "must": ["veja"], "size_ml": 500, "size_tol_ml": 150, "q": "veja multiuso 500ml"},
        {"key": "Pinho Sol 1 L", "must": ["pinho", "sol"], "size_ml": 1000, "size_tol_ml": 300, "q": "pinho sol 1l"},
        {"key": "Detergente Ypê 500 ml", "must": ["detergente"], "brand_any": ["ype"], "size_ml": 500, "size_tol_ml": 150, "q": "detergente ype 500ml"},
        {"key": "Água sanitária 1 L", "must": ["agua", "sanitaria"], "alt_any": [["candida"]], "size_ml": 1000, "size_tol_ml": 300, "q": "agua sanitaria 1l"},
    ],
    "BEBIDA LÁCTEA": [
        {"key": "Iogurte integral Nestlé 170 g", "must": ["iogurte", "integral"], "brand_any": ["nestle", "ninho"], "size_g": 170, "size_tol_g": 60, "q": "iogurte integral nestle 170g"},
        {"key": "Iogurte integral Danone 170 g", "must": ["iogurte", "integral"], "brand_any": ["danone"], "size_g": 170, "size_tol_g": 60, "q": "iogurte integral danone 170g"},
    ],
}

def tokens_ok(text_norm, must):
    return all(tok in text_norm for tok in (must or []))

def any_alt_hit(text_norm, alt_any):
    if not alt_any:
        return True
    return any(all(tok in text_norm for tok in grp) for grp in alt_any)

def brand_ok(text_norm, brand_any):
    if not brand_any:
        return True
    brands = extract_brands(text_norm)
    return any(b in brands for b in brand_any)

def size_ok(text_norm, size_g=None, tol_g=None, size_ml=None, tol_ml=None, perkg=False):
    if perkg:
        return True
    if size_g is None and size_ml is None:
        return True
    parsed = parse_size(text_norm)
    if size_g is not None:
        return approx(parsed["effective_g"], size_g, tol_g or 0)
    if size_ml is not None:
        return approx(parsed["effective_ml"], size_ml, tol_ml or 0)
    return True

def match_canonical(prod_name: str):
    n = norm(prod_name)
    for section, items in CATALOG.items():
        for item in items:
            if not tokens_ok(n, item.get("must")):
                continue
            if not any_alt_hit(n, item.get("alt_any")):
                continue
            if not brand_ok(n, item.get("brand_any")):
                continue
            if not size_ok(
                n,
                item.get("size_g"),
                item.get("size_tol_g"),
                item.get("size_ml"),
                item.get("size_tol_ml"),
                perkg=item.get("perkg", False),
            ):
                continue
            return section, item["key"]
    return None, None

# ------------------- Playwright helpers -------------------
def ensure_chromium_installed():
    try:
        subprocess.run(
            ["python", "-m", "playwright", "install", "chromium", "--with-deps"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        subprocess.run(
            ["python", "-m", "playwright", "install", "chromium"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

def click_if_visible(page, role=None, name_regex=None, css=None, timeout=900):
    try:
        if css:
            el = page.locator(css)
            if el and el.is_visible():
                el.click()
                page.wait_for_timeout(timeout)
                return True
        if role and name_regex:
            btn = page.get_by_role(role, name=re.compile(name_regex, re.I)).first
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(timeout)
                return True
    except Exception:
        return False
    return False

def set_store_and_cep(page, cep, host=None):
    """Fluxo teimoso p/ Centerbox (CEP + 'CLIQUE E RETIRE') e genérico p/ demais."""
    # 1) cookies
    for pat in ["Aceitar", "Aceito", "Concordo", "Permitir", "OK", "Prosseguir", "Continuar", "Fechar"]:
        try:
            page.get_by_role("button", name=re.compile(pat, re.I)).first.click()
            page.wait_for_timeout(700)
            break
        except Exception:
            pass

    # 2) abrir modal (pin/entrega/retirada)
    try:
        page.get_by_role("button", name=re.compile("Selecion(e|ar).+método|Entrega|Retirada|loja", re.I)).first.click()
        page.wait_for_timeout(600)
    except Exception:
        pass

    # 3) CEP
    if cep:
        filled = False
        for css in ['input[placeholder*="CEP"]', 'input[placeholder*="cep"]', 'input[type="tel"]', 'input[name*="cep"]']:
            try:
                el = page.locator(css).first
                if el and el.is_visible():
                    el.fill(cep)
                    filled = True
                    page.wait_for_timeout(400)
                    break
            except Exception:
                pass
        if not filled:
            try:
                page.get_by_placeholder(re.compile("CEP", re.I)).fill(cep)
                page.wait_for_timeout(400)
            except Exception:
                pass

    # 4) Centerbox: “CLIQUE E RETIRE” (ou entrega como fallback)
    try:
        btn = page.get_by_role("button", name=re.compile(r"CLIQUE\s*E\s*RETIRE|Retirar|Retire", re.I)).first
        if btn and btn.is_visible():
            btn.click()
            page.wait_for_timeout(900)
        else:
            page.get_by_role("button", name=re.compile(r"RECEBA\s*EM\s*CASA|Entrega", re.I)).first.click()
            page.wait_for_timeout(900)
    except Exception:
        pass

    # 5) lista de lojas → selecionar
    try:
        for pat in ["Selecionar esta loja", "Usar esta loja", "Selecionar", "Usar loja", "Escolher esta loja"]:
            btn = page.get_by_role("button", name=re.compile(pat, re.I)).first
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(1000)
                break
    except Exception:
        pass

    # 6) confirma/avança se aparecer
    for pat in ["Confirmar", "Aplicar", "Continuar", "Salvar", "OK"]:
        try:
            page.get_by_role("button", name=re.compile(pat, re.I)).first.click()
            page.wait_for_timeout(800)
            break
        except Exception:
            pass

    # 7) se host informado, garanta que está na listagem
    if host:
        try:
            pu = urlparse(page.url)
            if re.search(r"/loja/\d+", pu.path or ""):
                if "centerbox" in host:
                    page.goto(f"{host}/clube", wait_until="domcontentloaded")
                elif "saoluiz" in host or "mercadinho" in host:
                    page.goto(f"{host}/ofertas", wait_until="domcontentloaded")
                page.wait_for_timeout(1000)
        except Exception:
            pass

def go_to_list(page, host):
    pu = urlparse(page.url)
    if re.search(r"/loja/\d+", pu.path or ""):
        if "centerbox" in host:
            page.goto(f"{host}/clube", wait_until="domcontentloaded")
        elif "saoluiz" in host or "mercadinho" in host:
            page.goto(f"{host}/ofertas", wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
    if not re.search(r"oferta|clube|promo|categoria|busca", page.url, re.I):
        for pat in ["Ofertas", "Clube", "Promo"]:
            if click_if_visible(page, role="link", name_regex=pat):
                break

def wait_prices(page):
    try:
        page.wait_for_selector('.vtex-product-price-1-x-sellingPriceValue, .best-price, meta[itemprop="price"]', timeout=12000)
    except Exception:
        pass

def scroll_and_more(page, rounds=18):
    for _ in range(rounds):
        page.mouse.wheel(0, 2200)
        page.wait_for_timeout(320)
    for _ in range(5):
        if not click_if_visible(page, role="button", name_regex="ver mais|mais produtos|carregar"):
            break

# --------- JSON walker (VTEX + genérico) ----------
PRICE_KEYS = {"price", "salePrice", "bestPrice", "value", "finalPrice", "sellingPrice", "Price", "SellingPrice", "unitPrice"}
NAME_KEYS = {"name", "productName", "itemName", "title", "Name", "Title", "product_name", "description"}

def walk_json_for_products(obj):
    found = []
    try:
        if isinstance(obj, dict):
            if "commertialOffer" in obj and isinstance(obj["commertialOffer"], dict):
                raw = obj["commertialOffer"].get("Price") or obj["commertialOffer"].get("ListPrice")
                name = obj.get("name") or obj.get("productName") or obj.get("itemName") or ""
                if raw is not None and name:
                    try:
                        found.append({"name": str(name), "price": float(raw)})
                    except Exception:
                        pass
            has_name = any(k in obj for k in NAME_KEYS)
            has_price = any(k in obj for k in PRICE_KEYS)
            if has_name and has_price:
                name = next((str(obj[k]) for k in NAME_KEYS if k in obj and obj[k]), "")
                raw = next((obj[k] for k in PRICE_KEYS if k in obj and obj[k] is not None), None)
                if isinstance(raw, str):
                    price = cleanup_money(raw)
                elif isinstance(raw, (int, float)):
                    price = float(raw)
                else:
                    price = None
                if name and price is not None:
                    found.append({"name": name, "price": price})
            for v in obj.values():
                found += walk_json_for_products(v)
        elif isinstance(obj, list):
            for x in obj:
                found += walk_json_for_products(x)
    except Exception:
        pass
    return found

def parse_inline_state(html: str):
    items = []
    try:
        for m in re.finditer(r"__STATE__\s*=\s*({.*?});</script>", html, re.S):
            data = json.loads(m.group(1))
            items += walk_json_for_products(data)
        for m in re.finditer(r"__NEXT_DATA__\s*=\s*({.*?});</script>", html, re.S):
            data = json.loads(m.group(1))
            items += walk_json_for_products(data)
        for m in re.finditer(r"<script[^>]*application/json[^>]*>(.*?)</script>", html, re.S | re.I):
            try:
                data = json.loads(m.group(1))
                items += walk_json_for_products(data)
            except Exception:
                pass
    except Exception:
        pass
    # dedup
    out, seen = [], set()
    for it in items:
        if not it.get("name") or not isinstance(it.get("price"), (int, float)):
            continue
        k = (it["name"], round(float(it["price"]), 2))
        if k not in seen:
            out.append(it)
            seen.add(k)
    return out

# ------------------- Coleta principal -------------------
def fetch_with_browser(url: str, cep: str):
    ensure_chromium_installed()
    captured = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-zygote",
                    "--single-process",
                ],
            )
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(30000)

            def on_response(res):
                try:
                    ct = (res.headers or {}).get("content-type", "").lower()
                    if "application/json" in ct or res.url.endswith(".json"):
                        try:
                            data = res.json()
                        except Exception:
                            try:
                                data = json.loads(res.text())
                            except Exception:
                                data = None
                        if data is not None:
                            captured.extend(walk_json_for_products(data))
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                page.goto(url, wait_until="domcontentloaded")
            except PwTimeout:
                pass

            host = f"{urlparse(page.url).scheme}://{urlparse(page.url).netloc}"
            set_store_and_cep(page, cep, host=host)
            go_to_list(page, host)
            wait_prices(page)
            scroll_and_more(page)

            html = page.content()
            context.close()
            browser.close()
            return html, captured
    except Exception:
        return "", []

# ------------------- Extratores HTML -------------------
def extract_cards(html: str):
    items = []
    soup = BeautifulSoup(html or "", "lxml")
    cards = soup.select(
        '[data-testid="product-summary-container"], .vtex-product-summary-2-x-container, '
        '.product-card, .shelf-item, .product, [itemtype*="Product"]'
    )
    for c in cards[:2000]:
        name = None
        for sel in [
            '[data-testid="product-name"]',
            '.vtex-product-summary-2-x-productBrand',
            '.product-title',
            '.name',
            'h3',
            'h2',
            '[itemprop="name"]',
        ]:
            el = c.select_one(sel)
            if el and el.get_text(strip=True):
                name = el.get_text(" ", strip=True)
                break
        if not name:
            name = c.get_text(" ", strip=True)[:200]

        price = None
        meta = c.select_one('meta[itemprop="price"]')
        if meta and meta.has_attr("content"):
            try:
                price = float(str(meta["content"]).replace(",", "."))
            except Exception:
                price = None
        if price is None:
            elp = c.select_one(
                '.vtex-product-price-1-x-sellingPriceValue, .best-price, .price, '
                '[data-price], .vtex-product-price-1-x-currencyInteger'
            )
            if elp:
                price = cleanup_money(elp.get_text(" ", strip=True))
        if name and price is not None:
            items.append({"name": name[:200], "price": price})

    out, seen = [], set()
    for it in items:
        k = (it["name"], round(float(it["price"]), 2))
        if k not in seen:
            out.append(it)
            seen.add(k)
    return out

# ------------------- Fallback: /busca?ft=... -------------------
def search_and_pick(page, host, query, validator):
    url = f"{host}/busca?ft={urllib.parse.quote(query)}"
    try:
        page.goto(url, wait_until="domcontentloaded")
        wait_prices(page)
        scroll_and_more(page, rounds=8)
        html = page.content()
        items = extract_cards(html)
        for it in items[:10]:
            if validator(it["name"]):
                return it
    except Exception:
        return None
    return None

def fill_with_search(host, cep, already, needed_keys):
    ensure_chromium_installed()
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-zygote",
                    "--single-process",
                ],
            )
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(25000)

            page.goto(host, wait_until="domcontentloaded")
            set_store_and_cep(page, cep, host=host)

            for section, items in CATALOG.items():
                for item in items:
                    key = item["key"]
                    if key not in needed_keys:
                        continue
                    q = item.get("q") or key

                    def validator(name):
                        sec, k = match_canonical(name)
                        return (sec == section and k == key)

                    hit = search_and_pick(page, host, q, validator)
                    if hit:
                        results.append(hit)

            context.close()
            browser.close()
    except Exception:
        pass

    out, seen = [], set()
    for it in results:
        k = (it["name"], round(float(it["price"]), 2))
        if k in seen:
            continue
        seen.add(k)
        out.append(it)

    out2 = []
    for it in out:
        sec, k = match_canonical(it["name"])
        if not sec:
            continue
        if (sec, k) not in already:
            out2.append(it)
    return out2

# ------------------- Comparação -------------------
def map_prices(items):
    mapped = {}
    for it in items:
        sec, key = match_canonical(it["name"])
        if not sec:
            continue
        price = it["price"]
        cur = mapped.get((sec, key))
        if cur is None or price < cur:
            mapped[(sec, key)] = price
    return mapped

if go:
    if not url1 or not url2:
        st.error("Manda os dois links 😉")
        st.stop()

    with st.spinner("Abrindo páginas, definindo loja/CEP e coletando preços…"):
        html1, net1 = fetch_with_browser(url1, cep)
        html2, net2 = fetch_with_browser(url2, cep)

    items1 = extract_cards(html1) + net1 + parse_inline_state(html1)
    items2 = extract_cards(html2) + net2 + parse_inline_state(html2)

    def dedup(items):
        out, seen = [], set()
        for it in items:
            if not it.get("name") or not isinstance(it.get("price"), (int, float)):
                continue
            k = (it["name"], round(float(it["price"]), 2))
            if k not in seen:
                out.append({"name": it["name"], "price": float(it["price"])})
                seen.add(k)
        return out

    items1 = dedup(items1)
    items2 = dedup(items2)

    host1 = f"{urlparse(url1).scheme}://{urlparse(url1).netloc}"
    host2 = f"{urlparse(url2).scheme}://{urlparse(url2).netloc}"

    map1 = map_prices(items1)
    map2 = map_prices(items2)

    def missing_keys(mapped):
        want = set()
        for section, items in CATALOG.items():
            for it in items:
                if (section, it["key"]) not in mapped:
                    want.add((section, it["key"]))
        return want

    miss1 = missing_keys(map1)
    miss2 = missing_keys(map2)

    if len(miss1) > 6:
        need_keys = {k for _, k in miss1}
        extra = fill_with_search(host1, cep, map1, need_keys)
        items1 += extra
        map1 = map_prices(items1)

    if len(miss2) > 6:
        need_keys = {k for _, k in miss2}
        extra = fill_with_search(host2, cep, map2, need_keys)
        items2 += extra
        map2 = map_prices(items2)

    name1 = urlparse(url1).netloc.replace("www.", "")
    name2 = urlparse(url2).netloc.replace("www.", "")

    total1 = total2 = 0.0
    sum1 = sum2 = 0.0
    pairs = 0

    st.markdown("---")
    for section, products in CATALOG.items():
        rows = []
        for p in products:
            key = p["key"]
            v1 = map1.get((section, key))
            v2 = map2.get((section, key))
            rows.append(
                {
                    "Produto": key,
                    name1: (f"R$ {v1:.2f}" if isinstance(v1, (int, float)) else "—"),
                    name2: (f"R$ {v2:.2f}" if isinstance(v2, (int, float)) else "—"),
                }
            )
            if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
                if v1 < v2:
                    total1 += 1
                elif v2 < v1:
                    total2 += 1
                else:
                    total1 += 0.5
                    total2 += 0.5
                sum1 += v1
                sum2 += v2
                pairs += 1
        st.subheader(section)
        st.dataframe(pd.DataFrame(rows), use_container_width=True)

    st.markdown("---")
    st.markdown("### 🏁 Resultado")

    def fmt(x):
        return f"{x:.1f}".replace(".0", "")

    msg = f"**{name1}** {fmt(total1)} × {fmt(total2)} **{name2}** (critério: mais itens com menor preço)"
    if total1 > total2:
        winner = name1
    elif total2 > total1:
        winner = name2
    else:
        if pairs > 0:
            if sum1 < sum2:
                winner = name1
                msg += f". Desempate pela menor soma (R$ {sum1:.2f} vs R$ {sum2:.2f})."
            elif sum2 < sum1:
                winner = name2
                msg += f". Desempate pela menor soma (R$ {sum2:.2f} vs R$ {sum1:.2f})."
            else:
                winner = f"{name1} / {name2}"
                msg += ". Empate após soma."
        else:
            winner = f"{name1} / {name2}"
            msg += ". Empate técnico (pouco item em comum)."

    st.success(f"🏆 **Vencedor:** {winner}\n\n{msg}")

    with st.expander("🔎 Itens brutos"):
        st.write(name1, len(items1))
        st.write(items1[:40])
        st.write(name2, len(items2))
        st.write(items2[:40])

st.caption("Dica: cole /loja/XX ou diretamente /clube (Centerbox) /ofertas (São Luiz). Informe um CEP se o site pedir loja.")
