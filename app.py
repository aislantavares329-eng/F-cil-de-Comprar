# app.py — Comparador de preços “plug-and-play”
# • Auto-instala dependências na 1ª execução (pip + playwright + chromium)
# • Desbloqueia loja/CEP (Centerbox: CEP + "CLIQUE E RETIRE") e abre /clube ou /ofertas
# • Lê preços de HTML, XHR/JSON VTEX e JSON embutido; fallback /busca?ft=…
# • Mostra tabela por seção e decide vencedor

import sys, subprocess, importlib, re, json, unicodedata, urllib.parse
from urllib.parse import urlparse
import time

# -------------------- AUTO-BOOTSTRAP --------------------
NEEDED = [
    ("streamlit", "streamlit>=1.34"),
    ("pandas", "pandas>=2.0"),
    ("bs4", "beautifulsoup4>=4.12"),
    ("lxml", "lxml>=5.2"),
    ("playwright", "playwright>=1.45"),
    ("requests_html", "requests-html==0.10.0"),  # fallback p/ JS
]

def pip_install(pkg):
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", pkg])
        return True
    except Exception:
        return False

def ensure_modules():
    missing = []
    for mod, spec in NEEDED:
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(spec)
    if missing:
        for spec in missing:
            pip_install(spec)

def ensure_chromium():
    # baixa o runtime do Chromium p/ playwright (silencioso)
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

# faz o bootstrap apenas 1x por sessão
ensure_modules()
try:
    import streamlit as st
    import pandas as pd
    from bs4 import BeautifulSoup
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
except Exception:
    # última tentativa
    ensure_modules()
    import streamlit as st
    import pandas as pd
    from bs4 import BeautifulSoup
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

if "boot_done" not in st.session_state:
    with st.spinner("Preparando o ambiente (1ª vez demora um pouquinho)…"):
        ensure_chromium()
    st.session_state["boot_done"] = True

# ------------------- UI -------------------
st.set_page_config(page_title="Comparador de Supermercados", layout="wide")
st.title("🛒 Comparador — cole os links e, se precisar, um CEP")

c1, c2 = st.columns(2)
with c1:
    url1 = st.text_input("🔗 URL do Supermercado #1", "")
with c2:
    url2 = st.text_input("🔗 URL do Supermercado #2", "")

cep = st.text_input("📍 CEP (opcional — alguns sites só liberam preços após definir loja)", "")
go = st.button("Comparar")

# ------------------- Helpers de parsing -------------------
def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s).lower())
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace() or ch in "xµ/.-")
    return " ".join(s.split())

def money(txt: str):
    if not isinstance(txt, str): return None
    m = re.search(r"(\d{1,3}(?:\.\d{3})*|\d+)\s*[,\.](\d{2})", txt)
    if not m: return None
    return float(f"{m.group(1).replace('.','')}.{m.group(2)}")

BRAND_ALIASES = {
    "nestle":"nestle","nestlé":"nestle","ninho":"ninho","danone":"danone",
    "omo":"omo","ype":"ype","ypê":"ype","veja":"veja","pinho":"pinho sol","pinho sol":"pinho sol",
    "downy":"downy","comfort":"comfort",
}
def brands_in(text_norm):
    b=set()
    if "pinho sol" in text_norm: b.add("pinho sol")
    for k in BRAND_ALIASES:
        if k!="pinho sol" and k in text_norm: b.add(BRAND_ALIASES[k])
    return b

SIZE_RE = re.compile(r"(?:(\d{1,3})\s*[xX]\s*)?(\d+(?:[\.,]\d+)?)\s*(kg|g|l|ml)\b")
def parse_size(text_norm):
    g=ml=None
    for m in SIZE_RE.finditer(text_norm):
        mult=int(m.group(1)) if m.group(1) else 1
        v=float(m.group(2).replace(",",".")); u=m.group(3)
        if u=="kg": g=max(g or 0, v*1000*mult)
        elif u=="g": g=max(g or 0, v*mult)
        elif u=="l": ml=max(ml or 0, v*1000*mult)
        elif u=="ml": ml=max(ml or 0, v*mult)
    return g, ml

def approx(val, tgt, tol): return (val is not None) and (tgt-tol)<=val<=(tgt+tol)

