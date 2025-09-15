# app.py — Comparador simples (2 supermercados) com Playwright (sem cookies do usuário)
# Fluxo: escolhe loja pelo CEP, navega para "Ofertas/Clube", coleta preços, compara por SEÇÕES.

import re, json, time, unicodedata
from urllib.parse import urlparse
import streamlit as st
import pandas as pd
from bs4 import BeautifulSoup

# ---- Playwright (automação do navegador) ----
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ---- Fallback leve (caso automação falhe totalmente) ----
try:
    from requests_html import HTMLSession
    RHTML_OK = True
except Exception:
    RHTML_OK = False

# -------------------- UI --------------------
st.set_page_config(page_title="Comparador de Supermercados (simples)", layout="wide")
st.title("🛒 Comparador de Supermercados — modo simples (CEP)")

COLS = st.columns(2)
with COLS[0]:
    store_a = st.selectbox("Supermercado #1", ["Centerbox", "São Luiz"], index=0)
with COLS[1]:
    store_b = st.selectbox("Supermercado #2", ["Centerbox", "São Luiz"], index=1)

cep = st.text_input("CEP para selecionar a loja automaticamente", placeholder="Ex.: 60000-000")
categoria = st.selectbox("Categoria-alvo", ["Ofertas/Clube (padrão)"], index=0)

