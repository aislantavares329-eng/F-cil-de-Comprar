# app.py — Comparador por SEÇÃO (2 supermercados) com TAMANHO e MARCA
# Critério de vencedor: quem tem MAIS itens com MENOR preço (empate = 0,5). Desempate: menor soma.

import re
import unicodedata
from urllib.parse import urlparse
import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup

# tentar dados estruturados
try:
    import extruct
    from w3lib.html import get_base_url
    EXSTRUCT = True
except Exception:
    EXSTRUCT = False

st.set_page_config(page_title="Comparador por SEÇÃO (2 supermercados)", layout="wide")
st.title("🛒 Comparador por SEÇÃO (2 supermercados) — com tamanho e marca")

# ----------------------- Utils -----------------------
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return "desconhecido"

def fetch_html(url: str):
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        return r.text, r.url
    except Exception:
        return "", url

def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s).lower())
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace() or ch in "xµ/.-")
    return " ".join(s.split())

def cleanup_money(txt: str):
    if not isinstance(txt, str):
        return None
    m = re.search(r"(\d{1,3}(?:\.\d{3})*|\d+)(?:,(\d{2}))", txt)
    if not m:
        return None
    inteiro = m.group(1).replace(".", "")
    cent = m.group(2)
    try:
        return float(f"{inteiro}.{cent}")
    except Exception:
        return None

# ----------------------- Parsing de tamanho e marca -----------------------
BRAND_ALIASES = {
    "nestle": "nestle", "nestlé": "nestle", "ninho": "ninho",
    "danone": "danone",
    "omo": "omo",
    "ype": "ype", "ypê": "ype", "ypee": "ype",
    "veja": "veja",
    "pinho sol": "pinho sol", "pinho": "pinho sol",
    "downy": "downy", "comfort": "comfort",
}

def extract_brands(text_norm: str):
    # detecta marcas conhecidas; checa bigramas também (pinho sol)
    brands = set()
    if "pinho sol" in text_norm:
        brands.add("pinho sol")
    for b in BRAND_ALIASES.keys():
        if b != "pinho sol" and b in text_norm:
            brands.add(BRAND_ALIASES[b])
    return brands

SIZE_REGEX = re.compile(
    r"(?:(\d{1,3})\s*[xX]\s*)?"              # pack ex.: 12x
    r"(\d+(?:[\.,]\d+)?)\s*"                 # valor
    r"(kg|g|l|ml)\b"                         # unidade
)

def parse_size(text_norm: str):
    """
    Retorna {"g":..., "kg":..., "ml":..., "l":..., "pack":N, "effective_g":..., "effective_ml":...}
    Se houver 12x170g -> pack=12, g=170, effective_g = 12*170
    """
    total_g = None
    total_ml = None
    pack = 1
    matches = list(SIZE_REGEX.finditer(text_norm))
    # pega o MAIOR total encontrado (se aparecer mais de um, ex.: 6x1l + 200ml brinde)
    for m in matches:
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
    return {
        "pack": pack,
        "effective_g": total_g,   # em gramas
        "effective_ml": total_ml, # em ml
    }

def approx(val, tgt, tol):
    if val is None:
        return False
    return (tgt - tol) <= val <= (tgt + tol)

