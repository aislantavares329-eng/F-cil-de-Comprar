# app.py — Comparador por SEÇÃO (2 supermercados) com renderização JS opcional
# Mantém o critério: vencedor = quem tem MAIS itens com MENOR preço (empate = 0,5). Desempate: menor soma.

import re, json, unicodedata, time
from urllib.parse import urlparse
import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup

# structured data
try:
    import extruct
    from w3lib.html import get_base_url
    EXSTRUCT = True
except Exception:
    EXSTRUCT = False

# JS render (opcional)
try:
    from requests_html import HTMLSession
    RHTML_OK = True
except Exception:
    RHTML_OK = False

st.set_page_config(page_title="Comparador por SEÇÃO (2 supermercados)", layout="wide")
st.title("🛒 Comparador por SEÇÃO (2 supermercados) — com renderização JS")

# ========================= Utils =========================
HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

def domain_of(url: str) -> str:
    try: return urlparse(url).netloc.replace("www.", "")
    except: return "desconhecido"

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
    except: return None

def fetch_html(url: str, cookie_str: str = "", render_js: bool = False, wait: float = 2.5):
    """
    Tenta pegar HTML.
    - Se render_js=True (e requests_html disponível): abre como navegador headless e executa JS.
    - cookie_str: opcional "cookie1=a; cookie2=b"
    """
    headers = HEADERS_BASE.copy()
    if cookie_str.strip():
        headers["Cookie"] = cookie_str.strip()

    # 1) modo rápido
    try:
        r = requests.get(url, headers=headers, timeout=25)
        r.raise_for_status()
        html_fast, final = r.text, r.url
    except Exception:
        html_fast, final = "", url

    if not render_js or not RHTML_OK:
        return html_fast, final, False  # sem JS

    # 2) modo renderizado (lento)
    try:
        sess = HTMLSession()
        r = sess.get(url, headers=headers, timeout=30)
        # render: carrega Chromium, executa JS; sleep = dar tempo p/ cards de produto aparecerem
        r.html.render(timeout=40, sleep=wait)
        html_js = r.html.html
        return html_js or html_fast, r.url, True
    except Exception:
        return html_fast, final, False

def get_site_title(html: str, url: str):
    soup = BeautifulSoup(html or "", "lxml")
    t = soup.find("title")
    if t and t.text.strip(): return t.text.strip()
    return domain_of(url)

# ========================= Catálogo SEÇÕES (igual ao anterior) =========================
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
    if size_g is not None: return approx(parsed["effective_g"], size_g, tol_g or 0)
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

# ========================= Extração =========================
def extract_structured(html: str, url: str):
    """pega de JSON-LD/microdata e de scripts __NEXT_DATA__/__NUXT__/APOLLO"""
    out = []

    # extruct
    if EXSTRUCT and html:
        try:
            data = extruct.extract(html, base_url=get_base_url(html, url), syntaxes=['json-ld','microdata'])
            for blk in data.get('json-ld', []) or []:
                nodes = blk if isinstance(blk, list) else [blk]
                for node in nodes:
                    out.extend(_from_struct_node(node))
            for m in (data.get('microdata') or []):
                node = m.get('properties') or {}
                node["@type"] = m.get('type') or node.get("@type")
                out.extend(_from_struct_node(node))
        except Exception:
            pass

    # blobs em <script>
    soup = BeautifulSoup(html or "", "lxml")
    for sc in soup.find_all("script"):
        txt = sc.string or sc.text or ""
        if not txt or len(txt) < 100: continue
        if any(k in txt for k in ["__NEXT_DATA__","__NUXT__","__APOLLO_STATE__","window.__INITIAL_STATE__"]):
            # tenta achar objetos JSON grandes
            for m in re.finditer(r"(\{.*\})", txt, re.S):
                blob = m.group(1)
                try:
                    data = json.loads(blob)
                except Exception:
                    continue
                out.extend(_walk_json_for_products(data))
    # dedup
    dedup, seen = [], set()
    for it in out:
        if it.get("name") and isinstance(it.get("price"), (int,float)):
            k = (it["name"], it["price"])
            if k not in seen:
                dedup.append(it); seen.add(k)
    return dedup

