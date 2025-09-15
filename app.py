# app.py — Comparador “cole os links e pronto” (Centerbox + São Luiz)
# - Trata /loja/XX: aceita cookies, seleciona loja, define CEP, redireciona p/ /clube ou /ofertas
# - Espera seletor de preço, scroll, clica "ver mais"
# - Captura HTML e XHR/JSON (VTEX), com log de URLs úteis p/ debug
# - Fallback requests_html se o Chromium não subir

import re, json, unicodedata, subprocess, time
from urllib.parse import urlparse

import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ------------------- UI -------------------
st.set_page_config(page_title="Comparador de Supermercados (link direto)", layout="wide")
st.title("🛒 Comparador — cole os links, opcionalmente um CEP, e pronto")

c1, c2 = st.columns(2)
with c1:
    url1 = st.text_input("🔗 URL do Supermercado #1", placeholder="Ex.: https://loja.centerbox.com.br/loja/58 ou /clube")
with c2:
    url2 = st.text_input("🔗 URL do Supermercado #2", placeholder="Ex.: https://mercadinhossaoluiz.com.br/loja/355 ou /ofertas")

cep = st.text_input("📍 CEP (opcional – alguns sites liberam preços só após definir loja)", placeholder="Ex.: 60000-000")
go = st.button("Comparar")

# ------------------- Helpers (texto/tamanho/marca/dinheiro) -------------------
def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s).lower())
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace() or ch in "xµ/.-")
    return " ".join(s.split())

def cleanup_money(txt: str):
    if not isinstance(txt, str): return None
    m = re.search(r"(\d{1,3}(?:\.\d{3})*|\d+)[\s]*[,\.](\d{2})", txt)
    if not m: return None
    inteiro = m.group(1).replace(".", "")
    cent = m.group(2)
    try: return float(f"{inteiro}.{cent}")
    except Exception: return None

BRAND_ALIASES = {
    "nestle":"nestle","nestlé":"nestle","ninho":"ninho",
    "danone":"danone","omo":"omo","ype":"ype","ypê":"ype",
    "veja":"veja","pinho sol":"pinho sol","pinho":"pinho sol",
    "downy":"downy","comfort":"comfort",
}
def extract_brands(text_norm: str):
    brands=set()
    if "pinho sol" in text_norm: brands.add("pinho sol")
    for b in BRAND_ALIASES.keys():
        if b!="pinho sol" and b in text_norm: brands.add(BRAND_ALIASES[b])
    return brands

SIZE_REGEX = re.compile(r"(?:(\d{1,3})\s*[xX]\s*)?(\d+(?:[\.,]\d+)?)\s*(kg|g|l|ml)\b")
def parse_size(text_norm: str):
    total_g,total_ml,pack=None,None,1
    for m in SIZE_REGEX.finditer(text_norm):
        p=int(m.group(1)) if m.group(1) else 1
        val=float(m.group(2).replace(",",".")); unit=m.group(3)
        if unit=="kg": g=val*1000*p; total_g=max(total_g or 0,g)
        elif unit=="g": g=val*p; total_g=max(total_g or 0,g)
        elif unit=="l": ml=val*1000*p; total_ml=max(total_ml or 0,ml)
        elif unit=="ml": ml=val*p; total_ml=max(total_ml or 0,ml)
        pack=max(pack,p)
    return {"pack":pack,"effective_g":total_g,"effective_ml":total_ml}

def approx(val,tgt,tol): return (val is not None) and (tgt-tol)<=val<=(tgt+tol)

