# app.py — Comparador de supermercados com fallback automático (Playwright → requests_html)
# - Auto-instala libs (pip) e tenta instalar Chromium (silencioso)
# - Se Playwright falhar ao lançar o browser, cai para requests_html (Pyppeteer)
# - Define CEP/loja quando possível; em seguida busca item-a-item via /busca?ft=...
# - Tabelas por seção e “vencedor” pelo critério de mais itens com menor preço

import sys, subprocess, importlib, re, json, unicodedata, urllib.parse
from urllib.parse import urlparse

# -------------------- AUTO-BOOTSTRAP --------------------
NEEDED = [
    ("streamlit", "streamlit>=1.34"),
    ("pandas", "pandas>=2.0"),
    ("bs4", "beautifulsoup4>=4.12"),
    ("lxml", "lxml>=5.2"),
    ("playwright", "playwright>=1.45"),
    ("requests_html", "requests-html==0.10.0"),  # fallback
]

def pip_install(spec):
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", spec])
    except Exception:
        pass

for mod, spec in NEEDED:
    try:
        importlib.import_module(mod)
    except Exception:
        pip_install(spec)

import streamlit as st, pandas as pd
from bs4 import BeautifulSoup

# tentar importar playwright; se não rolar, marcaremos para usar fallback
PLAYWRIGHT_OK = True
try:
    from playwright.sync_api import sync_playwright
except Exception:
    PLAYWRIGHT_OK = False

def ensure_chromium():
    """Tenta baixar o runtime do Chromium para o Playwright (silencioso)."""
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# roda uma vez por sessão
if "boot" not in st.session_state:
    with st.spinner("Preparando ambiente (primeira vez pode demorar)…"):
        if PLAYWRIGHT_OK:
            ensure_chromium()
    st.session_state["boot"] = True

# -------------------- Helpers de texto/tamanho --------------------
def norm(s:str)->str:
    s = unicodedata.normalize("NFD", str(s).lower())
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace() or ch in "xµ/.-")
    return " ".join(s.split())

def money(txt:str):
    if not isinstance(txt,str): return None
    m = re.search(r"(\d{1,3}(?:\.\d{3})*|\d+)\s*[,\.](\d{2})", txt)
    if not m: return None
    return float(f"{m.group(1).replace('.','')}.{m.group(2)}")

BRAND_ALIASES = {
    "nestle":"nestle","nestlé":"nestle","ninho":"ninho","danone":"danone",
    "omo":"omo","ype":"ype","ypê":"ype","veja":"veja","pinho":"pinho sol","pinho sol":"pinho sol",
    "downy":"downy","comfort":"comfort",
}
def brands_in(n:str):
    b=set()
    if "pinho sol" in n: b.add("pinho sol")
    for k in BRAND_ALIASES:
        if k!="pinho sol" and k in n: b.add(BRAND_ALIASES[k])
    return b

SIZE_RE = re.compile(r"(?:(\d{1,3})\s*[xX]\s*)?(\d+(?:[\.,]\d+)?)\s*(kg|g|l|ml)\b")
def parse_size(n:str):
    g=ml=None
    for m in SIZE_RE.finditer(n):
        mult=int(m.group(1)) if m.group(1) else 1
        v=float(m.group(2).replace(",",".")); u=m.group(3)
        if u=="kg": g=max(g or 0, v*1000*mult)
        elif u=="g": g=max(g or 0, v*mult)
        elif u=="l": ml=max(ml or 0, v*1000*mult)
        elif u=="ml": ml=max(ml or 0, v*mult)
    return g, ml

def approx(val,tgt,tol): return (val is not None) and (tgt-tol)<=val<=(tgt+tol)

