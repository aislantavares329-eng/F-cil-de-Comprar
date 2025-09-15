# app.py — Comparador de preços (versão HTTP pura, sem navegador)
# - Consulta diretamente as APIs públicas VTEX (search/autocomplete)
# - Faz o match por marca/tamanho/peso e escolhe o menor preço válido
# - Mostra por seção e elege o supermercado vencedor

import re, json, unicodedata, urllib.parse, requests
from urllib.parse import urlparse
import streamlit as st
import pandas as pd

# ---------- Helpers básicos ----------
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

def approx(val, tgt, tol): return (val is not None) and (tgt-tol)<=val<=(tgt+tol)

# ---------- Catálogo ----------
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

# ---------- Clientes HTTP ----------
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
}

def get_json(url, timeout=20):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.ok:
            return r.json()
    except Exception:
        return None
    return None

def get_text(url, timeout=20):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.ok:
            return r.text
    except Exception:
        return ""
    return ""

def vtex_search(host, q):
    """
    VTEX: /api/catalog_system/pub/products/search/?ft=<q>
    retorna lista de produtos com items -> sellers -> commertialOffer.Price / spotPrice
    """
    url = f"{host}/api/catalog_system/pub/products/search/?ft={urllib.parse.quote(q)}"
    data = get_json(url)
    out=[]
    if isinstance(data, list):
        for prod in data:
            name = prod.get("productName") or prod.get("productTitle") or ""
            for item in prod.get("items", []):
                sku_name = item.get("name") or item.get("itemName") or ""
                nm = (sku_name or name) or ""
                for s in item.get("sellers", []):
                    offer = s.get("commertialOffer", {})
                    raw = offer.get("Price") or offer.get("spotPrice") or offer.get("ListPrice")
                    if isinstance(raw, (int, float)) and raw>0:
                        out.append({"name": nm, "price": float(raw)})
    # dedup
    seen=set(); uniq=[]
    for it in out:
        k=(it["name"], round(it["price"],2))
        if k not in seen:
            uniq.append(it); seen.add(k)
    return uniq

def vtex_autocomplete_urls(host, q):
    """
    VTEX: /buscaautocomplete/?productNameContains=<q>
    Pega URLs dos produtos sugeridos e tenta raspar JSON-LD da PDP.
    """
    url = f"{host}/buscaautocomplete/?productNameContains={urllib.parse.quote(q)}"
    data = get_json(url)
    urls=[]
    try:
        items = data.get("itemsReturned", [])
        for it in items:
            href = it.get("href") or it.get("url")
            if href:
                if href.startswith("http"): urls.append(href)
                else: urls.append(host + href)
    except Exception:
        pass
    return urls[:6]

def pdp_jsonld_price(html):
    # tenta achar "application/ld+json" com oferta
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S|re.I):
        try:
            obj=json.loads(m.group(1))
            if isinstance(obj, dict):
                # produto com offers
                offers=obj.get("offers") or {}
                if isinstance(offers, dict):
                    p=offers.get("price") or offers.get("lowPrice") or offers.get("highPrice")
                    try: return float(str(p).replace(",", "."))
                    except Exception: pass
            elif isinstance(obj, list):
                for x in obj:
                    if isinstance(x, dict) and x.get("@type") in ("Product","AggregateOffer","Offer"):
                        offers=x.get("offers") or {}
                        if isinstance(offers, dict):
                            p=offers.get("price") or offers.get("lowPrice") or offers.get("highPrice")
                            try: return float(str(p).replace(",", "."))
                            except Exception: pass
        except Exception:
            continue
    # meta itemprop
    m=re.search(r'<meta[^>]+itemprop="price"[^>]+content="([\d\. ,]+)"', html, re.I)
    if m:
        try: return float(m.group(1).replace(".","").replace(",","."))
        except Exception: return None
    return None