# ------------------- Catálogo por SEÇÃO -------------------
CATALOG = {
    "ALIMENTOS":[
        {"key":"Arroz 5 kg","must":["arroz"],"size_g":5000,"size_tol_g":600},
        {"key":"Feijão 1 kg","must":["feijao"],"size_g":1000,"size_tol_g":200},
        {"key":"Leite em pó Ninho 380 g","must":["leite","po"],"brand_any":["ninho","nestle"],"size_g":380,"size_tol_g":60},
        {"key":"Macarrão 500 g","must":["macarrao"],"size_g":500,"size_tol_g":100},
        {"key":"Açúcar 1 kg","must":["acucar"],"size_g":1000,"size_tol_g":200},
        {"key":"Sal 1 kg","must":["sal"],"size_g":1000,"size_tol_g":200},
        {"key":"Café 500 g","must":["cafe"],"size_g":500,"size_tol_g":100},
        {"key":"Farinha de trigo 1 kg","must":["farinha","trigo"],"size_g":1000,"size_tol_g":200},
        {"key":"Massa de milho (Fubá) 1 kg","must":["fuba"],"alt_any":[["massa","milho"]],"size_g":1000,"size_tol_g":300},
        {"key":"Carne bovina (kg)","must":["carne"],"alt_any":[["bovina"],["patinho"],["contrafile"],["alcatra"],["acem"],["coxao"]],"perkg":True},
    ],
    "FRUTAS":[
        {"key":"Mamão (kg)","must":["mamao"],"alt_any":[["papaya"],["formosa"]],"perkg":True},
        {"key":"Banana (kg)","must":["banana"],"perkg":True},
        {"key":"Pera (kg)","must":["pera"],"perkg":True},
        {"key":"Uva (kg)","must":["uva"],"perkg":True},
        {"key":"Tangerina (kg)","must":["tangerina"],"alt_any":[["mexerica"],["bergamota"]],"perkg":True},
    ],
    "PRODUTO DE LIMPEZA":[
        {"key":"Sabão líquido OMO 3 L","must":["sabao","liquido"],"brand_any":["omo"],"size_ml":3000,"size_tol_ml":600},
        {"key":"Amaciante Downy 1 L","must":["amaciante"],"brand_any":["downy","comfort"],"size_ml":1000,"size_tol_ml":300},
        {"key":"Veja Multiuso 500 ml","must":["veja"],"size_ml":500,"size_tol_ml":150},
        {"key":"Pinho Sol 1 L","must":["pinho","sol"],"size_ml":1000,"size_tol_ml":300},
        {"key":"Detergente Ypê 500 ml","must":["detergente"],"brand_any":["ype"],"size_ml":500,"size_tol_ml":150},
        {"key":"Água sanitária 1 L","must":["agua","sanitaria"],"alt_any":[["candida"]],"size_ml":1000,"size_tol_ml":300},
    ],
    "BEBIDA LÁCTEA":[
        {"key":"Iogurte integral Nestlé 170 g","must":["iogurte","integral"],"brand_any":["nestle","ninho"],"size_g":170,"size_tol_g":60},
        {"key":"Iogurte integral Danone 170 g","must":["iogurte","integral"],"brand_any":["danone"],"size_g":170,"size_tol_g":60},
    ]
}
def tokens_ok(text_norm, must): return all(tok in text_norm for tok in (must or []))
def any_alt_hit(text_norm, alt_any):
    if not alt_any: return True
    return any(all(tok in text_norm for tok in grp) for grp in alt_any)
def brand_ok(text_norm, brand_any):
    if not brand_any: return True
    brands = extract_brands(text_norm)
    return any(b in brands for b in brand_any)
def size_ok(text_norm, size_g=None, tol_g=None, size_ml=None, tol_ml=None, perkg=False):
    if perkg: return True
    if size_g is None and size_ml is None: return True
    parsed = parse_size(text_norm)
    if size_g is not None:  return approx(parsed["effective_g"], size_g, tol_g or 0)
    if size_ml is not None: return approx(parsed["effective_ml"], size_ml, tol_ml or 0)
    return True
def match_canonical(prod_name: str):
    n = norm(prod_name)
    for section, items in CATALOG.items():
        for item in items:
            if not tokens_ok(n, item.get("must")): continue
            if not any_alt_hit(n, item.get("alt_any")): continue
            if not brand_ok(n, item.get("brand_any")): continue
            if not size_ok(n, item.get("size_g"), item.get("size_tol_g"),
                          item.get("size_ml"), item.get("size_tol_ml"),
                          perkg=item.get("perkg", False)): continue
            return section, item["key"]
    return None, None

