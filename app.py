# app.py — Comparador de encartes (PDF/JPG) por supermercado
# v3: detecção forte de nome do mercado, amostra PT-BR enxuta, matching mais tolerante

import sys, subprocess, importlib, io, re, unicodedata
from pathlib import Path

# -------- deps mínimos --------
REQS = [
    ("streamlit","streamlit>=1.34"),
    ("pandas","pandas>=2.0"),
    ("numpy","numpy>=1.26"),
    ("Pillow","Pillow>=10.0"),
    ("pymupdf","pymupdf>=1.23"),      # fitz
    ("rapidfuzz","rapidfuzz>=3.0"),   # fuzzy para juntar nomes
]
def _pip(x):
    try: subprocess.check_call([sys.executable,"-m","pip","install","--quiet",x])
    except Exception: pass
for mod,spec in REQS:
    try: importlib.import_module(mod)
    except Exception: _pip(spec)

import streamlit as st, pandas as pd, numpy as np
from PIL import Image
import fitz  # PyMuPDF
from rapidfuzz import fuzz

# OCR opcional (só se existir)
HAS_TESS = HAS_EASYOCR = False
try:
    import pytesseract; HAS_TESS=True
except Exception: pass
try:
    import easyocr; HAS_EASYOCR=True
except Exception: pass

# -------- helpers --------
def norm_txt(s:str)->str:
    s = unicodedata.normalize("NFD", s or "").lower()
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace() or ch in "-_/.,+")
    return " ".join(s.split())

PRICE_RE = re.compile(r"(?:r\$\s*)?((?:\d{1,3}(?:[\.\s]\d{3})*|\d+)[,\.]\d{2})", re.I)
SIZE_RE  = re.compile(r"(?:(\d{1,3})\s*[xX]\s*)?(\d+(?:[\.,]\d+)?)\s*(kg|g|l|ml)\b")

STOP = {
    "kg","un","unidade","unidades","lt","l","ml","g","gr","gramas","litro","litros",
    "cada","pacote","bandeja","caixa","garrafa","lata","sachê","sache","pct","pcte",
    "ou","e","de","da","do","dos","das","para","pronta","pronto","congelado","resfriado",
    "tipo","sabores","varios","vários","promo","promoção","promocao","oferta","ofertas",
    "clube","economia","leve","pague","leve2","pague1"
}

KNOWN_STORES = [
    # grafias e sinônimos comuns
    "frangolandia","frangolândia","mix mateus","mateus","centerbox",
    "são luiz","sao luiz","carrefour","assai","atacadão","atacadao",
    "super lagoa","pao de acucar","pão de açúcar","guanabara","bh supermercados",
    "fort atacadista","dia","extra","angeloni","mundial","big","macro","comper"
]
NORMALIZE_STORE = {
    "frangolândia":"frangolandia",
    "frangolandia":"frangolandia",
    "mix mateus":"mix mateus",
    "mateus":"mix mateus",          # muitos encartes abreviam
    "sao luiz":"sao luiz","são luiz":"sao luiz",
}

def to_float_price(s:str):
    if not s: return None
    s = s.replace("R$","").replace("r$","").replace(" ","")
    if "," in s and "." in s: s = s.replace(".","").replace(",",".")
    elif "," in s: s = s.replace(",",".")
    try:
        v = float(s); 
        return v if 0 < v < 100000 else None
    except: return None

def parse_size(text:str):
    text = norm_txt(text)
    g=ml=None
    for m in SIZE_RE.finditer(text):
        mult = int(m.group(1)) if m.group(1) else 1
        val  = float(m.group(2).replace(",","."))
        unit = m.group(3)
        if unit=="kg": g  = max(g or 0, val*1000*mult)
        elif unit=="g": g = max(g or 0, val*mult)
        elif unit=="l": ml = max(ml or 0, val*1000*mult)
        elif unit=="ml": ml = max(ml or 0, val*mult)
    return g, ml

def canonical_name(raw:str):
    n = norm_txt(raw)
    n = re.sub(PRICE_RE, " ", n)
    n = n.replace(" r ", " ")
    n = " ".join(w for w in n.split() if w not in STOP and not w.isdigit())
    return n.strip(" -._")

def fuse(a,b,th=88):  # um pouco mais permissivo que antes
    return fuzz.token_set_ratio(a,b) >= th

# -------- leitura + OCR --------
def ocr_image(img:Image.Image)->str:
    if HAS_TESS:
        try: return pytesseract.image_to_string(img, lang="por")
        except Exception: pass
    if HAS_EASYOCR:
        try:
            reader = easyocr.Reader(["pt"], gpu=False)
            txt = "\n".join(reader.readtext(np.array(img), detail=0))
            return txt
        except Exception: pass
    return ""

