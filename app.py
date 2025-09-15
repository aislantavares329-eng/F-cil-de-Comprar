# app.py — Comparador “cole os links e pronto” (2 supermercados)
# - Aceita URLs /loja/XX (salta automaticamente para /clube ou /ofertas).
# - CEP opcional para destravar preço quando houver modal de loja/endereço.
# - Playwright com auto-instalação do Chromium + flags anti-sandbox/dev-shm.
# - Captura HTML e também XHR/JSON (muito resiliente a mudanças de CSS).
# - Fallback: requests_html (render JS leve) se o browser falhar.

import os, re, unicodedata, json, subprocess
from urllib.parse import urlparse

import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ------------------- UI -------------------
st.set_page_config(page_title="Comparador de Supermercados (link direto)", layout="wide")
st.title("🛒 Comparador de Supermercados — cole os links e pronto")

c1, c2 = st.columns(2)
with c1:
    url1 = st.text_input("🔗 URL do Supermercado #1", placeholder="Ex.: https://loja.centerbox.com.br/loja/58 ou página de ofertas…")
with c2:
    url2 = st.text_input("🔗 URL do Supermercado #2", placeholder="Ex.: https://mercadinhossaoluiz.com.br/loja/355 ou página de ofertas…")

cep = st.text_input("📍 CEP (opcional — ajuda quando o site pede loja)", placeholder="Ex.: 60000-000")
go = st.button("Comparar")

# ------------------- Helpers (texto/tamanho/marca/dinheiro) -------------------
def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s).lower())
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace() or ch in "xµ/.-")
    return " ".join(s.split())

def cleanup_money(txt: str):
    if not isinstance(txt, str): return None
    m = re.search(r"(\d{1,3}(?:\.\d{3})*|\d+)(?:,(\d{2}))", txt)
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