# ----------------------- Catálogo por seção (com marca e tamanho) -----------------------
# size: usar chaves "g", "ml", "kg", "l" (aqui padronizamos em g/ml para matching)
CATALOG = {
    "ALIMENTOS": [
        {"key": "Arroz 5 kg",                 "must": ["arroz"],                       "size_g": 5000, "size_tol_g": 600},
        {"key": "Feijão 1 kg",                "must": ["feijao"],                      "size_g": 1000, "size_tol_g": 200},
        {"key": "Leite em pó Ninho 380 g",    "must": ["leite", "po"], "brand_any": ["ninho","nestle"], "size_g": 380, "size_tol_g": 60},
        {"key": "Macarrão 500 g",             "must": ["macarrao"],                    "size_g": 500,  "size_tol_g": 100},
        {"key": "Açúcar 1 kg",                "must": ["acucar"],                      "size_g": 1000, "size_tol_g": 200},
        {"key": "Sal 1 kg",                   "must": ["sal"],                         "size_g": 1000, "size_tol_g": 200},
        {"key": "Café 500 g",                 "must": ["cafe"],                        "size_g": 500,  "size_tol_g": 100},
        {"key": "Farinha de trigo 1 kg",      "must": ["farinha","trigo"],             "size_g": 1000, "size_tol_g": 200},
        {"key": "Massa de milho (Fubá) 1 kg", "must": ["fuba"], "alt_any":[["massa","milho"]],
                                                                                       "size_g": 1000, "size_tol_g": 300},
        {"key": "Carne bovina (kg)",          "must": ["carne"], "alt_any":[["bovina"],["patinho"],["contrafile"],["alcatra"],["acem"],["coxao"]],
                                                                                       "perkg": True},
    ],
    "FRUTAS": [
        {"key": "Mamão (kg)",     "must": ["mamao"],     "alt_any":[["papaya"],["formosa"]], "perkg": True},
        {"key": "Banana (kg)",    "must": ["banana"],                                  "perkg": True},
        {"key": "Pera (kg)",      "must": ["pera"],                                    "perkg": True},
        {"key": "Uva (kg)",       "must": ["uva"],                                     "perkg": True},
        {"key": "Tangerina (kg)", "must": ["tangerina"], "alt_any":[["mexerica"],["bergamota"]], "perkg": True},
    ],
    "PRODUTO DE LIMPEZA": [
        {"key": "Sabão líquido OMO 3 L",  "must": ["sabao","liquido"], "brand_any":["omo"],      "size_ml": 3000, "size_tol_ml": 600},
        {"key": "Amaciante Downy 1 L",    "must": ["amaciante"],       "brand_any":["downy","comfort"], "size_ml": 1000, "size_tol_ml": 300},
        {"key": "Veja Multiuso 500 ml",   "must": ["veja"],                                    "size_ml": 500,  "size_tol_ml": 150},
        {"key": "Pinho Sol 1 L",          "must": ["pinho","sol"],                             "size_ml": 1000, "size_tol_ml": 300},
        {"key": "Detergente Ypê 500 ml",  "must": ["detergente"],       "brand_any":["ype"],   "size_ml": 500,  "size_tol_ml": 150},
        {"key": "Água sanitária 1 L",     "must": ["agua","sanitaria"], "alt_any":[["candida"]], "size_ml": 1000, "size_tol_ml": 300},
    ],
    "BEBIDA LÁCTEA": [
        {"key": "Iogurte integral Nestlé 170 g", "must": ["iogurte","integral"], "brand_any":["nestle","ninho"], "size_g": 170, "size_tol_g": 60},
        {"key": "Iogurte integral Danone 170 g", "must": ["iogurte","integral"], "brand_any":["danone"],         "size_g": 170, "size_tol_g": 60},
    ]
}

def tokens_ok(text_norm: str, must: list) -> bool:
    return all(tok in text_norm for tok in must)

def any_alt_hit(text_norm: str, alt_any: list[list[str]] | None) -> bool:
    if not alt_any:
        return True
    for group in alt_any:
        if all(tok in text_norm for tok in group):
            return True
    return False

def brand_ok(text_norm: str, brand_any: list[str] | None) -> bool:
    if not brand_any:
        return True
    brands_found = extract_brands(text_norm)
    return any(b in brands_found for b in brand_any)

def size_ok(text_norm: str, size_g=None, tol_g=None, size_ml=None, tol_ml=None) -> bool:
    if size_g is None and size_ml is None:
        return True
    parsed = parse_size(text_norm)
    if size_g is not None:
        return approx(parsed["effective_g"], size_g, tol_g or 0)
    if size_ml is not None:
        return approx(parsed["effective_ml"], size_ml, tol_ml or 0)
    return True