def pdf_text_and_header(file_bytes:bytes, force_ocr=False):
    """Retorna (texto_total, candidatos_header: [(texto, tamanho_font)])."""
    text_parts=[]; header_spans=[]
    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        for i,pg in enumerate(doc):
            d = pg.get_text("dict")
            page_txt=[]
            # coletar spans com tamanho (pra detectar logo/nome grande)
            for block in d.get("blocks", []):
                for line in block.get("lines", []):
                    for sp in line.get("spans", []):
                        s = sp.get("text","").strip()
                        if s:
                            page_txt.append(s)
                            if i==0 and not any(ch.isdigit() for ch in s) and len(s)<=40:
                                header_spans.append((s, sp.get("size",0)))
            raw = "\n".join(page_txt).strip()

            if (not raw or len(raw)<40) and (force_ocr or True):
                # se vier vazio, tenta OCR da página
                pix = pg.get_pixmap(dpi=230)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                raw = ocr_image(img)

            text_parts.append(raw)
    return "\n".join(text_parts), header_spans

def image_text_and_header(file_bytes:bytes):
    img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    t = ocr_image(img)
    # header = maiores palavras (sem dígitos) nas 10 primeiras linhas
    lines = [ln for ln in (t or "").splitlines() if ln.strip()][:10]
    header = [(ln, 32) for ln in lines if ln and not any(ch.isdigit() for ch in ln)]
    return t, header

# -------- detectar nome de mercado --------
def detect_market_name(full_text:str, header_spans:list, filename:str):
    # 1) procurar por conhecidos (fuzzy) nos spans grandes (size > 20)
    spans = sorted([(s,sz) for (s,sz) in header_spans if sz>=20], key=lambda x:-x[1])[:30]
    cands  = [s for s,_ in spans] + full_text.splitlines()[:120]

    for s in cands:
        n = norm_txt(s)
        best=None; bestscore=0
        for ref in KNOWN_STORES:
            sc = fuzz.token_set_ratio(n, ref)
            if sc>bestscore:
                bestscore=sc; best=ref
        if bestscore>=80:
            return NORMALIZE_STORE.get(best, best)

    # 2) heurística: linha curta, sem número, muitas maiúsculas
    for s,_ in spans[:10]:
        raw = s.strip()
        if 3<=len(raw)<=40 and not any(ch.isdigit() for ch in raw):
            up = sum(1 for c in raw if c.isalpha() and c==c.upper())
            tot= sum(1 for c in raw if c.isalpha())
            if tot>=1 and (up/tot)>0.6:
                return norm_txt(raw)

    # 3) fallback: nome do arquivo
    return norm_txt(Path(filename).stem.replace("_"," ").replace("-"," "))