# ------------------- Catálogo (seções) -------------------
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
    n=norm(name)
    for section, items in CATALOG.items():
        for it in items:
            if not tokens_ok(n, it.get("must")): continue
            if not alt_ok(n, it.get("alt_any")): continue
            if not brand_ok(n, it.get("brand_any")): continue
            if not size_ok(n, it.get("size_g"), it.get("tol_g"), it.get("size_ml"), it.get("tol_ml"), it.get("perkg",False)): continue
            return section, it["key"]
    return None, None

# ------------------- Playwright flow -------------------
def click_if(page, role=None, name_regex=None, css=None, t=900):
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

def set_store_and_cep(page, cep, host=None):
    # cookies
    for pat in ["Aceitar","Aceito","Concordo","Permitir","OK","Prosseguir","Continuar","Fechar"]:
        click_if(page, role="button", name_regex=pat, t=600)

    # abre modal (pin / método)
    click_if(page, role="button", name_regex="Selecion(e|ar).+método|Entrega|Retirada|loja", t=600)

    # CEP
    if cep:
        ok=False
        for css in ['input[placeholder*="CEP"]','input[placeholder*="cep"]','input[type="tel"]','input[name*="cep"]']:
            try:
                el=page.locator(css).first
                if el and el.is_visible():
                    el.fill(cep); page.wait_for_timeout(400); ok=True; break
            except Exception: pass
        if not ok:
            try: page.get_by_placeholder(re.compile("CEP", re.I)).fill(cep); page.wait_for_timeout(400)
            except Exception: pass

    # Centerbox: clique e retire (ou entrega)
    if "centerbox" in (host or ""):
        clicked = click_if(page, role="button", name_regex=r"CLIQUE\s*E\s*RETIRE|Retirar|Retire", t=900)
        if not clicked:
            click_if(page, role="button", name_regex=r"RECEBA\s*EM\s*CASA|Entrega", t=900)
    else:
        click_if(page, role="button", name_regex="Confirmar|Aplicar|Continuar|OK|Usar|Salvar", t=800)

    # escolher 1ª loja se lista aparecer
    for pat in ["Selecionar esta loja","Usar esta loja","Selecionar","Usar loja","Escolher esta loja","Usar unidade"]:
        if click_if(page, role="button", name_regex=pat, t=900): break

    # confirmar se houver outro passo
    for pat in ["Confirmar","Aplicar","Continuar","Salvar","OK"]:
        if click_if(page, role="button", name_regex=pat, t=800): break

    # garantir listagem
    if host:
        pu = urlparse(page.url)
        if re.search(r"/loja/\d+", pu.path or ""):
            if "centerbox" in host:
                page.goto(f"{host}/clube", wait_until="domcontentloaded"); page.wait_for_timeout(900)
            elif "saoluiz" in host or "mercadinho" in host:
                page.goto(f"{host}/ofertas", wait_until="domcontentloaded"); page.wait_for_timeout(900)

def wait_prices(page):
    try:
        page.wait_for_selector('.vtex-product-price-1-x-sellingPriceValue, .best-price, meta[itemprop="price"]', timeout=12000)
    except Exception:
        pass

def scroll_more(page, rounds=18):
    for _ in range(rounds):
        page.mouse.wheel(0, 2200); page.wait_for_timeout(320)
    for _ in range(5):
        if not click_if(page, role="button", name_regex="ver mais|mais produtos|carregar"): break

def walk_json(obj):
    PRICE={"price","salePrice","bestPrice","value","finalPrice","sellingPrice","Price","SellingPrice","unitPrice"}
    NAME={"name","productName","itemName","title","Name","Title","product_name","description"}
    out=[]
    try:
        if isinstance(obj, dict):
            if "commertialOffer" in obj and isinstance(obj["commertialOffer"], dict):
                raw = obj["commertialOffer"].get("Price") or obj["commertialOffer"].get("ListPrice")
                nm = obj.get("name") or obj.get("productName") or obj.get("itemName") or ""
                if raw is not None and nm:
                    try: out.append({"name":str(nm), "price":float(raw)})
                    except: pass
            if any(k in obj for k in NAME) and any(k in obj for k in PRICE):
                nm = next((str(obj[k]) for k in NAME if k in obj and obj[k]), "")
                rv = next((obj[k] for k in PRICE if k in obj and obj[k] is not None), None)
                if isinstance(rv,str): pr=money(rv)
                elif isinstance(rv,(int,float)): pr=float(rv)
                else: pr=None
                if nm and pr is not None: out.append({"name":nm,"price":pr})
            for v in obj.values(): out+=walk_json(v)
        elif isinstance(obj, list):
            for x in obj: out+=walk_json(x)
    except Exception: pass
    return out