# ----------------------- Extração de produtos -----------------------
def extract_products(html: str, base_url: str):
    """Retorna lista de {name, price} (float). Tenta JSON-LD/Microdata → heurística leve."""
    items = []

    # 1) JSON-LD & Microdata
    if EXSTRUCT and html:
        try:
            data = extruct.extract(html, base_url=get_base_url(html, base_url), syntaxes=['json-ld','microdata'])
            # JSON-LD
            for blk in data.get('json-ld', []) or []:
                nodes = blk if isinstance(blk, list) else [blk]
                for node in nodes:
                    items.extend(_from_structured_node(node))
            # Microdata
            for m in (data.get('microdata') or []):
                node = m.get('properties') or {}
                node["@type"] = m.get('type') or node.get("@type")
                items.extend(_from_structured_node(node))
        except Exception:
            pass

    # 2) Heurística no HTML (cartões de produto)
    if not items:
        soup = BeautifulSoup(html or "", "lxml")
        cards = soup.select('[itemtype*="Product"], [itemscope][itemtype*="Product"], .product, .produto, .card, .item, .product-card')
        for c in cards[:400]:
            txt = c.get_text(separator=" ", strip=True)
            price = cleanup_money(txt)
            if price is None:
                continue
            name_el = c.select_one('[itemprop="name"], .product-title, .titulo, .name, h2, h3, .product-name')
            name = (name_el.get_text(" ", strip=True) if name_el else txt)[:160]
            items.append({"name": name, "price": price})

    # Dedup básico
    dedup, seen = [], set()
    for it in items:
        if it.get("name") and it.get("price") is not None:
            k = (it["name"], it["price"])
            if k not in seen:
                dedup.append(it); seen.add(k)
    return dedup

def _from_structured_node(node):
    out = []
    if not isinstance(node, dict):
        return out
    t = node.get("@type")
    if isinstance(t, list):
        t = " ".join(t)
    # Product com offers
    if t and "Product" in str(t):
        name = node.get("name") or node.get("description") or ""
        offers = node.get("offers")
        if isinstance(offers, dict):
            p = _price_from_offer(offers)
            if p is not None:
                out.append({"name": name, "price": p})
        elif isinstance(offers, list):
            for off in offers:
                p = _price_from_offer(off)
                if p is not None:
                    out.append({"name": name, "price": p})
    # Offer isolado
    if (t and "Offer" in str(t)) or node.get("price"):
        p2 = _price_from_offer(node)
        if p2 is not None:
            out.append({"name": node.get("name") or "", "price": p2})
    return out

def _price_from_offer(offer):
    if not isinstance(offer, dict):
        return None
    if offer.get("price"):
        return cleanup_money(str(offer["price"]))
    ps = offer.get("priceSpecification") or {}
    if isinstance(ps, dict) and ps.get("price"):
        return cleanup_money(str(ps["price"]))
    return None

# ----------------------- Matching p/ catálogo -----------------------
def match_canonical(prod_name: str):
    n = norm(prod_name)
    for section, items in CATALOG.items():
        for item in items:
            if not tokens_ok(n, item.get("must", [])):
                continue
            if not any_alt_hit(n, item.get("alt_any")):
                continue
            if not brand_ok(n, item.get("brand_any")):
                continue
            if not size_ok(
                n,
                size_g=item.get("size_g"), tol_g=item.get("size_tol_g"),
                size_ml=item.get("size_ml"), tol_ml=item.get("size_tol_ml"),
            ):
                # Se for item "perkg", não forçamos tamanho
                if not item.get("perkg"):
                    continue
            # passou em tudo
            return section, item["key"]
    return None, None

# ----------------------- UI -----------------------
c1, c2 = st.columns(2)
with c1:
    url1 = st.text_input("🔗 URL do Supermercado #1", placeholder="https://...")
with c2:
    url2 = st.text_input("🔗 URL do Supermercado #2", placeholder="https://...")

go = st.button("Comparar")