def _from_struct_node(node):
    res=[]
    if not isinstance(node, dict): return res
    t=node.get("@type"); 
    if isinstance(t,list): t=" ".join(t)
    if t and "Product" in str(t):
        name=node.get("name") or node.get("description") or ""
        offers=node.get("offers")
        if isinstance(offers, dict):
            p=_price_from_offer(offers); 
            if p is not None: res.append({"name":name, "price":p})
        elif isinstance(offers, list):
            for off in offers:
                p=_price_from_offer(off); 
                if p is not None: res.append({"name":name, "price":p})
    if (t and "Offer" in str(t)) or node.get("price"):
        p=_price_from_offer(node)
        if p is not None: res.append({"name":node.get("name") or "", "price":p})
    return res

def _walk_json_for_products(obj):
    """percorre JSON procurando campos de produto/preço comuns"""
    found=[]
    try:
        if isinstance(obj, dict):
            # nomes e preços frequentes
            name_keys=["name","productName","itemName","title"]
            price_keys=["price","salePrice","bestPrice","value","finalPrice","sellingPrice"]
            if any(k in obj for k in name_keys) and any(k in obj for k in price_keys):
                name=str(next((obj[k] for k in name_keys if k in obj and obj[k]), ""))
                price_raw=str(next((obj[k] for k in price_keys if k in obj and obj[k] is not None), ""))
                price=cleanup_money(price_raw) if isinstance(price_raw,str) else (float(price_raw) if isinstance(price_raw,(int,float)) else None)
                if name and price is not None:
                    found.append({"name":name, "price":price})
            # andar recursivo
            for v in obj.values():
                found.extend(_walk_json_for_products(v))
        elif isinstance(obj, list):
            for x in obj: found.extend(_walk_json_for_products(x))
    except Exception:
        pass
    return found

def _price_from_offer(offer):
    if not isinstance(offer, dict): return None
    if offer.get("price"): 
        return cleanup_money(str(offer["price"]))
    ps = offer.get("priceSpecification") or {}
    if isinstance(ps, dict) and ps.get("price"):
        return cleanup_money(str(ps["price"]))
    return None

def extract_from_cards(html: str, domain_hint: str):
    """CSS ganchos por domínio + heurística genérica"""
    items=[]
    soup=BeautifulSoup(html or "", "lxml")

    # Centerbox (loja.centerbox.com.br) – classes observadas variam; tentamos alguns padrões
    if "centerbox" in domain_hint:
        cards = soup.select('.product, .product-card, .vtex-product-summary-2-x-container, .card')
    # São Luiz (mercadinhossaoluiz / saoluiz) – também VTEX/Próprio
    elif "saoluiz" in domain_hint or "mercadinho" in domain_hint:
        cards = soup.select('.product, .product-card, .vtex-product-summary-2-x-container, .shelf-item, .card')
    else:
        cards = soup.select('[itemtype*="Product"], [itemscope][itemtype*="Product"], .product, .produto, .product-card, .card, .item')

    for c in cards[:600]:
        txt = c.get_text(" ", strip=True)
        price = cleanup_money(txt)
        if price is None: continue
        name_el = c.select_one('[itemprop="name"], .product-title, .vtex-product-summary-2-x-productBrand, .name, h3, h2')
        name = (name_el.get_text(" ", strip=True) if name_el else txt)[:160]
        items.append({"name":name, "price":price})

    # fallback: pegar blocos com "R$" e um título vizinho
    if not items:
        for blk in soup.find_all(text=re.compile(r"R\$\s*\d")):
            cont = blk.parent
            name = None
            # sobe um pouco procurando títulos
            for anc in cont.parents:
                title = anc.find(["h2","h3"])
                if title and len(title.get_text(strip=True))>2:
                    name = title.get_text(" ", strip=True); break
            price = cleanup_money(blk)
            if name and price is not None:
                items.append({"name":name[:160], "price":price})

    dedup, seen=[], set()
    for it in items:
        k=(it["name"], it["price"])
        if k not in seen:
            dedup.append(it); seen.add(k)
    return dedup

def extract_products_any(html: str, url: str, domain_hint: str):
    # 1) structured/script blobs
    items = extract_structured(html, url)
    # 2) cards
    if not items:
        items = extract_from_cards(html, domain_hint)
    return items