def parse_inline(html):
    items=[]
    for pat in [r"__STATE__\s*=\s*({.*?});</script>", r"__NEXT_DATA__\s*=\s*({.*?});</script>"]:
        for m in re.finditer(pat, html, re.S):
            try: items+=walk_json(json.loads(m.group(1)))
            except Exception: pass
    for m in re.finditer(r"<script[^>]*application/json[^>]*>(.*?)</script>", html, re.S|re.I):
        try: items+=walk_json(json.loads(m.group(1)))
        except Exception: pass
    # dedup
    out,seen=[],set()
    for it in items:
        k=(it["name"], round(float(it["price"]),2))
        if k not in seen: out.append(it); seen.add(k)
    return out

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

def fetch_with_playwright(url, cep):
    ensure_chromium()
    captured=[]
    html=""
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True, args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu","--no-zygote","--single-process"])
            ctx=browser.new_context()
            page=ctx.new_page()
            page.set_default_timeout(30000)

            def on_response(res):
                try:
                    ct=(res.headers or {}).get("content-type","").lower()
                    if "application/json" in ct or res.url.endswith(".json"):
                        try: data=res.json()
                        except Exception:
                            try: data=json.loads(res.text())
                            except Exception: data=None
                        if data is not None:
                            captured.extend(walk_json(data))
                except Exception: pass
            page.on("response", on_response)

            try: page.goto(url, wait_until="domcontentloaded")
            except PwTimeout: pass

            host=f"{urlparse(page.url).scheme}://{urlparse(page.url).netloc}"
            set_store_and_cep(page, cep, host=host)
            # garantir que estamos na listagem
            if "centerbox" in host:
                page.goto(f"{host}/clube", wait_until="domcontentloaded")
            elif "saoluiz" in host or "mercadinho" in host:
                page.goto(f"{host}/ofertas", wait_until="domcontentloaded")
            wait_prices(page); scroll_more(page)
            html=page.content()
            ctx.close(); browser.close()
    except Exception:
        pass
    return html, captured

def fetch_with_requests_html(url):
    # fallback quando Chromium não pôde rodar
    try:
        from requests_html import HTMLSession
        s=HTMLSession()
        r=s.get(url, timeout=35)
        r.html.render(timeout=70, sleep=6, scrolldown=16)
        return r.html.html
    except Exception:
        return ""

def fetch(url, cep):
    html, cap = fetch_with_playwright(url, cep)
    if not html:
        html = fetch_with_requests_html(url)
    return html, cap

def map_prices(items):
    mapped={}
    for it in items:
        sec,key=match_key(it["name"])
        if not sec: continue
        cur=mapped.get((sec,key))
        if cur is None or it["price"]<cur: mapped[(sec,key)] = float(it["price"])
    return mapped

def missing_keys(mapped):
    want=set()
    for section, items in CATALOG.items():
        for it in items:
            if (section, it["key"]) not in mapped:
                want.add((section, it["key"]))
    return want

def search_fill(host, cep, already_map, need_keys):
    # usa /busca?ft=… pra completar
    extra=[]
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True, args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu","--no-zygote","--single-process"])
            ctx=browser.new_context(); pg=ctx.new_page(); pg.set_default_timeout(25000)
            pg.goto(host, wait_until="domcontentloaded"); set_store_and_cep(pg, cep, host=host)
            for section, items in CATALOG.items():
                for it in items:
                    key=it["key"]
                    if key not in need_keys: continue
                    q=it.get("q") or key
                    url=f"{host}/busca?ft={urllib.parse.quote(q)}"
                    try:
                        pg.goto(url, wait_until="domcontentloaded")
                        wait_prices(pg); scroll_more(pg, 8)
                        html=pg.content()
                        for hit in extract_cards(html)[:10]:
                            s,k=match_key(hit["name"])
                            if s==section and k==key and (s,k) not in already_map:
                                extra.append({"name":hit["name"],"price":float(hit["price"])})
                                break
                    except Exception: pass
            ctx.close(); browser.close()
    except Exception:
        pass
    # dedup
    out,seen=[],set()
    for it in extra:
        k=(it["name"], round(float(it["price"]),2))
        if k not in seen: out.append(it); seen.add(k)
    return out