def vtex_pdp_prices_from_autocomplete(host, q):
    out=[]
    for u in vtex_autocomplete_urls(host, q):
        html = get_text(u)
        pr = pdp_jsonld_price(html)
        if isinstance(pr, (int,float)) and pr>0:
            # nome bruto do <title> como fallback
            m = re.search(r"<title>(.*?)</title>", html, re.S|re.I)
            nm = m.group(1).strip() if m else u
            out.append({"name": nm[:180], "price": float(pr)})
    # dedup
    seen=set(); uniq=[]
    for it in out:
        k=(it["name"], round(it["price"],2))
        if k not in seen:
            uniq.append(it); seen.add(k)
    return uniq

# ---------- Estratégia por host ----------
def collect_for_host(host):
    results=[]
    def is_match(section, key, name):
        sec,k = match_key(name)
        return sec==section and k==key

    for section, items in CATALOG.items():
        for it in items:
            q = it.get("q") or it["key"]
            # 1) tenta API de busca VTEX
            hits = vtex_search(host, q)
            # 2) se nada, tenta autocomplete + PDP
            if not hits:
                hits = vtex_pdp_prices_from_autocomplete(host, q)
            # filtra e escolhe o melhor que bate
            best=None
            for h in hits:
                if is_match(section, it["key"], h["name"]):
                    if (best is None) or (h["price"]<best["price"]):
                        best=h
            if best:
                results.append({"section":section,"key":it["key"],"name":best["name"],"price":best["price"]})
    return results

# ---------- UI ----------
st.set_page_config(page_title="Comparador de Supermercados (HTTP)", layout="wide")
st.title("🛒 Comparador — cole os links das lojas (VTEX)")

c1,c2 = st.columns(2)
with c1: url1 = st.text_input("🔗 URL do Supermercado #1", "https://loja.centerbox.com.br/loja/58")
with c2: url2 = st.text_input("🔗 URL do Supermercado #2", "https://mercadinhossaoluiz.com.br/loja/355")
go = st.button("Comparar")

if go:
    host1=f"{urlparse(url1).scheme}://{urlparse(url1).netloc}"
    host2=f"{urlparse(url2).scheme}://{urlparse(url2).netloc}"

    with st.spinner("Consultando APIs e páginas…"):
        res1 = collect_for_host(host1)
        res2 = collect_for_host(host2)

    # mapeia menor preço por produto
    map1, map2 = {}, {}
    for r in res1:
        k=(r["section"], r["key"])
        map1[k] = min(map1.get(k, 1e9), r["price"])
    for r in res2:
        k=(r["section"], r["key"])
        map2[k] = min(map2.get(k, 1e9), r["price"])

    name1 = urlparse(url1).netloc.replace("www.","")
    name2 = urlparse(url2).netloc.replace("www.","")

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

    # placar
    total1=total2=0.0; sum1=sum2=0.0; pares=0
    for section, items in CATALOG.items():
        for it in items:
            k=(section,it["key"])
            v1=map1.get(k); v2=map2.get(k)
            if isinstance(v1,(int,float)) and isinstance(v2,(int,float)):
                if v1<v2: total1+=1
                elif v2<v1: total2+=1
                else: total1+=0.5; total2+=0.5
                sum1+=v1; sum2+=v2; pares+=1

    def fmt(x): return f"{x:.1f}".replace(".0","")
    msg=f"**{name1}** {fmt(total1)} × {fmt(total2)} **{name2}** (mais itens com menor preço)"
    if total1>total2: vencedor=name1
    elif total2>total1: vencedor=name2
    else:
        if pares>0:
            if sum1<sum2: vencedor=name1; msg+=f". Desempate pela menor soma (R$ {sum1:.2f} vs R$ {sum2:.2f})."
            elif sum2<sum1: vencedor=name2; msg+=f". Desempate pela menor soma (R$ {sum2:.2f} vs R$ {sum1:.2f})."
            else: vencedor=f"{name1} / {name2}"; msg+=". Empate após soma."
        else:
            vencedor=f"{name1} / {name2}"; msg+=". Poucos itens com preço público."
    st.success(f"🏆 **Vencedor:** {vencedor}\n\n{msg}")

    with st.expander("🔎 Debug (amostras capturadas)"):
        st.write(name1, res1[:20])
        st.write(name2, res2[:20])

st.caption("Trabalho direto nas APIs VTEX (/api/catalog_system/pub/products/search, /buscaautocomplete). Se algum preço não vier, o site pode limitar por região/loja.")