if st.button("Comparar"):
    if not cep or not re.search(r"\d{5}-?\d{3}", cep):
        st.error("Manda um CEP válido, por favor 😉")
        st.stop()

    # --------------- Catálogo / Normalização (igual antes) ---------------
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

    # --------------- Fluxos automáticos por domínio ---------------
    def run_centerbox(pw, cep: str) -> str:
        """Abre Centerbox, escolhe loja pelo CEP e retorna HTML de /clube (ofertas)."""
        page = pw.new_page()
        page.set_default_timeout(20000)
        page.goto("https://loja.centerbox.com.br/", wait_until="load")
        # Seleciona método/loja
        try:
            # opções de texto variam… tentamos algumas
            page.get_by_text("Selecione um método de entrega", exact=False).first.wait_for()
        except PwTimeout:
            pass

        # Abre seletor de loja/CEP (botão no topo)
        try:
            page.get_by_role("button", name=re.compile("Selecione|Retirada|Entrega", re.I)).click()
        except Exception:
            pass

        # Campo de CEP
        try:
            page.get_by_placeholder(re.compile("CEP", re.I)).fill(cep)
        except Exception:
            try:
                page.locator("input[type='tel']").first.fill(cep)
            except Exception:
                pass

        # Botão buscar loja
        try:
            page.get_by_role("button", name=re.compile("Buscar|Pesquisar|Confirmar", re.I)).click()
            page.wait_for_timeout(1500)
        except Exception:
            pass

        # Escolhe primeira loja da lista (quando aparece)
        try:
            page.get_by_role("button", name=re.compile("Selecionar|Escolher|Retirar", re.I)).first.click()
            page.wait_for_timeout(1200)
        except Exception:
            pass

        # Vai para ofertas (Clube)
        page.goto("https://loja.centerbox.com.br/clube", wait_until="domcontentloaded")

        # scroll para carregar vitrine
        for _ in range(12):
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(500)

        html = page.content()
        page.close()
        return html

    def run_saoluiz(pw, cep: str) -> str:
        """Abre São Luiz, escolhe loja pelo CEP e retorna HTML de /ofertas."""
        page = pw.new_page()
        page.set_default_timeout(20000)
        page.goto("https://mercadinhossaoluiz.com.br/", wait_until="load")

        # Abre seletor
        try:
            page.get_by_role("button", name=re.compile("Selecione|Entrega|Retirada|Minha", re.I)).first.click()
        except Exception:
            pass

        # Tenta preencher CEP
        try:
            page.get_by_placeholder(re.compile("CEP", re.I)).fill(cep)
        except Exception:
            try:
                page.locator("input[type='tel']").first.fill(cep)
            except Exception:
                pass

        # Buscar/confirmar loja
        try:
            page.get_by_role("button", name=re.compile("Buscar|Confirmar|Usar", re.I)).click()
            page.wait_for_timeout(1500)
        except Exception:
            pass

        # Selecionar primeira loja
        try:
            page.get_by_role("button", name=re.compile("Selecionar|Escolher|Retirar", re.I)).first.click()
            page.wait_for_timeout(1200)
        except Exception:
            pass

        # Ir para ofertas
        page.goto("https://mercadinhossaoluiz.com.br/ofertas", wait_until="domcontentloaded")

        # scroll
        for _ in range(12):
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(500)

        html = page.content()
        page.close()
        return html

    def scrape_with_playwright(super_name: str, cep: str) -> str:
        """Retorna HTML da lista de ofertas para o supermercado informado."""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            try:
                if super_name == "Centerbox":
                    html = run_centerbox(context, cep)
                else:
                    html = run_saoluiz(context, cep)
            finally:
                context.close()
                browser.close()
        return html

    # --------------- Parse da listagem (pega nome e preço) ---------------
    def extract_from_list_html(html: str, domain_hint: str):
        items = []
        soup = BeautifulSoup(html or "", "lxml")

        if "centerbox" in domain_hint:
            cards = soup.select(
                '.vtex-product-summary-2-x-container, .product-card, .shelf-item, .product, .card'
            )
            price_sel = (
                '.vtex-product-price-1-x-sellingPriceValue, .best-price, .price, [class*="price"]'
            )
            name_sel = (
                '.vtex-product-summary-2-x-productBrand, .product-title, .name, h3, h2, [itemprop="name"]'
            )
        elif "saoluiz" in domain_hint or "mercadinho" in domain_hint:
            cards = soup.select(
                '.vtex-product-summary-2-x-container, .shelf-item, .product-card, .card, .product'
            )
            price_sel = (
                '.vtex-product-price-1-x-sellingPriceValue, .best-price, .price, [class*="price"]'
            )
            name_sel = (
                '.vtex-product-summary-2-x-productBrand, .product-title, .name, h3, h2, [itemprop="name"]'
            )
        else:
            cards = soup.select('[itemtype*="Product"], .product, .produto, .product-card, .card, .item')
            price_sel = '.best-price, .price, [class*="price"]'
            name_sel = '.product-title, .name, h3, h2, [itemprop="name"]'

        for c in cards[:1000]:
            # preço
            price_txt = None
            for el in c.select(price_sel):
                t = el.get_text(" ", strip=True)
                if t: price_txt = t; break
            if not price_txt:
                price_txt = c.get_text(" ", strip=True)
            price = cleanup_money(price_txt)
            if price is None:
                continue
            # nome
            name_txt = None
            for el in c.select(name_sel):
                t = el.get_text(" ", strip=True)
                if t and len(t) > 2:
                    name_txt = t; break
            if not name_txt:
                name_txt = c.get_text(" ", strip=True)
            items.append({"name": name_txt[:200], "price": price})

        # dedup
        dedup, seen = [], set()
        for it in items:
            k = (it["name"], it["price"])
            if k not in seen:
                dedup.append(it); seen.add(k)
        return dedup

    # --------------- Rodar coleta A/B ---------------
    with st.spinner("Coletando preços automaticamente (pode levar alguns segundos)…"):
        html_a = scrape_with_playwright(store_a, cep)
        html_b = scrape_with_playwright(store_b, cep)

    dom_a = "centerbox" if store_a == "Centerbox" else "mercadinhossaoluiz"
    dom_b = "centerbox" if store_b == "Centerbox" else "mercadinhossaoluiz"

    items_a = extract_from_list_html(html_a, dom_a)
    items_b = extract_from_list_html(html_b, dom_b)

    # Fallback super leve: se algum vier vazio, tenta requests_html render (sem cookies)
    if not items_a and RHTML_OK:
        try:
            sess = HTMLSession()
            url_fallback = "https://loja.centerbox.com.br/clube" if store_a=="Centerbox" \
                           else "https://mercadinhossaoluiz.com.br/ofertas"
            r = sess.get(url_fallback); r.html.render(timeout=40, sleep=4, scrolldown=10)
            items_a = extract_from_list_html(r.html.html, dom_a)
        except Exception:
            pass
    if not items_b and RHTML_OK:
        try:
            sess = HTMLSession()
            url_fallback = "https://loja.centerbox.com.br/clube" if store_b=="Centerbox" \
                           else "https://mercadinhossaoluiz.com.br/ofertas"
            r = sess.get(url_fallback); r.html.render(timeout=40, sleep=4, scrolldown=10)
            items_b = extract_from_list_html(r.html.html, dom_b)
        except Exception:
            pass

    if not items_a and not items_b:
        st.error("Não consegui capturar preços automaticamente. O site pode ter mudado o fluxo. Me avise e ajusto os seletores 😉")
        st.stop()

    # --------------- Mapeia pro catálogo e compara ---------------
    def map_prices(items):
        mapped={}
        for it in items:
            sec, key = match_canonical(it["name"])
            if not sec: 
                continue
            price = it["price"]
            cur = mapped.get((sec, key))
            if cur is None or price < cur:
                mapped[(sec, key)] = price
        return mapped

    map_a = map_prices(items_a)
    map_b = map_prices(items_b)

    total1=total2=0.0; sum1=sum2=0.0; pairs=0

    st.markdown("---")
    for section, products in CATALOG.items():
        rows=[]
        for p in products:
            key=p["key"]
            v1=map_a.get((section, key))
            v2=map_b.get((section, key))
            rows.append({
                "Produto": key,
                store_a: (f"R$ {v1:.2f}" if isinstance(v1,(int,float)) else "—"),
                store_b: (f"R$ {v2:.2f}" if isinstance(v2,(int,float)) else "—"),
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
    msg = f"**{store_a}** {fmt(total1)} × {fmt(total2)} **{store_b}** (mais itens com menor preço)"
    if total1>total2:
        winner = store_a
    elif total2>total1:
        winner = store_b
    else:
        if pairs>0:
            if sum1<sum2: winner=store_a; msg+=f". Desempate pela menor soma (R$ {sum1:.2f} vs R$ {sum2:.2f})."
            elif sum2<sum1: winner=store_b; msg+=f". Desempate pela menor soma (R$ {sum2:.2f} vs R$ {sum1:.2f})."
            else: winner=f"{store_a} / {store_b}"; msg+=". Empate após soma."
        else:
            winner=f"{store_a} / {store_b}"; msg+=". Empate técnico (pouca interseção)."

    st.success(f"🏆 **Vencedor:** {winner}\n\n{msg}")

    with st.expander("🔎 Itens capturados (debug)"):
        st.write(store_a, len(items_a)); st.write(items_a[:30])
        st.write(store_b, len(items_b)); st.write(items_b[:30])