# ------------------- Playwright: garantir Chromium + helpers de navegação -------------------
def ensure_chromium_installed():
    try:
        subprocess.run(
            ["python", "-m", "playwright", "install", "chromium", "--with-deps"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        subprocess.run(
            ["python", "-m", "playwright", "install", "chromium"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

def click_if_visible(page, role=None, name_regex=None, css=None, timeout=1500):
    try:
        if css:
            el = page.locator(css)
            if el and el.is_visible():
                el.click(); page.wait_for_timeout(timeout); return True
        if role and name_regex:
            btn = page.get_by_role(role, name=re.compile(name_regex, re.I)).first
            if btn and btn.is_visible():
                btn.click(); page.wait_for_timeout(timeout); return True
    except Exception:
        return False
    return False

def accept_cookies(page):
    # tenta vários botões comuns
    for pat in ["Aceitar", "Aceito", "Concordo", "Permitir", "OK", "Prosseguir", "Continuar", "Fechar"]:
        if click_if_visible(page, role="button", name_regex=pat): return
        # alguns banners usam <a> como botão
        try:
            link = page.get_by_role("link", name=re.compile(pat, re.I)).first
            if link and link.is_visible():
                link.click(); page.wait_for_timeout(1000); return
        except Exception:
            pass

def set_store_and_cep(page, host, cep):
    """Para páginas /loja/XX: selecionar loja e setar CEP se modal aparecer."""
    accept_cookies(page)
    # botões típicos de “Usar/Selecionar esta loja”
    for pat in ["Selecionar esta loja", "Usar esta loja", "Escolher esta loja", "Usar loja", "Definir loja", "Confirmar loja"]:
        if click_if_visible(page, role="button", name_regex=pat): break

    # abrir modal de endereço/CEP, se existir
    for pat in ["Endereço", "CEP", "Entrega", "Retirada", "Definir endereço", "Alterar endereço"]:
        if click_if_visible(page, role="button", name_regex=pat): break

    # preencher CEP
    if cep:
        try:
            page.get_by_placeholder(re.compile("CEP", re.I)).fill(cep)
            click_if_visible(page, role="button", name_regex="Buscar|Confirmar|Aplicar|Usar|OK|Continuar|Salvar")
        except Exception:
            try:
                page.locator("input[type=tel]").first.fill(cep)
                click_if_visible(page, role="button", name_regex="Buscar|Confirmar|Aplicar|Usar|OK|Continuar|Salvar")
            except Exception:
                pass

def go_to_list_page(page, host):
    """Se estiver em /loja/XX, pula para a página de ofertas correta por domínio."""
    pu = urlparse(page.url)
    if re.search(r"/loja/\d+", pu.path or ""):
        if "centerbox" in host:
            page.goto(f"{host}/clube", wait_until="domcontentloaded"); page.wait_for_timeout(1200)
        elif "saoluiz" in host or "mercadinho" in host:
            page.goto(f"{host}/ofertas", wait_until="domcontentloaded"); page.wait_for_timeout(1200)

    # se ainda não for listagem, tenta links de navegação
    if not re.search(r"oferta|clube|promo|categoria|horti|merce|busca", page.url, re.I):
        for pat in ["Ofertas", "Clube", "Promo"]:
            if click_if_visible(page, role="link", name_regex=pat): break

def wait_prices(page):
    """Espera preço aparecer pra garantir que carregou (VTEX)."""
    try:
        page.wait_for_selector('.vtex-product-price-1-x-sellingPriceValue, .best-price, meta[itemprop="price"]', timeout=12000)
    except Exception:
        pass

def scroll_and_load_more(page, rounds=22):
    for _ in range(rounds):
        page.mouse.wheel(0, 2400)
        page.wait_for_timeout(360)
    # “ver mais”
    for _ in range(6):
        clicked = click_if_visible(page, role="button", name_regex="mais|ver mais|carregar|see more|load more", timeout=900)
        if not clicked: break

# --------- JSON walker (VTEX/geral) ---------
PRICE_KEYS = {"price","salePrice","bestPrice","value","finalPrice","sellingPrice","Price","SellingPrice","unitPrice"}
NAME_KEYS  = {"name","productName","itemName","title","Name","Title","product_name","description"}

def walk_json_for_products(obj):
    found=[]
    try:
        if isinstance(obj, dict):
            # VTEX comum: commertialOffer.Price
            if "commertialOffer" in obj and isinstance(obj["commertialOffer"], dict):
                offer = obj["commertialOffer"]
                raw = offer.get("Price") or offer.get("ListPrice") or offer.get("PriceWithoutDiscount")
                if raw is not None:
                    name = obj.get("name") or obj.get("productName") or obj.get("itemName") or ""
                    if name:
                        try:
                            price = float(raw)
                            found.append({"name": str(name), "price": price})
                        except Exception:
                            pass
            has_name = any(k in obj for k in NAME_KEYS)
            has_price= any(k in obj for k in PRICE_KEYS)
            if has_name and has_price:
                name = next((str(obj[k]) for k in NAME_KEYS if k in obj and obj[k]), "")
                raw  = next((obj[k] for k in PRICE_KEYS if k in obj and obj[k] is not None), None)
                if isinstance(raw, str):
                    price = cleanup_money(raw)
                elif isinstance(raw, (int, float)):
                    price = float(raw)
                else:
                    price = None
                if name and price is not None:
                    found.append({"name": name, "price": price})
            for v in obj.values():
                found.extend(walk_json_for_products(v))
        elif isinstance(obj, list):
            for x in obj:
                found.extend(walk_json_for_products(x))
    except Exception:
        pass
    return found

# ------------------- Coleta com Playwright (HTML + XHR/JSON) -------------------
def fetch_with_browser(url: str, cep: str):
    ensure_chromium_installed()
    captured = []       # itens de JSON
    net_urls = []       # URLs de rede úteis (log)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage",
                      "--disable-gpu","--no-zygote","--single-process"],
            )
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(30000)

            # Ouve respostas (pra extrair JSON)
            def on_response(res):
                try:
                    u = res.url
                    if any(k in u for k in ["product","sku","search","shelf","pric","offer","items","simulation"]):
                        net_urls.append(u[:220])
                    ct = (res.headers or {}).get("content-type","").lower()
                    if "application/json" in ct or u.endswith(".json"):
                        data = None
                        try:
                            data = res.json()
                        except Exception:
                            try:
                                txt = res.text()
                                data = json.loads(txt)
                            except Exception:
                                pass
                        if data is not None:
                            items = walk_json_for_products(data)
                            if items:
                                captured.extend(items)
                except Exception:
                    pass
            page.on("response", on_response)

            # 1) Abre a URL original
            try:
                page.goto(url, wait_until="domcontentloaded")
            except PwTimeout:
                pass

            host = f"{urlparse(page.url).scheme}://{urlparse(page.url).netloc}"

            # 2) Aceita cookies + define loja + CEP
            accept_cookies(page)
            if re.search(r"/loja/\d+", urlparse(page.url).path or ""):
                set_store_and_cep(page, host, cep)

            # 3) Vai pra página de ofertas correta
            go_to_list_page(page, host)

            # 4) Espera preço aparecer e força carregamento
            wait_prices(page)
            scroll_and_load_more(page, rounds=22)

            # 5) HTML final
            html = page.content()

            context.close(); browser.close()
            return html, captured, net_urls

    except Exception:
        # fallback requests_html
        try:
            from requests_html import HTMLSession
            sess = HTMLSession()
            r = sess.get(url, timeout=35)
            r.html.render(timeout=70, sleep=6, scrolldown=16)
            return r.html.html, [], []
        except Exception:
            return "", [], []

# ------------------- Extratores -------------------
# Específicos
def extract_centerbox(html: str):
    items=[]
    soup = BeautifulSoup(html or "", "lxml")
    for card in soup.select('[data-testid="product-summary-container"], .vtex-product-summary-2-x-container, .product-card, .shelf-item'):
        name = None; price = None
        for sel in ['[data-testid="product-name"]','.vtex-product-summary-2-x-productBrand','.product-title, .name, h3, h2, [itemprop="name"]']:
            el = card.select_one(sel)
            if el and el.get_text(strip=True):
                name = el.get_text(" ", strip=True); break
        if not name:
            name = card.get_text(" ", strip=True)[:200]
        for sel in ['.vtex-product-price-1-x-sellingPriceValue','.vtex-product-price-1-x-currencyInteger','.best-price, .price, [data-price]']:
            el = card.select_one(sel)
            if el:
                price = cleanup_money(el.get_text(" ", strip=True))
                if price is not None: break
        if price is None:
            meta = card.select_one('meta[itemprop="price"]')
            if meta and meta.has_attr("content"):
                try: price = float(str(meta["content"]).replace(",", ".")); 
                except Exception: pass
        if name and price is not None:
            items.append({"name": name[:200], "price": price})
    # dedup
    out, seen = [], set()
    for it in items:
        k=(it["name"], round(float(it["price"]),2))
        if k not in seen: out.append(it); seen.add(k)
    return out

def extract_saoluiz(html: str):
    items=[]
    soup = BeautifulSoup(html or "", "lxml")
    for card in soup.select('.vtex-product-summary-2-x-container, .product-card, .shelf-item, [data-testid="product-summary-container"]'):
        name = None; price = None
        for sel in ['.vtex-product-summary-2-x-productBrand','[data-testid="product-name"]','.product-title, .name, h3, h2, [itemprop="name"]']:
            el = card.select_one(sel)
            if el and el.get_text(strip=True):
                name = el.get_text(" ", strip=True); break
        if not name:
            name = card.get_text(" ", strip=True)[:200]
        for sel in ['.vtex-product-price-1-x-sellingPriceValue','.best-price, .price, [data-price]','.vtex-product-price-1-x-currencyInteger']:
            el = card.select_one(sel)
            if el:
                price = cleanup_money(el.get_text(" ", strip=True))
                if price is not None: break
        if price is None:
            meta = card.select_one('meta[itemprop="price"]')
            if meta and meta.has_attr("content"):
                try: price = float(str(meta["content"]).replace(",", ".")); 
                except Exception: pass
        if name and price is not None:
            items.append({"name": name[:200], "price": price})
    out, seen = [], set()
    for it in items:
        k=(it["name"], round(float(it["price"]),2))
        if k not in seen: out.append(it); seen.add(k)
    return out

# Genérico
GEN_PRICE_SEL = (
    '.vtex-product-price-1-x-sellingPriceValue, .best-price, .price, '
    '[class*="price"], [data-price], meta[itemprop="price"]'
)
GEN_NAME_SEL = (
    '.vtex-product-summary-2-x-productBrand, .product-title, .name, '
    'h3, h2, [itemprop="name"], [data-name]'
)
def extract_generic(html: str):
    items=[]
    soup = BeautifulSoup(html or "", "lxml")
    cards = soup.select(
        '.vtex-product-summary-2-x-container, .product-card, .shelf-item, '
        '.product, .card, [itemtype*="Product"], .item, [data-testid="product-summary-container"]'
    )
    for card in cards[:2000]:
        name = None; price = None
        eln = card.select_one(GEN_NAME_SEL)
        name = eln.get_text(" ", strip=True) if (eln and eln.get_text(strip=True)) else card.get_text(" ", strip=True)[:200]
        meta = card.select_one('meta[itemprop="price"]')
        if meta and meta.has_attr("content"):
            try: price = float(str(meta["content"]).replace(",", ".")) 
            except Exception: price = None
        if price is None:
            elp = card.select_one(GEN_PRICE_SEL)
            if elp:
                price = cleanup_money(elp.get_text(" ", strip=True))
        if name and price is not None:
            items.append({"name": name[:200], "price": price})
    out, seen = [], set()
    for it in items:
        k=(it["name"], round(float(it["price"]),2))
        if k not in seen: out.append(it); seen.add(k)
    return out

# ------------------- Comparação -------------------
def map_prices(items):
    mapped={}
    for it in items:
        sec,key = match_canonical(it["name"])
        if not sec: 
            continue
        price = it["price"]
        cur = mapped.get((sec,key))
        if cur is None or price < cur:
            mapped[(sec,key)] = price
    return mapped

if go:
    if not url1 or not url2:
        st.error("Manda os dois links 😉"); st.stop()

    with st.spinner("Abrindo páginas, definindo loja/CEP, capturando HTML + XHR/JSON…"):
        html1, net_items1, net_urls1 = fetch_with_browser(url1, cep)
        html2, net_items2, net_urls2 = fetch_with_browser(url2, cep)

    host1 = urlparse(url1).netloc
    host2 = urlparse(url2).netloc

    if "centerbox" in host1:
        items1 = extract_centerbox(html1)
    elif "saoluiz" in host1 or "mercadinho" in host1:
        items1 = extract_saoluiz(html1)
    else:
        items1 = extract_generic(html1)

    if "centerbox" in host2:
        items2 = extract_centerbox(html2)
    elif "saoluiz" in host2 or "mercadinho" in host2:
        items2 = extract_saoluiz(html2)
    else:
        items2 = extract_generic(html2)

    items1 += net_items1
    items2 += net_items2

    # dedup geral
    def dedup_items(items):
        out, seen = [], set()
        for it in items:
            if not it.get("name") or not isinstance(it.get("price"), (int,float)): 
                continue
            k = (it["name"], round(float(it["price"]), 2))
            if k not in seen:
                out.append({"name": it["name"], "price": float(it["price"])})
                seen.add(k)
        return out
    items1 = dedup_items(items1)
    items2 = dedup_items(items2)

    if not items1 and not items2:
        st.error("Ainda não consegui ler preços (pode ser login rígido). Tenta informar um CEP. Se persistir, me manda prints que eu cravo o seletor exato.")
        with st.expander("🔧 Debug de rede capturado"):
            st.write("URLs suspeitas - Supermercado #1:", net_urls1[:40])
            st.write("URLs suspeitas - Supermercado #2:", net_urls2[:40])
        st.stop()

    name1 = host1.replace("www.","")
    name2 = host2.replace("www.","")

    map1, map2 = map_prices(items1), map_prices(items2)

    total1=total2=0.0; sum1=sum2=0.0; pairs=0

    st.markdown("---")
    for section, products in CATALOG.items():
        rows=[]
        for p in products:
            key=p["key"]
            v1=map1.get((section,key))
            v2=map2.get((section,key))
            rows.append({
                "Produto": key,
                name1: (f"R$ {v1:.2f}" if isinstance(v1,(int,float)) else "—"),
                name2: (f"R$ {v2:.2f}" if isinstance(v2,(int,float)) else "—"),
            })
            if isinstance(v1,(int,float)) and isinstance(v2,(int,float)):
                if v1<v2: total1+=1
                elif v2<v1: total2+=1
                else: total1+=0.5; total2+=0.5
                sum1+=v1; sum2+=v2; pairs+=1
        st.subheader(section)
        st.dataframe(pd.DataFrame(rows), use_container_width=True)

    st.markdown("---")
    st.markdown("### 🏁 Resultado")
    def fmt(x): return f"{x:.1f}".replace(".0","")
    msg = f"**{name1}** {fmt(total1)} × {fmt(total2)} **{name2}** (critério: mais itens com menor preço)"
    if total1>total2:
        winner = name1
    elif total2>total1:
        winner = name2
    else:
        if pairs>0:
            if sum1<sum2: winner=name1; msg+=f". Desempate pela menor soma (R$ {sum1:.2f} vs R$ {sum2:.2f})."
            elif sum2<sum1: winner=name2; msg+=f". Desempate pela menor soma (R$ {sum2:.2f} vs R$ {sum1:.2f})."
            else: winner=f"{name1} / {name2}"; msg+=". Empate após soma."
        else:
            winner=f"{name1} / {name2}"; msg+=". Empate técnico (pouco item em comum)."

    st.success(f"🏆 **Vencedor:** {winner}\n\n{msg}")

    with st.expander("🔎 Itens brutos + Rede (debug)"):
        st.write(name1, len(items1)); st.write(items1[:40])
        st.write(name2, len(items2)); st.write(items2[:40])
        st.write("URLs de rede #1:", net_urls1[:40])
        st.write("URLs de rede #2:", net_urls2[:40])

st.caption("Dica: cole /loja/XX ou diretamente /clube (Centerbox) /ofertas (São Luiz). Informe um CEP se o site pedir loja.")