# -------------------- Catálogo (seções) --------------------
CATALOG = {
    "ALIMENTOS":[
        {"key":"Arroz 5 kg","must":["arroz"],"size_g":5000,"tol_g":600,"q":"arroz 5kg"},
        {"key":"Feijão 1 kg","must":["feijao"],"size_g":1000,"tol_g":200,"q":"feijao 1kg"},
        {"key":"Leite em pó Ninho 380 g","must":["leite","po"],"brand_any":["ninho","nestle"],"size_g":380,"tol_g":60,"q":"leite po ninho 380g"},
        {"key":"Macarrão 500 g","must":["macarrao"],"size_g":500,"tol_g":100,"q":"macarrao 500g"},
        {"key":"Açúcar 1 kg","must":["acucar"],"size_g":1000,"tol_g":200,"q":"acucar 1kg"},
        {"key":"Sal 1 kg","must":["sal"],"size_g":1000,"tol_g":200,"q":"sal 1kg"},
        {"key":"Café 500 g","must":["cafe"],"size_g":500,"tol_g":100,"q":"cafe 500g"},
        {"key":"Farinha de trigo 1 kg","must":["farinha","trigo"],"size_g":1000,"tol_g":200,"q":"farinha trigo 1kg"},
        {"key":"Massa de milho (Fubá) 1 kg","must":["fuba"],"alt_any":[["massa","milho"]],"size_g":1000,"tol_g":300,"q":"fuba 1kg"},
        {"key":"Carne bovina (kg)","must":["carne"],"alt_any":[["bovina"],["patinho"],["contrafile"],["alcatra"],["acem"],["coxao"]],"perkg":True,"q":"carne bovina kg"},
    ],
    "FRUTAS":[
        {"key":"Mamão (kg)","must":["mamao"],"alt_any":[["papaya"],["formosa"]],"perkg":True,"q":"mamao kg"},
        {"key":"Banana (kg)","must":["banana"],"perkg":True,"q":"banana kg"},
        {"key":"Pera (kg)","must":["pera"],"perkg":True,"q":"pera kg"},
        {"key":"Uva (kg)","must":["uva"],"perkg":True,"q":"uva kg"},
        {"key":"Tangerina (kg)","must":["tangerina"],"alt_any":[["mexerica"],["bergamota"]],"perkg":True,"q":"tangerina kg"},
    ],
    "PRODUTO DE LIMPEZA":[
        {"key":"Sabão líquido OMO 3 L","must":["sabao","liquido"],"brand_any":["omo"],"size_ml":3000,"tol_ml":600,"q":"sabao liquido omo 3l"},
        {"key":"Amaciante Downy 1 L","must":["amaciante"],"brand_any":["downy","comfort"],"size_ml":1000,"tol_ml":300,"q":"amaciante downy 1l"},
        {"key":"Veja Multiuso 500 ml","must":["veja"],"size_ml":500,"tol_ml":150,"q":"veja multiuso 500ml"},
        {"key":"Pinho Sol 1 L","must":["pinho","sol"],"size_ml":1000,"tol_ml":300,"q":"pinho sol 1l"},
        {"key":"Detergente Ypê 500 ml","must":["detergente"],"brand_any":["ype"],"size_ml":500,"tol_ml":150,"q":"detergente ype 500ml"},
        {"key":"Água sanitária 1 L","must":["agua","sanitaria"],"alt_any":[["candida"]],"size_ml":1000,"tol_ml":300,"q":"agua sanitaria 1l"},
    ],
    "BEBIDA LÁCTEA":[
        {"key":"Iogurte integral Nestlé 170 g","must":["iogurte","integral"],"brand_any":["nestle","ninho"],"size_g":170,"tol_g":60,"q":"iogurte integral nestle 170g"},
        {"key":"Iogurte integral Danone 170 g","must":["iogurte","integral"],"brand_any":["danone"],"size_g":170,"tol_g":60,"q":"iogurte integral danone 170g"},
    ],
}

def tokens_ok(n, must): return all(t in n for t in (must or []))
def alt_ok(n, alt_any):
    if not alt_any: return True
    return any(all(t in n for t in grp) for grp in alt_any)
def brand_ok(n, brand_any):
    if not brand_any: return True
    return any(b in brands_in(n) for b in brand_any)
def size_ok(n, g=None,tg=None, ml=None,tml=None, perkg=False):
    if perkg: return True
    if g is None and ml is None: return True
    pg, pml = parse_size(n)
    if g is not None:  return approx(pg, g, tg or 0)
    if ml is not None: return approx(pml, ml, tml or 0)
    return True

def match_key(name):
    n = norm(name)
    for section, items in CATALOG.items():
        for it in items:
            if not tokens_ok(n, it.get("must")): continue
            if not alt_ok(n, it.get("alt_any")): continue
            if not brand_ok(n, it.get("brand_any")): continue
            if not size_ok(n, it.get("size_g"), it.get("tol_g"),
                          it.get("size_ml"), it.get("tol_ml"),
                          it.get("perkg", False)): continue
            return section, it["key"]
    return None, None