if go:
    if not url1 or not url2:
        st.error("Manda os dois links 😉")
        st.stop()

    # Carrega páginas
    html1, final1 = fetch_html(url1)
    html2, final2 = fetch_html(url2)
    if not html1: st.error(f"Não consegui abrir: {url1}")
    if not html2: st.error(f"Não consegui abrir: {url2}")
    if not (html1 and html2):
        st.stop()

    # Extrai produtos
    items1 = extract_products(html1, final1)
    items2 = extract_products(html2, final2)
    name1 = (BeautifulSoup(html1 or "", "lxml").find("title").text.strip() if BeautifulSoup(html1 or "", "lxml").find("title") else domain_of(final1))
    name2 = (BeautifulSoup(html2 or "", "lxml").find("title").text.strip() if BeautifulSoup(html2 or "", "lxml").find("title") else domain_of(final2))

    # Mapeia p/ catálogo (pega o MENOR preço que casar por produto/supermercado)
    def map_prices(items):
        mapped = {}  # (section, key) -> min price
        for it in items:
            sec, key = match_canonical(it["name"])
            if not sec:
                continue
            price = it["price"]
            cur = mapped.get((sec, key))
            if cur is None or price < cur:
                mapped[(sec, key)] = price
        return mapped

    map1 = map_prices(items1)
    map2 = map_prices(items2)

    # Monta tabelas por seção
    total_score_1 = 0.0
    total_score_2 = 0.0
    sum_prices_1 = 0.0
    sum_prices_2 = 0.0
    counted_pairs = 0

    st.markdown("---")
    for section, products in CATALOG.items():
        rows = []
        for p in products:
            key = p["key"]
            v1 = map1.get((section, key))
            v2 = map2.get((section, key))
            rows.append({
                "Produto": key,
                name1: (f"R$ {v1:.2f}" if isinstance(v1, (int,float)) else "—"),
                name2: (f"R$ {v2:.2f}" if isinstance(v2, (int,float)) else "—"),
            })
            # contagem de “quem tem o menor preço”
            if isinstance(v1, (int,float)) and isinstance(v2, (int,float)):
                if v1 < v2:
                    total_score_1 += 1
                elif v2 < v1:
                    total_score_2 += 1
                else:
                    total_score_1 += 0.5
                    total_score_2 += 0.5
                sum_prices_1 += v1; sum_prices_2 += v2
                counted_pairs += 1
            # se só um tem, não pontua (pra não viciar), mas poderíamos optar por pontuar 1-0; mantive neutro

        if rows:
            st.subheader(section)
            st.dataframe(pd.DataFrame(rows), use_container_width=True)

    # Decide vencedor
    st.markdown("---")
    st.markdown("### 🏁 Resultado")

    def fmt_score(s): 
        return f"{s:.1f}".replace(".0", "")

    res_txt = (f"**{name1}** {fmt_score(total_score_1)} × {fmt_score(total_score_2)} **{name2}** "
               f"(critério: mais produtos com menor preço; empate = 0,5)")

    if total_score_1 > total_score_2:
        winner = name1
    elif total_score_2 > total_score_1:
        winner = name2
    else:
        if counted_pairs > 0:
            if sum_prices_1 < sum_prices_2:
                winner = name1
                res_txt += f". Desempate por soma nas comparações: **R$ {sum_prices_1:.2f} vs R$ {sum_prices_2:.2f}**."
            elif sum_prices_2 < sum_prices_1:
                winner = name2
                res_txt += f". Desempate por soma nas comparações: **R$ {sum_prices_2:.2f} vs R$ {sum_prices_1:.2f}**."
            else:
                winner = f"{name1} / {name2}"
                res_txt += ". Permaneceu empatado após desempate por soma."
        else:
            winner = f"{name1} / {name2}"
            res_txt += ". Empate técnico (poucos itens em comum com preço)."

    st.success(f"🏆 **Vencedor:** {winner}\n\n{res_txt}")

    st.caption(
        "Observações: matching por nome + marca + tamanho (com tolerância). "
        "Unidades/gramaturas são inferidas do texto (ex.: 12x170 g, 1 L). "
        "Para frutas/carnes assumimos preço por kg (quando possível). Sites que renderizam via JavaScript podem ocultar preços."
    )