# -------- parser de produtos --------
def parse_products(text:str):
    items=[]
    if not text: return items
    lines = [ln.strip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        if not ln: continue
        ctx = " ".join([lines[i-1] if i>0 else "", ln, lines[i+1] if i+1<len(lines) else ""])
        for m in PRICE_RE.finditer(ctx):
            price = to_float_price(m.group(1))
            if not price: continue
            left  = ctx[:m.start()].strip()[-150:]
            right = ctx[m.end():].strip()[:90]
            name  = (left + " " + right).strip()
            name  = re.sub(PRICE_RE, " ", name)
            name  = name.strip(" -._")
            if len(name)<3:
                name = re.sub(PRICE_RE, " ", ln).strip(" -._")
            ncan = canonical_name(name)
            if len(ncan) < 4:   # filtra lixo tipo "r", "100g 100g"
                continue
            # descarta nomes que sejam só tamanho
            g,ml = parse_size(name)
            only_size = (len(ncan.replace("g","").replace("ml",""))<3) and (g or ml)
            if only_size: 
                continue
            items.append({"name_raw":name,"price":price})
    # dedup por (nome, preço)
    out=[]; seen=set()
    for it in items:
        k=(it["name_raw"], round(it["price"],2))
        if k in seen: continue
        seen.add(k); out.append(it)
    return out

def build_key(raw:str):
    base = canonical_name(raw)
    return base or raw

def unify_keys(rows, th=86):
    keys = list({r["key"] for r in rows})
    roots=[]; mapping={}
    for k in keys:
        found=None
        for r in roots:
            if fuse(k,r,th):
                found=r; break
        if found is None:
            roots.append(k); mapping[k]=k
        else:
            mapping[k]=found
    return mapping

# -------- comparação --------
def compare_min(all_rows):
    markets = sorted({r["market"] for r in all_rows})
    prods   = sorted({r["key_root"] for r in all_rows})
    table=[]
    for p in prods:
        row={"Produto":p}
        for m in markets:
            vals=[r["price"] for r in all_rows if r["market"]==m and r["key_root"]==p]
            row[m] = min(vals) if vals else np.nan
        table.append(row)
    df = pd.DataFrame(table)
    winners=[]
    for _,r in df.iterrows():
        vals=[(m,r[m]) for m in markets if pd.notna(r[m])]
        if not vals: winners.append(None); continue
        mn=min(v for _,v in vals)
        ws=sorted([m for m,v in vals if v==mn])
        winners.append(ws)
    df["Vencedor(es)"]=winners
    score={m:0 for m in markets}
    for ws in winners:
        if not ws: continue
        if len(ws)==1: score[ws[0]]+=1
        else:
            for m in ws: score[m]+=0.5
    champ=max(score.items(), key=lambda kv:kv[1])[0] if score else None
    return df, score, champ

# -------- UI --------
st.set_page_config(page_title="Comparador de Encartes (PDF/JPG)", layout="wide")
st.title("🧾🛒 Comparador de encartes — quem tem mais preços menores?")

uploads = st.file_uploader("Envie 2+ encartes (PDF/JPG/PNG)", type=["pdf","jpg","jpeg","png"], accept_multiple_files=True)
force_ocr = st.checkbox("Forçar OCR quando o PDF tiver pouco texto (mais lento)", value=False)

if uploads and st.button("Comparar preços"):
    all_rows=[]
    with st.spinner("Lendo encartes, detectando mercados e extraindo preços…"):
        for f in uploads:
            data = f.read()
            ext  = Path(f.name).suffix.lower()
            # texto + header
            if ext==".pdf":
                full_text, header = pdf_text_and_header(data, force_ocr)
            else:
                full_text, header = image_text_and_header(data)

            market = detect_market_name(full_text, header, f.name)
            market = NORMALIZE_STORE.get(market, market)

            # parse produtos
            items = parse_products(full_text)

            rows=[]
            for it in items:
                key = build_key(it["name_raw"])
                rows.append({"market":market,"key":key,"name_raw":it["name_raw"],"price":float(it["price"])})
            # unifica dentro de cada encarte
            m = unify_keys(rows, th=88)
            for r in rows: r["key_root"]=m[r["key"]]
            all_rows.extend(rows)

    if not all_rows:
        st.error("Não consegui achar preços nos encartes. Se forem só-imagem, habilite 'Forçar OCR' e tente novamente.")
        st.stop()

    # unifica ENTRE encartes (mais permissivo pra casar 'mix mateus' com outro encarte)
    cross = unify_keys(all_rows, th=82)
    for r in all_rows: r["key_root"]=cross[r["key_root"]]

    df_all = pd.DataFrame(all_rows)

    # ===== Amostra enxuta PT-BR =====
    amostra = df_all.rename(columns={"market":"Supermercado","name_raw":"Produto","price":"Preço"})[
        ["Supermercado","Produto","Preço"]
    ].copy()
    amostra["Preço"] = amostra["Preço"].map(lambda v: f"R$ {v:.2f}")
    st.subheader("Amostra de itens detectados")
    st.dataframe(amostra.head(60), use_container_width=True)

    # ===== Comparação =====
    comp, placar, vencedor = compare_min(all_rows)

    st.markdown("## 🏁 Resultado")
    if vencedor:
        st.success(f"**Supermercado vencedor:** {vencedor} — mais itens com menor preço.")
    else:
        st.warning("Sem vencedor claro (pouca interseção entre os encartes).")

    st.markdown("### Placar (quantidade de menores preços)")
    st.write(pd.DataFrame([placar]))

    st.markdown("### Tabela comparativa (menores preços por produto)")
    comp_fmt = comp.copy()
    for col in comp.columns:
        if col not in ("Produto","Vencedor(es)"):
            comp_fmt[col]=comp[col].apply(lambda v: f"R$ {v:.2f}" if pd.notna(v) else "—")
    st.dataframe(comp_fmt, use_container_width=True)

    # lista final pedida
    st.markdown("### 🧾 Lista final — produtos e respectivos menores preços")
    linhas=[]
    for _,r in comp.iterrows():
        ws = r["Vencedor(es)"]
        if not ws: continue
        cols=[c for c in comp.columns if c not in ("Produto","Vencedor(es)")]
        mn = np.nanmin([r[c] for c in cols if pd.notna(r[c])])
        linhas.append({"Produto":r["Produto"],"Menor preço":f"R$ {mn:.2f}","Supermercado(s)":", ".join(ws)})
    st.dataframe(pd.DataFrame(linhas), use_container_width=True)

    csv = comp.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Baixar comparação (CSV)", data=csv, file_name="comparacao_encartes.csv", mime="text/csv")