# -------------------- Parsing comum de cards --------------------
def extract_cards(html):
    items=[]
    soup=BeautifulSoup(html or "","lxml")
    cards=soup.select('[data-testid="product-summary-container"], .vtex-product-summary-2-x-container, .product-card, .shelf-item, .product, [itemtype*="Product"]')
    for c in cards[:2000]:
        # nome
        name=None
        for sel in ['[data-testid="product-name"]','.vtex-product-summary-2-x-productBrand','.product-title','.name','h3','h2','[itemprop="name"]']:
            el=c.select_one(sel)
            if el and el.get_text(strip=True):
                name=el.get_text(" ",strip=True); break
        if not name: name=c.get_text(" ",strip=True)[:200]
        # preço
        price=None
        meta=c.select_one('meta[itemprop="price"]')
        if meta and meta.has_attr("content"):
            try: price=float(str(meta["content"]).replace(",",".")); 
            except: price=None
        if price is None:
            elp=c.select_one('.vtex-product-price-1-x-sellingPriceValue, .best-price, .price, [data-price], .vtex-product-price-1-x-currencyInteger')
            if elp: price=money(elp.get_text(" ",strip=True))
        if name and price is not None: items.append({"name":name[:200],"price":price})
    # dedup
    out,seen=[],set()
    for it in items:
        k=(it["name"], round(float(it["price"]),2))
        if k not in seen: out.append(it); seen.add(k)
    return out

# -------------------- Playwright (quando possível) --------------------
def pw_collect_by_search(host, cep):
    """Coleta via Playwright — define CEP/loja e busca cada item."""
    results=[]
    from playwright.sync_api import sync_playwright  # import local (se disponível)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu","--no-zygote","--single-process"],
        )
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            locale="pt-BR", timezone_id="America/Fortaleza",
        )
        page = ctx.new_page()
        page.set_default_timeout(30000)

        # helpers de clique
        def click_if(role=None, name_regex=None, css=None, t=900):
            try:
                if css:
                    el = page.locator(css)
                    if el and el.is_visible(): el.click(); page.wait_for_timeout(t); return True
                if role and name_regex:
                    btn = page.get_by_role(role, name=re.compile(name_regex, re.I)).first
                    if btn and btn.is_visible(): btn.click(); page.wait_for_timeout(t); return True
            except Exception:
                return False
            return False

        def set_store_and_cep():
            # abrir host
            page.goto(host, wait_until="domcontentloaded")
            # cookies
            for pat in ["Aceitar","Aceito","Concordo","Permitir","OK","Prosseguir","Continuar","Fechar"]:
                click_if(role="button", name_regex=pat, t=600)
            # abrir modal
            click_if(role="button", name_regex="Selecion(e|ar).+método|Entrega|Retirada|loja", t=600)
            # CEP
            if cep:
                ok=False
                for css in ['input[placeholder*="CEP"]','input[placeholder*="cep"]','input[type="tel"]','input[name*="cep"]']:
                    try:
                        el=page.locator(css).first
                        if el and el.is_visible(): el.fill(cep); page.wait_for_timeout(400); ok=True; break
                    except Exception: pass
                if not ok:
                    try: page.get_by_placeholder(re.compile("CEP", re.I)).fill(cep); page.wait_for_timeout(400)
                    except Exception: pass
            # Centerbox → “CLIQUE E RETIRE” (ou entrega)
            if "centerbox" in host:
                clicked = click_if(role="button", name_regex=r"CLIQUE\s*E\s*RETIRE|Retirar|Retire", t=900)
                if not clicked: click_if(role="button", name_regex=r"RECEBA\s*EM\s*CASA|Entrega", t=900)
            # lista de lojas
            for pat in ["Selecionar esta loja","Usar esta loja","Selecionar","Usar loja","Escolher esta loja","Usar unidade"]:
                if click_if(role="button", name_regex=pat, t=900): break
            # confirmar
            for pat in ["Confirmar","Aplicar","Continuar","Salvar","OK"]:
                if click_if(role="button", name_regex=pat, t=800): break

        set_store_and_cep()

        def is_match(section, key, name):
            sec, k = match_key(name)
            return sec==section and k==key

        for section, items in CATALOG.items():
            for it in items:
                q = it.get("q") or it["key"]
                url = f"{host}/busca?ft={urllib.parse.quote(q)}"
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    # rolar para carregar a vitrine
                    for _ in range(10):
                        page.mouse.wheel(0, 1800); page.wait_for_timeout(250)
                    html = page.content()
                    for c in extract_cards(html)[:12]:
                        if is_match(section, it["key"], c["name"]):
                            results.append({"section":section,"key":it["key"],"name":c["name"],"price":float(c["price"])})
                            break
                except Exception:
                    pass

        ctx.close(); browser.close()
    return results

# -------------------- Fallback: requests_html --------------------
def rhtml_collect_by_search(host, cep):
    """
    Coleta com requests_html (Pyppeteer). Não conseguimos clicar no modal de CEP,
    mas muitos sites exibem preços mesmo assim. Se o modal bloquear geral, ficará '—'.
    """
    from requests_html import HTMLSession
    s = HTMLSession()
    results=[]

    def fetch_search(q):
        url = f"{host}/busca?ft={urllib.parse.quote(q)}"
        try:
            r = s.get(url, timeout=35)
            # tenta renderizar JS
            r.html.render(timeout=70, sleep=5, scrolldown=16)
            return r.html.html
        except Exception:
            try:
                return r.text
            except Exception:
                return ""

    def is_match(section, key, name):
        sec, k = match_key(name)
        return sec==section and k==key

    for section, items in CATALOG.items():
        for it in items:
            q = it.get("q") or it["key"]
            html = fetch_search(q)
            if not html: continue
            for c in extract_cards(html)[:12]:
                if is_match(section, it["key"], c["name"]):
                    results.append({"section":section,"key":it["key"],"name":c["name"],"price":float(c["price"])})
                    break
    return results

