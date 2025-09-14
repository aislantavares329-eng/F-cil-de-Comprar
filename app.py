# app.py — Comparador de Supermercados (2 links) + plano 5W2H
# - Extrai preços via JSON-LD (schema.org) ou heurística
# - Compara custos, tenta achar "dia de promoção", calcula distância
# - Gera plano 5W2H do vencedor e permite baixar CSV

import re, json, math
from urllib.parse import urlparse
import requests
import pandas as pd
import streamlit as st

from bs4 import BeautifulSoup
try:
    import extruct
    from w3lib.html import get_base_url
    EXSTRUCT_OK = True
except Exception:
    EXSTRUCT_OK = False

from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
from math import radians, sin, cos, asin, sqrt
from datetime import datetime, timedelta

st.set_page_config(page_title="Comparador de Supermercados (2 links)", layout="wide")
st.title("🛒 Comparador de Supermercados (2 links)")

st.caption("Cole 2 URLs de páginas de produto/ofertas/flyer dos supermercados. "
           "Eu tento extrair preços via JSON-LD (schema.org) e heurística. "
           "Também tento achar dia de promoção e calculo distância a partir do seu endereço.")

# -----------------------------
# Helpers
# -----------------------------
def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return "desconhecido"

def cleanup_money(txt: str):
    if not isinstance(txt, str):
        return None
    # PT-BR: R$ 12,34 | 12,34 | 1.234,56
    m = re.search(r"(\d{1,3}(?:\.\d{3})*|\d+)(?:,(\d{2}))", txt)
    if not m:
        return None
    inteiro = m.group(1).replace(".", "")
    cent = m.group(2)
    try:
        return float(f"{inteiro}.{cent}")
    except Exception:
        return None

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat, dlon = radians(lat2-lat1), radians(lon2-lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    return R * c

# -----------------------------
# Fetch & parse
# -----------------------------
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

@st.cache_data(show_spinner=False, ttl=600)
def fetch_html(url: str) -> tuple[str, str]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return r.text, r.url
    except Exception:
        return "", ""

def _price_of(offer):
    if not isinstance(offer, dict):
        return None
    if offer.get("price"):
        return cleanup_money(str(offer["price"]))
    ps = offer.get("priceSpecification") or {}
    if isinstance(ps, dict) and ps.get("price"):
        return cleanup_money(str(ps["price"]))
    return None

def _extract_from_ld_node(node):
    out = []
    try:
        typ = node.get("@type", "")
        if isinstance(typ, list):
            typ = " ".join(typ)
        if "Product" in str(typ):
            name = node.get("name") or node.get("description") or ""
            offers = node.get("offers")
            if isinstance(offers, dict):
                price = _price_of(offers)
                if price is not None:
                    out.append({"name": name, "price": price})
            elif isinstance(offers, list):
                for off in offers:
                    price = _price_of(off)
                    if price is not None:
                        out.append({"name": name, "price": price})
        elif "Offer" in str(typ) and node.get("price"):
            p = cleanup_money(str(node.get("price")))
            if p is not None:
                out.append({"name": node.get("name") or "", "price": p})
    except Exception:
        pass
    return out

def extract_jsonld_products(html: str, base_url: str):
    items = []
    if not (EXSTRUCT_OK and html):
        return items
    try:
        data = extruct.extract(html, base_url=get_base_url(html, base_url), syntaxes=['json-ld'])
        blocks = data.get('json-ld', []) if data else []
        for blk in blocks:
            for node in (blk if isinstance(blk, list) else [blk]):
                if not isinstance(node, dict):
                    continue
                if node.get("@graph"):
                    for g in node["@graph"]:
                        items.extend(_extract_from_ld_node(g))
                else:
                    items.extend(_extract_from_ld_node(node))
    except Exception:
        pass
    # dedup (name, price)
    seen, dedup = set(), []
    for it in items:
        key = (it.get("name"), it.get("price"))
        if key not in seen and it.get("price") is not None:
            dedup.append(it)
            seen.add(key)
    return dedup

def extract_name_address_from_jsonld(html: str, base_url: str):
    name, addr = None, None
    if not (EXSTRUCT_OK and html):
        return name, addr
    try:
        data = extruct.extract(html, base_url=get_base_url(html, base_url), syntaxes=['json-ld'])
        blocks = data.get('json-ld', []) if data else []
        for blk in blocks:
            nodes = blk if isinstance(blk, list) else [blk]
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                if n.get("@type") in (["Organization"], "Organization", "Store", "GroceryStore"):
                    name = name or n.get("name")
                    a = n.get("address")
                    if isinstance(a, dict):
                        parts = [a.get(k) for k in ["streetAddress","addressLocality","addressRegion","postalCode","addressCountry"] if a.get(k)]
                        if parts:
                            addr = ", ".join(parts)
                if n.get("@graph"):
                    for g in n["@graph"]:
                        if isinstance(g, dict) and g.get("@type") in ("Organization","Store","GroceryStore"):
                            name = name or g.get("name")
                            a = g.get("address")
                            if isinstance(a, dict):
                                parts = [a.get(k) for k in ["streetAddress","addressLocality","addressRegion","postalCode","addressCountry"] if a.get(k)]
                                if parts:
                                    addr = ", ".join(parts)
    except Exception:
        pass
    return name, addr

def extract_prices_heuristic(html: str):
    soup = BeautifulSoup(html, "lxml")
    texts = soup.find_all(text=True)
    prices = []
    for t in texts:
        p = cleanup_money(str(t))
        if p is not None:
            prices.append({"name": "", "price": p})
        if len(prices) >= 200:
            break
    return prices

def extract_title_site(html: str, url: str):
    soup = BeautifulSoup(html or "", "lxml")
    site = soup.find("meta", property="og:site_name")
    if site and site.get("content"):
        return site["content"]
    title = soup.find("title")
    if title and title.text.strip():
        return title.text.strip()
    return domain_of(url)

def guess_promo_day(text: str):
    if not text:
        return None
    txt = re.sub(r"\s+", " ", text.lower())
    dias = ["segunda","terça","terca","quarta","quinta","sexta","sábado","sabado","domingo"]
    gatilhos = ["promo", "oferta", "dia da", "feira", "quarta da", "terça da", "terca da"]
    found = []
    for d in dias:
        for g in gatilhos:
            if f"{g} {d}" in txt or (f"{d} da " in txt and ("promo" in txt or "oferta" in txt)):
                found.append(d)
    if not found:
        for d in dias:
            if d in txt:
                found.append(d); break
    if not found:
        return None
    m = {"terca":"terça","sabado":"sábado"}
    return m.get(found[0], found[0])

def stats_from_prices(items):
    vals = [x["price"] for x in items if isinstance(x.get("price"), (int,float))]
    if not vals:
        return {"count":0,"min":None,"median":None,"mean":None}
    vals_sorted = sorted(vals)
    n = len(vals_sorted)
    median = vals_sorted[n//2] if n % 2 == 1 else (vals_sorted[n//2-1] + vals_sorted[n//2]) / 2
    mean = sum(vals_sorted) / n
    return {"count":n, "min":min(vals_sorted), "median":median, "mean":mean}

# -----------------------------
# Geocoding
# -----------------------------
@st.cache_data(show_spinner=False, ttl=3600)
def geocode(addr: str):
    try:
        geol = Nominatim(user_agent="super-compare")
        geocode_rl = RateLimiter(geol.geocode, min_delay_seconds=1)
        loc = geocode_rl(addr)
        if loc:
            return float(loc.latitude), float(loc.longitude)
    except Exception:
        pass
    return None

def best_name(fallback_domain, html, url):
    n = extract_title_site(html, url)
    return n or fallback_domain

# -----------------------------
# 5W2H helpers
# -----------------------------
DOW_MAP = {
    "segunda": 0, "terça": 1, "terca":1, "quarta":2,
    "quinta":3, "sexta":4, "sábado":5, "sabado":5, "domingo":6
}

def next_weekday_date(target_name: str):
    if not target_name or target_name.lower() not in DOW_MAP:
        return None
    today = datetime.today()
    target = DOW_MAP[target_name.lower()]
    days_ahead = (target - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return (today + timedelta(days=days_ahead)).date().isoformat()

def build_5w2h(winner, others):
    """Monta um 5W2H com 2–3 linhas para execução."""
    reasons = []
    # compara com o outro mercado
    other = next((o for o in others if o["idx"] != winner["idx"]), None)
    if other:
        # custo
        if winner["stats"]["mean"] is not None and other["stats"]["mean"] is not None:
            if winner["stats"]["mean"] < other["stats"]["mean"]:
                reasons.append("menor preço médio")
            elif winner["stats"]["mean"] == other["stats"]["mean"]:
                reasons.append("preço médio empatado (critério de desempate aplicado)")
        # quantidade de itens
        if (winner["stats"]["count"] or 0) > (other["stats"]["count"] or 0):
            reasons.append("mais itens com preço visível")
        # distância
        if winner["dist_km"] is not None and other["dist_km"] is not None:
            if winner["dist_km"] < other["dist_km"]:
                reasons.append("mais próximo")
    else:
        reasons.append("melhor classificação no comparativo")

    why = "Escolha do mercado por " + ", ".join(reasons) if reasons else "Melhor custo/benefício na análise atual"
    when_date = None
    if winner["promo_day"]:
        when_date = next_weekday_date(winner["promo_day"])
    when_text = when_date or "próxima visita de compras"

    rows = []
    # 1: compra no vencedor
    rows.append({
        "What (o quê)": f"Realizar compras no {winner['name']}",
        "Why (por quê)": why,
        "Where (onde)": winner.get("address") or winner["domain"],
        "When (quando)": when_text,
        "Who (quem)": "Compras",
        "How (como)": f"Utilizar lista do site ({winner['domain']}); conferir ofertas do dia",
        "How much (quanto)": (
            f"Média R$ {winner['stats']['mean']:.2f} | Mín R$ {winner['stats']['min']:.2f} | Itens {winner['stats']['count']}"
            if winner["stats"]["count"] > 0 else "—"
        )
    })
    # 2: opcional - criar lista base de itens essenciais
    rows.append({
        "What (o quê)": "Montar lista comparativa de 10–20 itens essenciais",
        "Why (por quê)": "Acompanhar variação de preço semanal/mensal",
        "Where (onde)": "Planilha/Notas",
        "When (quando)": "Hoje + 1 dia",
        "Who (quem)": "Compras",
        "How (como)": "Registrar preço por item nos dois mercados nas próximas visitas",
        "How much (quanto)": "—"
    })
    # 3: opcional - logística/distância
    rows.append({
        "What (o quê)": "Definir logística (retirada/entrega) e rota",
        "Why (por quê)": "Reduzir tempo/custo de deslocamento",
        "Where (onde)": winner.get("address") or winner["domain"],
        "When (quando)": "Junto da próxima compra",
        "Who (quem)": "Compras",
        "How (como)": "Aplicativo de mapas; janela de entrega mais barata",
        "How much (quanto)": "—"
    })
    df = pd.DataFrame(rows)
    return df

# -----------------------------
# UI
# -----------------------------
col1, col2 = st.columns(2)
with col1:
    url1 = st.text_input("🔗 URL #1", placeholder="https://...")
    addr1_manual = st.text_input("📍 Endereço do supermercado #1 (opcional)")
with col2:
    url2 = st.text_input("🔗 URL #2", placeholder="https://...")
    addr2_manual = st.text_input("📍 Endereço do supermercado #2 (opcional)")

user_addr = st.text_input("📌 Seu endereço/bairro/cidade", placeholder="Av. Exemplo, 123 - Cidade/UF")
go = st.button("Comparar agora")

if go:
    if not url1 or not url2:
        st.error("Manda os dois links, por favor 😉")
        st.stop()

    results = []
    for idx, (u, addr_manual) in enumerate([(url1, addr1_manual), (url2, addr2_manual)], start=1):
        with st.spinner(f"Lendo {domain_of(u)} ..."):
            html, final_url = fetch_html(u)
            if not html:
                st.error(f"Não consegui abrir: {u}")
                continue

            prods = extract_jsonld_products(html, final_url)
            via = "JSON-LD"
            if not prods:
                prods = extract_prices_heuristic(html)
                via = "heurística (pouco confiável)"

            name_ld, addr_ld = extract_name_address_from_jsonld(html, final_url)
            name = name_ld or best_name(domain_of(u), html, final_url)
            addr_store = addr_manual or addr_ld

            soup_txt = BeautifulSoup(html, "lxml").get_text(separator=" ")
            promo_day = guess_promo_day(soup_txt)

            stats = stats_from_prices(prods)

            user_geo = geocode(user_addr) if user_addr.strip() else None
            store_geo = geocode(addr_store) if addr_store else None
            dist_km = None
            if user_geo and store_geo:
                dist_km = round(haversine_km(user_geo[0], user_geo[1], store_geo[0], store_geo[1]), 2)

            results.append({
                "idx": idx,
                "url": u,
                "name": name,
                "domain": domain_of(u),
                "address": addr_store,
                "promo_day": promo_day,
                "via": via,
                "items": prods,
                "stats": stats,
                "dist_km": dist_km
            })

    if not results:
        st.stop()

    c1, c2 = st.columns(2)
    for i, res in enumerate(results):
        with (c1 if i == 0 else c2):
            st.subheader(f"🏬 Supermercado #{res['idx']}: {res['name']}")
            st.write(f"**Domínio:** {res['domain']}")
            st.write(f"**Coleta de preço:** {res['via']}")
            st.write(f"**Itens com preço:** {res['stats']['count']}")
            if res['stats']['count'] > 0:
                st.write(f"- Mín: R$ {res['stats']['min']:.2f}")
                st.write(f"- Mediana: R$ {res['stats']['median']:.2f}")
                st.write(f"- Média: R$ {res['stats']['mean']:.2f}")
            st.write(f"**Dia de promoção (heurística):** {res['promo_day'] or 'não encontrado'}")
            st.write(f"**Endereço:** {res['address'] or 'não identificado'}")
            st.write(f"**Distância até você:** {f'{res['dist_km']} km' if res['dist_km'] is not None else '—'}")

            if res["items"]:
                df_show = pd.DataFrame(res["items"]).rename(columns={"name":"Produto","price":"Preço (R$)"})
                st.dataframe(df_show.head(20), use_container_width=True)
            else:
                st.info("Nenhum preço visível. O site pode carregar via JavaScript ou bloquear scraping.")

    # Decisão
    def decision_key(r):
        mean = r["stats"]["mean"] if r["stats"]["mean"] is not None else math.inf
        count = -(r["stats"]["count"] or 0)
        dist = r["dist_km"] if r["dist_km"] is not None else math.inf
        return (mean, count, dist)

    winner = sorted(results, key=decision_key)[0]

    st.markdown("---")
    if winner["stats"]["count"] > 0:
        st.success(
            f"🏆 **Melhor custo estimado:** {winner['name']} "
            f"(média R$ {winner['stats']['mean']:.2f} | {winner['stats']['count']} itens "
            f"| promoção: {winner['promo_day'] or '—'} | distância: {winner['dist_km']} km)"
        )
    else:
        st.success(
            f"🏆 **Mais acessível pela distância:** {winner['name']} "
            f"(sem preços confiáveis; promoção: {winner['promo_day'] or '—'} | distância: {winner['dist_km']} km)"
        )

    # -----------------------------
    # Plano 5W2H (botão + CSV)
    # -----------------------------
    st.markdown("### 📋 Gerar plano 5W2H do vencedor")
    if st.button("Criar 5W2H"):
        df5 = build_5W2H(winner, results)
        st.dataframe(df5, use_container_width=True)
        csv = df5.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Baixar 5W2H (CSV)", csv, file_name="plano_5w2h_supermercado.csv")

    st.caption(
        "Obs.: Comparação baseada **apenas nos itens visíveis** nessas URLs. "
        "Sites que renderizam via JavaScript ou bloqueiam robôs podem retornar poucos itens. "
        "Para precisão real, prefira APIs oficiais/CSV do mercado."
    )