# ------------------- APP FLOW -------------------
if go:
    if not url1 or not url2:
        st.error("Manda os dois links 😉"); st.stop()

    with st.spinner("Abrindo páginas, definindo loja/CEP e coletando preços…"):
        html1, net1 = fetch(url1, cep)
        html2, net2 = fetch(url2, cep)

    items1 = extract_cards(html1) + net1 + parse_inline(html1)
    items2 = extract_cards(html2) + net2 + parse_inline(html2)

    # dedup
    def dedup(items):
        out,seen=[],set()
        for it in items:
            if not it.get("name") or not isinstance(it.get("price"), (int,float)): continue
            k=(it["name"], round(float(it["price"]),2))
            if k not in seen: out.append({"name":it["name"],"price":float(it["price"])}); seen.add(k)
        return out
    items1, items2 = dedup(items1), dedup(items2)

    host1=f"{urlparse(url1).scheme}://{urlparse(url1).netloc}"
    host2=f"{urlparse(url2).scheme}://{urlparse(url2).netloc}"

    map1, map2 = map_prices(items1), map_prices(items2)

    miss1 = missing_keys(map1)
    miss2 = missing_keys(map2)

    if len(miss1)>6:
        need={k for _,k in miss1}
        add=search_fill(host1, cep, map1, need)
        items1+=add; map1 = map_prices(items1)
    if len(miss2)>6:
        need={k for _,k in miss2}
        add=search_fill(host2, cep, map2, need)
        items2+=add; map2 = map_prices(items2)

    name1=urlparse(url1).netloc.replace("www.","")
    name2=urlparse(url2).netloc.replace("www.","")

    total1=total2=0.0; sum1=sum2=0.0; pairs=0

    st.markdown("---")
    for section, products in CATALOG.items():
        rows=[]
        for it in products:
            key=it["key"]
            v1=map1.get((section,key)); v2=map2.get((section,key))
            rows.append({"Produto":key, name1:(f"R$ {v1:.2f}" if isinstance(v1,(int,float)) else "—"),
                                   name2:(f"R$ {v2:.2f}" if isinstance(v2,(int,float)) else "—")})
            if isinstance(v1,(int,float)) and isinstance(v2,(int,float)):
                if v1<v2: total1+=1
                elif v2<v1: total2+=1
                else: total1+=0.5; total2+=0.5
                sum1+=v1; sum2+=v2; pairs+=1
        st.subheader(section)
        st.dataframe(pd.DataFrame(rows), use_container_width=True)

    st.markdown("---"); st.markdown("### 🏁 Resultado")
    def fmt(x): return f"{x:.1f}".replace(".0","")
    msg=f"**{name1}** {fmt(total1)} × {fmt(total2)} **{name2}** (critério: mais itens com menor preço)"
    if total1>total2: winner=name1
    elif total2>total1: winner=name2
    else:
        if pairs>0:
            if sum1<sum2: winner=name1; msg+=f". Desempate pela menor soma (R$ {sum1:.2f} vs R$ {sum2:.2f})."
            elif sum2<sum1: winner=name2; msg+=f". Desempate pela menor soma (R$ {sum2:.2f} vs R$ {sum1:.2f})."
            else: winner=f"{name1} / {name2}"; msg+=". Empate após soma."
        else:
            winner=f"{name1} / {name2}"; msg+=". Empate técnico."
    st.success(f"🏆 **Vencedor:** {winner}\n\n{msg}")

st.caption("Dica: cole /loja/XX (ou /clube /ofertas). O app aceita cookies, define CEP, seleciona loja e raspa os preços automaticamente.")