# -------------------- UI --------------------
st.set_page_config(page_title="Comparador de Supermercados", layout="wide")
st.title("🛒 Comparador — cole os links e, se precisar, um CEP")

c1,c2 = st.columns(2)
with c1: url1 = st.text_input("🔗 URL do Supermercado #1", "https://loja.centerbox.com.br/loja/58")
with c2: url2 = st.text_input("🔗 URL do Supermercado #2", "https://mercadinhossaoluiz.com.br/loja/355")
cep = st.text_input("📍 CEP (opcional — alguns sites só liberam preços após definir loja)", "60761-280")
go = st.button("Comparar")

def run_collect(host, cep):
    # 1) tenta Playwright
    if PLAYWRIGHT_OK:
        try:
            return pw_collect_by_search(host, cep), "playwright"
        except Exception:
            pass
    # 2) fallback requests_html
    try:
        return rhtml_collect_by_search(host, cep), "requests_html"
    except Exception:
        return [], "none"

if go:
    with st.spinner("Coletando preços item a item (com fallback automático)…"):
        host1=f"{urlparse(url1).scheme}://{urlparse(url1).netloc}"
        host2=f"{urlparse(url2).scheme}://{urlparse(url2).netloc}"
        res1, mode1 = run_collect(host1, cep)
        res2, mode2 = run_collect(host2, cep)

    name1 = urlparse(url1).netloc.replace("www.","")
    name2 = urlparse(url2).netloc.replace("www.","")

    st.info(f"{name1}: modo **{mode1}** · {name2}: modo **{mode2}**")

    # mapeia menor preço por produto
    map1, map2 = {}, {}
    for r in res1:
        k=(r["section"], r["key"])
        map1[k] = min(map1.get(k, 1e9), r["price"])
    for r in res2:
        k=(r["section"], r["key"])
        map2[k] = min(map2.get(k, 1e9), r["price"])

    # tabelas por seção
    st.markdown("## Resultados")
    for section in CATALOG.keys():
        data=[]
        for it in CATALOG[section]:
            k=(section, it["key"])
            v1=map1.get(k); v2=map2.get(k)
            data.append({
                "Produto": it["key"],
                name1: f"R$ {v1:.2f}" if isinstance(v1,(int,float)) else "—",
                name2: f"R$ {v2:.2f}" if isinstance(v2,(int,float)) else "—",
            })
        st.subheader(section)
        st.dataframe(pd.DataFrame(data), use_container_width=True)

    # score
    total1=total2=0.0; sum1=sum2=0.0; pairs=0
    for section, items in CATALOG.items():
        for it in items:
            k=(section,it["key"])
            v1=map1.get(k); v2=map2.get(k)
            if isinstance(v1,(int,float)) and isinstance(v2,(int,float)):
                if v1<v2: total1+=1
                elif v2<v1: total2+=1
                else: total1+=0.5; total2+=0.5
                sum1+=v1; sum2+=v2; pairs+=1

    def fmt(x): return f"{x:.1f}".replace(".0","")
    msg=f"**{name1}** {fmt(total1)} × {fmt(total2)} **{name2}** (mais itens com menor preço)"
    if total1>total2: winner=name1
    elif total2>total1: winner=name2
    else:
        if pairs>0:
            if sum1<sum2: winner=name1; msg+=f". Desempate pela menor soma (R$ {sum1:.2f} vs R$ {sum2:.2f})."
            elif sum2<sum1: winner=name2; msg+=f". Desempate pela menor soma (R$ {sum2:.2f} vs R$ {sum1:.2f})."
            else: winner=f"{name1} / {name2}"; msg+=". Empate após soma."
        else:
            winner=f"{name1} / {name2}"; msg+=". Empate técnico (poucos itens encontrados)."

    st.success(f"🏆 **Vencedor:** {winner}\n\n{msg}")

    with st.expander("🔎 Debug (amostras capturadas)"):
        st.write(name1, res1[:30])
        st.write(name2, res2[:30])

st.caption("O app tenta Playwright; se não der, usa requests_html. Alguns sites exigem loja/CEP rígidos e podem esconder preços no fallback.")