# ========================= UI =========================
c1, c2 = st.columns(2)
with c1:
    url1 = st.text_input("🔗 URL do Supermercado #1", placeholder="https://...")
    cookie1 = st.text_input("🍪 Cookies (opcional #1)", placeholder="cookie1=...; cookie2=...")
with c2:
    url2 = st.text_input("🔗 URL do Supermercado #2", placeholder="https://...")
    cookie2 = st.text_input("🍪 Cookies (opcional #2)", placeholder="cookie1=...; cookie2=...")

render_js = st.toggle("Renderizar JavaScript (mais lento, precisa Chromium)", value=True,
                      help="Ative se a página não mostra preços sem JS. Se der erro, instale requests_html/pyppeteer no requirements.")

if st.button("Comparar"):
    if not url1 or not url2:
        st.error("Manda os dois links 😉"); st.stop()

    # fetch
    with st.spinner("Abrindo páginas..."):
        html1, final1, used_js1 = fetch_html(url1, cookie1, render_js=render_js)
        html2, final2, used_js2 = fetch_html(url2, cookie2, render_js=render_js)

    if not html1: st.error(f"Não consegui abrir: {url1}"); st.stop()
    if not html2: st.error(f"Não consegui abrir: {url2}"); st.stop()

    dom1, dom2 = domain_of(final1), domain_of(final2)
    name1, name2 = get_site_title(html1, final1), get_site_title(html2, final2)

    # extrair
    items1 = extract_products_any(html1, final1, dom1)
    items2 = extract_products_any(html2, final2, dom2)

    if not items1 and render_js and not used_js1:
        st.warning(f"{dom1}: sem dados. Tente com 'Renderizar JavaScript' ligado.")
    if not items2 and render_js and not used_js2:
        st.warning(f"{dom2}: sem dados. Tente com 'Renderizar JavaScript' ligado.")

    # mapear pro catálogo (pega menor preço por item)
    def map_prices(items):
        mapped={}
        for it in items:
            sec,key = match_canonical(it["name"])
            if not sec: continue
            price = it["price"]
            cur = mapped.get((sec,key))
            if cur is None or price < cur:
                mapped[(sec,key)] = price
        return mapped

    map1, map2 = map_prices(items1), map_prices(items2)

    # ---- mostrar por seção e decidir vencedor (igual antes)
    total1=total2=0.0; sum1=sum2=0.0; pairs=0

    st.markdown("---")
    for section, products in CATALOG.items():
        rows=[]
        for p in products:
            key=p["key"]; v1=map1.get((section,key)); v2=map2.get((section,key))
            rows.append({"Produto":key, name1:(f"R$ {v1:.2f}" if isinstance(v1,(int,float)) else "—"),
                                   name2:(f"R$ {v2:.2f}" if isinstance(v2,(int,float)) else "—")})
            if isinstance(v1,(int,float)) and isinstance(v2,(int,float)):
                if v1<v2: total1+=1
                elif v2<v1: total2+=1
                else: total1+=0.5; total2+=0.5
                sum1+=v1; sum2+=v2; pairs+=1
        st.subheader(section); st.dataframe(pd.DataFrame(rows), use_container_width=True)

    st.markdown("---"); st.markdown("### 🏁 Resultado")
    def fmt(x): return f"{x:.1f}".replace(".0","")
    msg = f"**{name1}** {fmt(total1)} × {fmt(total2)} **{name2}** (critério: mais itens com menor preço)"
    if total1>total2: winner=name1
    elif total2>total1: winner=name2
    else:
        if pairs>0:
            if sum1<sum2: winner=name1; msg += f". Desempate pela menor soma (R$ {sum1:.2f} vs R$ {sum2:.2f})."
            elif sum2<sum1: winner=name2; msg += f". Desempate pela menor soma (R$ {sum2:.2f} vs R$ {sum1:.2f})."
            else: winner=f"{name1} / {name2}"; msg+=". Empate após soma."
        else:
            winner=f"{name1} / {name2}"; msg+=". Empate técnico (pouca interseção)."

    st.success(f"🏆 **Vencedor:** {winner}\n\n{msg}")

    # debug opcional
    with st.expander("🔎 Itens brutos capturados (debug)"):
        st.write(dom1, len(items1)); st.write(items1[:20])
        st.write(dom2, len(items2)); st.write(items2[:20])