# ------------------- Playwright: garantir Chromium + abrir URL -------------------
def ensure_chromium_installed():
    """Garante o browser do Playwright. Idempotente/silencioso."""
    try:
        subprocess.run(
            ["python", "-m", "playwright", "install", "chromium", "--with-deps"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        try:
            subprocess.run(
                ["python", "-m", "playwright", "install", "chromium"],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception:
            pass

def try_select_store(page, cep: str):
    """Se aparecer modal de loja/CEP, tenta preencher e confirmar. Ignora erros."""
    if not cep: return
    try:
        texts = ["Selecione", "Entrega", "Retirada", "loja", "Definir", "Selecionar", "Confirmar", "Endereço"]
        for t in texts:
            loc = page.get_by_role("button", name=re.compile(t, re.I)).first
            if loc and loc.is_visible():
                loc.click()
                page.wait_for_timeout(800)
                break
    except Exception:
        pass
    try:
        page.get_by_placeholder(re.compile("CEP", re.I)).fill(cep)
    except Exception:
        try:
            page.locator("input[type=tel]").first.fill(cep)
        except Exception:
            pass
    try:
        page.get_by_role("button", name=re.compile("Buscar|Confirmar|Aplicar|Usar|OK|Continuar|Salvar", re.I)).first.click()
        page.wait_for_timeout(1200)
    except Exception:
        pass
    try:
        page.get_by_role("button", name=re.compile("Selecionar|Escolher|Retirar|Usar esta loja", re.I)).first.click()
        page.wait_for_timeout(1200)
    except Exception:
        pass

def scroll_load(page, rounds=18):
    for _ in range(rounds):
        page.mouse.wheel(0, 2000)
        page.wait_for_timeout(450)

# --------- JSON walker (acha produtos/preços em qualquer estrutura) ---------
PRICE_KEYS = {"price","salePrice","bestPrice","value","finalPrice","sellingPrice","Price","SellingPrice","unitPrice"}
NAME_KEYS  = {"name","productName","itemName","title","Name","Title","product_name","description"}

def walk_json_for_products(obj):
    found=[]
    try:
        if isinstance(obj, dict):
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
    """
    Abre a URL no Chromium headless; tenta CEP se surgir modal;
    se for /loja/XX, desvia para /clube (Centerbox) ou /ofertas (São Luiz);
    rola a página; devolve (html_final, itens_json_capturados).
    """
    ensure_chromium_installed()
    captured = []  # itens vindos de JSON da rede

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

            # Listener de respostas XHR/JSON
            def on_response(res):
                try:
                    ct = (res.headers or {}).get("content-type","").lower()
                    if "application/json" in ct or res.url.endswith(".json"):
                        try:
                            data = res.json()
                        except Exception:
                            try:
                                txt = res.text()
                                data = json.loads(txt)
                            except Exception:
                                data = None
                        if data is not None:
                            items = walk_json_for_products(data)
                            if items:
                                captured.extend(items)
                except Exception:
                    pass
            page.on("response", on_response)

            # 1) abre a URL original
            try:
                page.goto(url, wait_until="domcontentloaded")
            except PwTimeout:
                pass

            # 2) tenta destravar por CEP, se houver modal
            try_select_store(page, cep)

            # 3) se for /loja/XX, desvia para ofertas/clube
            from urllib.parse import urlparse as _u
            pu = _u(page.url)
            host = f"{pu.scheme}://{pu.netloc}"
            path = pu.path or "/"
            if re.search(r"/loja/\d+", path):
                if "centerbox" in host:
                    page.goto(f"{host}/clube", wait_until="domcontentloaded")
                elif "saoluiz" in host or "mercadinho" in host:
                    page.goto(f"{host}/ofertas", wait_until="domcontentloaded")
                page.wait_for_timeout(1200)

            # 4) se ainda não for listagem, tenta link “Ofertas/Clube/Promo”
            try:
                if not re.search(r"oferta|clube|promo|categoria|horti|merce|busca", page.url, re.I):
                    link = page.get_by_role("link", name=re.compile("Ofertas|Clube|Promo", re.I)).first
                    if link and link.is_visible():
                        link.click()
                        page.wait_for_timeout(1500)
            except Exception:
                pass

            # 5) scroll pra carregar os cards
            scroll_load(page, rounds=18)

            html = page.content()
            context.close(); browser.close()
            return html, captured

    except Exception:
        # fallback com requests_html
        try:
            from requests_html import HTMLSession
            sess = HTMLSession()
            r = sess.get(url, timeout=30)
            r.html.render(timeout=60, sleep=6, scrolldown=14)
            return r.html.html, []
        except Exception:
            return "", []

# ------------------- Extração dos cards HTML -------------------
PRICE_SEL = (
    '.vtex-product-price-1-x-sellingPriceValue, '
    '.best-price, .price, [class*="price"], [data-price]'
)
NAME_SEL = (
    '.vtex-product-summary-2-x-productBrand, '
    '.product-title, .name, h3, h2, [itemprop="name"], [data-name]'
)

def extract_cards_from_html(html: str):
    items=[]
    soup=BeautifulSoup(html or "", "lxml")
    cards = soup.select(
        '.vtex-product-summary-2-x-container, .product-card, .shelf-item, '
        '.product, .card, [itemtype*="Product"], .item'
    )
    for c in cards[:1500]:
        # preço
        price_txt = None
        for el in c.select(PRICE_SEL):
            t = el.get_text(" ", strip=True)
            if t: price_txt = t; break
        if not price_txt:
            price_txt = c.get_text(" ", strip=True)
        price = cleanup_money(price_txt)
        if price is None: 
            continue
        # nome
        name_txt = None
        for el in c.select(NAME_SEL):
            t = el.get_text(" ", strip=True)
            if t and len(t)>2:
                name_txt = t; break
        if not name_txt:
            name_txt = c.get_text(" ", strip=True)
        items.append({"name": name_txt[:200], "price": price})
    # dedup
    dedup, seen = [], set()
    for it in items:
        k=(it["name"], it["price"])
        if k not in seen:
            dedup.append(it); seen.add(k)
    return dedup

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

    with st.spinner("Abrindo as páginas e coletando os preços (HTML + XHR)…"):
        html1, net1 = fetch_with_browser(url1, cep)
        html2, net2 = fetch_with_browser(url2, cep)

    items1 = extract_cards_from_html(html1) + net1
    items2 = extract_cards_from_html(html2) + net2

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
        st.error("Não consegui ler preços nessas URLs (pode exigir login rígido ou o HTML/JSON mudou). Me envie os links exatos que eu ajusto os seletores.")
        st.stop()

    name1 = urlparse(url1).netloc.replace("www.","")
    name2 = urlparse(url2).netloc.replace("www.","")

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

    with st.expander("🔎 Itens brutos (debug)"):
        st.write(name1, len(items1)); st.write(items1[:30])
        st.write(name2, len(items2)); st.write(items2[:30])

st.caption("Cole links de **listagem** ou mesmo **/loja/XX** — eu pulo para /clube (Centerbox) ou /ofertas (São Luiz). Se pedir loja, informe um **CEP** (opcional).")
