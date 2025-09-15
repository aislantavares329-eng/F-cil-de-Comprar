# app.py — Comparador de Encartes (PDF/JPG) por supermercado — v8
# - Detecta automaticamente "Frangolândia" / "Mix Mateus" (e variações "matheus")
# - Amostra FIXA em PT-BR: Supermercado | Produto | Preço
# - Matching entre encartes mais tolerante (token_set + partial_ratio)
# - OCR automático com fallback silencioso

import sys, subprocess, importlib, io, os, re, unicodedata, shutil, platform
from pathlib import Path

# ------------------- bootstrap leve -------------------
BASE_REQS = [
    ("streamlit","streamlit>=1.34"),
    ("pandas","pandas>=2.0"),
    ("numpy","numpy>=1.26"),
    ("Pillow","Pillow>=10.0"),
    ("pymupdf","pymupdf>=1.23"),
    ("rapidfuzz","rapidfuzz>=3.0"),
]
def pipq(spec):
    try:
        subprocess.check_call([sys.executable,"-m","pip","install","--quiet",spec])
        return True
    except Exception:
        return False

for mod,spec in BASE_REQS:
    try: importlib.import_module(mod)
    except Exception: pipq(spec)

import streamlit as st, pandas as pd, numpy as np
from PIL import Image
import fitz
from rapidfuzz import fuzz

# ------------------- OCR auto -------------------
HAS_EASYOCR = False
HAS_TESS = False
HAS_TESS_BIN = False

def try_import(name):
    try:
        return importlib.import_module(name)
    except Exception:
        return None

def ensure_easyocr():
    global HAS_EASYOCR
    if try_import("easyocr"):
        HAS_EASYOCR = True; return True
    if pipq("easyocr>=1.7.1") and try_import("easyocr"):
        HAS_EASYOCR = True; return True
    return False

def ensure_pytesseract_and_binary():
    global HAS_TESS, HAS_TESS_BIN
    if try_import("pytesseract"):
        HAS_TESS = True
    else:
        if pipq("pytesseract>=0.3.10") and try_import("pytesseract"):
            HAS_TESS = True
    HAS_TESS_BIN = bool(shutil.which("tesseract"))
    if not HAS_TESS_BIN:
        try:
            osname = platform.system().lower()
            if "linux" in osname:
                subprocess.run(["bash","-lc","sudo apt-get update -y && sudo apt-get install -y tesseract-ocr"], check=False)
            elif "darwin" in osname:
                subprocess.run(["bash","-lc","brew install tesseract"], check=False)
            elif "windows" in osname:
                subprocess.run(["powershell","-Command","choco install tesseract -y"], check=False)
        except Exception:
            pass
    HAS_TESS_BIN = bool(shutil.which("tesseract"))
    return HAS_TESS and HAS_TESS_BIN

def ensure_ocr():
    if ensure_easyocr():
        return "easyocr"
    if ensure_pytesseract_and_binary():
        return "tesseract"
    return None

OCR_MODE = ensure_ocr()

# ------------------- utils -------------------
def norm_txt(s:str)->str:
    s = unicodedata.normalize("NFD", s or "").lower()
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace() or ch in "-_/.,+")
    return " ".join(s.split())

# aceita "R$ 1,99" / "1,99" / "1.999,99"
PRICE_RE = re.compile(r"(?:r\$\s*)?((?:\d{1,3}(?:[\.\s]\d{3})*|\d+)[,\.]\d{2})", re.I)

STOP = {
    "kg","un","unidade","unidades","lt","l","ml","g","gr","gramas","litro","litros",
    "cada","pacote","bandeja","caixa","garrafa","lata","sachê","sache","pct","pcte",
    "ou","e","de","da","do","dos","das","para","congelado","resfriado",
    "tipo","sabores","varios","vários","promo","promoção","promocao","oferta","ofertas",
    "clube","economia","leve","pague","leve2","pague1","rs","r$"
}

# nomes conhecidos + normalização (inclui "matheus")
KNOWN_STORES = [
    "frangolandia","frangolândia","mix mateus","mateus","matheus","mix matheus",
    "centerbox","sao luiz","são luiz","carrefour","assai","atacadão","atacadao",
    "super lagoa","pao de acucar","pão de açúcar","guanabara","bh supermercados",
]

NORMALIZE_STORE = {
    "frangolândia":"frangolandia",
    "são luiz":"sao luiz",
    "mateus":"mix mateus",
    "matheus":"mix mateus",
    "mix matheus":"mix mateus",
}

STORE_PATTERNS = [
    (re.compile(r"\bfrango?landia\b", re.I), "frangolandia"),
    (re.compile(r"\bmix\s*mat(e|é)us\b", re.I), "mix mateus"),
    (re.compile(r"\bmat(e|é)us\b", re.I), "mix mateus"),
]

def to_price(s:str):
    if not s: return None
    s = s.replace("R$","").replace("r$","").replace(" ","")
    if "," in s and "." in s: s = s.replace(".","").replace(",",".")
    elif "," in s: s = s.replace(",",".")
    try:
        v = float(s)
        return v if 0 < v < 100000 else None
    except:
        return None

def canonical(raw:str):
    n = norm_txt(raw)
    n = re.sub(PRICE_RE," ",n)
    # normalizações simples de unidade
    n = n.replace(" gr "," g ").replace(" litro "," l ").replace(" litros "," l ")
    n = " ".join(w for w in n.split() if w not in STOP and not w.isdigit())
    return n.strip(" -._")

def similar(a,b,th=80):
    # mais permissivo: token_set e partial_ratio
    s1 = fuzz.token_set_ratio(a,b)
    s2 = fuzz.partial_ratio(a,b)
    return max(s1, s2) >= th

# ------------------- leitura & OCR -------------------
def ocr_image(img:Image.Image)->str:
    if OCR_MODE == "easyocr":
        try:
            import easyocr, numpy as np
            reader = easyocr.Reader(["pt"], gpu=False)
            return "\n".join(reader.readtext(np.array(img), detail=0))
        except Exception:
            return ""
    if OCR_MODE == "tesseract":
        try:
            import pytesseract
            return pytesseract.image_to_string(img, lang="por")
        except Exception:
            return ""
    return ""

def pdf_text_and_header(bts:bytes):
    parts=[]; spans=[]
    with fitz.open(stream=bts, filetype="pdf") as doc:
        for i,pg in enumerate(doc):
            d = pg.get_text("dict")
            page=[]
            for block in d.get("blocks", []):
                for line in block.get("lines", []):
                    for sp in line.get("spans", []):
                        s = sp.get("text","").strip()
                        if not s: continue
                        page.append(s)
                        # cabeçalho mais largo (aceita dígitos e até 64 chars)
                        if i==0 and len(s)<=64:
                            spans.append((s, sp.get("size",0)))
            raw = "\n".join(page).strip()
            if len(raw) < 20:
                # provavelmente imagem: OCR
                pix = pg.get_pixmap(dpi=230)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                raw = ocr_image(img)
            parts.append(raw)
    return "\n".join(parts), spans

def image_text_and_header(bts:bytes):
    img = Image.open(io.BytesIO(bts)).convert("RGB")
    t = ocr_image(img)
    lines = [ln for ln in (t or "").splitlines() if ln.strip()][:15]
    # trata primeiras linhas como "cabeçalho"
    header = [(ln, 32) for ln in lines if ln]
    return t, header

# ------------------- detectar supermercado -------------------
def detect_market(text:str, header_spans:list, filename:str):
    nfull = norm_txt(text or "")

    # 1) padrões fortes no texto inteiro
    for rx, label in STORE_PATTERNS:
        if rx.search(nfull):
            return label

    # 2) analisar spans grandes do 1º página
    spans = sorted([(s,sz) for s,sz in (header_spans or []) if sz>=18], key=lambda x:-x[1])[:40]
    cands = [s for s,_ in spans] + (text.splitlines()[:120] if text else [])
    for s in cands:
        n = norm_txt(s)
        best=None; score=0
        for ref in KNOWN_STORES:
            sc = fuzz.token_set_ratio(n, ref)
            if sc>score: score=sc; best=ref
        if score>=80:
            return NORMALIZE_STORE.get(best, best)

    # 3) nome do arquivo ajuda muito
    stem = norm_txt(Path(filename).stem.replace("_"," ").replace("-"," "))
    if stem:
        for rx, label in STORE_PATTERNS:
            if rx.search(stem):
                return label
        # fuzzy no nome
        best=None; score=0
        for ref in KNOWN_STORES:
            sc=fuzz.token_set_ratio(stem, ref)
            if sc>score: score=sc; best=ref
        if score>=82:
            return NORMALIZE_STORE.get(best, best)

    # 4) se nada, devolve o "melhor palpite" legível
    return (stem or "desconhecido").strip()

# ------------------- parser de itens -------------------
def parse_items(text:str):
    out=[]
    if not text: return out
    lines=[ln.strip() for ln in text.splitlines()]
    for i,ln in enumerate(lines):
        if not ln: continue
        ctx = " ".join([lines[i-1] if i>0 else "", ln, lines[i+1] if i+1<len(lines) else ""])
        for m in PRICE_RE.finditer(ctx):
            price = to_price(m.group(1))
            if not price: continue
            left  = ctx[:m.start()].strip()[-160:]
            right = ctx[m.end():].strip()[:120]
            name  = (left + " " + right).strip()
            name  = re.sub(PRICE_RE," ",name).strip(" -._")
            if len(name)<3:
                name = re.sub(PRICE_RE," ",ln).strip(" -._")
            ncan = canonical(name)
            if len(ncan) < 4:
                continue
            # evita nomes só de medida
            if re.fullmatch(r"(?:\d+(?:g|ml|kg|l)\s*){1,3}", ncan):
                continue
            out.append({"name_raw":name, "price":float(price)})
    # dedup
    dedup=[]; seen=set()
    for it in out:
        k=(it["name_raw"], round(it["price"],2))
        if k in seen: continue
        seen.add(k); dedup.append(it)
    return dedup

def build_key(raw:str):
    base = canonical(raw)
    # remove duplicação "100g 100g"
    base = re.sub(r"\b(\d+(?:g|ml|kg|l))\b(?:\s+\1\b)+","\\1",base)
    return base or raw

# ------------------- unificação -------------------
def unify_keys(keys, th=80):
    roots=[]; mapping={}
    for k in keys:
        found=None
        for r in roots:
            if similar(k,r,th):
                found=r; break
        if not found:
            roots.append(k); mapping[k]=k
        else:
            mapping[k]=found
    return mapping

# ------------------- comparação -------------------
def compare(all_rows):
    markets = sorted({r["market"] for r in all_rows})
    prods   = sorted({r["key_root"] for r in all_rows})
    table=[]
    for p in prods:
        row={"Produto":p}
        for m in markets:
            vals=[r["price"] for r in all_rows if r["market"]==m and r["key_root"]==p]
            row[m]=min(vals) if vals else np.nan
        table.append(row)
    df=pd.DataFrame(table)
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

# ------------------- UI -------------------
st.set_page_config(page_title="Comparador de Encartes", layout="wide")
st.title("🧾🛒 Comparador de Encartes — quem tem mais preços menores?")

uploads = st.file_uploader(
    "Envie **2 ou mais** encartes (PDF/JPG/PNG). O nome do supermercado é detectado automaticamente.",
    type=["pdf","jpg","jpeg","png"], accept_multiple_files=True
)

if uploads and st.button("Comparar preços"):
    all_rows=[]; mercados_detectados=[]
    with st.spinner("Extraindo texto, detectando mercados e coletando preços…"):
        for f in uploads:
            data=f.read()
            ext = Path(f.name).suffix.lower()
            if ext==".pdf":
                text, header = pdf_text_and_header(data)
            else:
                text, header = image_text_and_header(data)

            market = detect_market(text, header, f.name)
            market = NORMALIZE_STORE.get(market, market)
            mercados_detectados.append(f"• {Path(f.name).name} → **{market}**")

            items = parse_items(text)
            if not items:
                continue
            # gera linhas deste encarte
            rows=[]
            for it in items:
                key = build_key(it["name_raw"])
                rows.append({"market":market,"key":key,"name_raw":it["name_raw"],"price":it["price"]})
            # unificação DENTRO do encarte (mais rígida)
            mp_in = unify_keys({r["key"] for r in rows}, th=85)
            for r in rows: r["key_root"]=mp_in[r["key"]]
            all_rows.extend(rows)

    if not all_rows:
        st.error("Não encontrei preços. Se o ambiente bloqueou a instalação de OCR, o PDF precisa ter texto embutido.")
        st.stop()

    # unificação ENTRE encartes (mais permissiva para casar produtos similares)
    cross_map = unify_keys({r["key_root"] for r in all_rows}, th=72)
    for r in all_rows: r["key_root"]=cross_map[r["key_root"]]

    # --- Mercados detectados ---
    st.subheader("Mercados detectados")
    st.markdown("\n".join(mercados_detectados))

    # --- Amostra: EXATAMENTE 3 colunas em PT-BR ---
    df_all=pd.DataFrame(all_rows)
    amostra = df_all.rename(columns={"market":"Supermercado","name_raw":"Produto","price":"Preço"})[
        ["Supermercado","Produto","Preço"]
    ].copy()
    amostra["Preço"]=amostra["Preço"].map(lambda v: f"R$ {float(v):.2f}")
    st.subheader("Amostra de itens detectados")
    st.dataframe(amostra.head(80), use_container_width=True)

    # --- Comparação ---
    comp, placar, vencedor = compare(all_rows)

    st.markdown("## 🏁 Resultado")
    if vencedor:
        st.success(f"**Supermercado vencedor:** {vencedor} — maior quantidade de menores preços.")
    else:
        st.warning("Sem vencedor claro (pouca interseção entre encartes).")

    st.markdown("### Placar (quantidade de menores preços)")
    st.write(pd.DataFrame([placar]))

    st.markdown("### Tabela comparativa (menores preços por produto)")
    comp_fmt = comp.copy()
    for c in comp.columns:
        if c not in ("Produto","Vencedor(es)"):
            comp_fmt[c]=comp[c].apply(lambda v: f"R$ {v:.2f}" if pd.notna(v) else "—")
    st.dataframe(comp_fmt, use_container_width=True)

    st.markdown("### 🧾 Lista final — produto, menor preço e supermercado(s)")
    linhas=[]
    for _,r in comp.iterrows():
        ws=r["Vencedor(es)"]
        if not ws: continue
        cols=[c for c in comp.columns if c not in ("Produto","Vencedor(es)")]
        valores=[r[c] for c in cols if pd.notna(r[c])]
        if not valores: continue
        mn=float(min(valores))
        linhas.append({"Produto":r["Produto"],"Menor preço":f"R$ {mn:.2f}","Supermercado(s)":", ".join(ws)})
    st.dataframe(pd.DataFrame(linhas), use_container_width=True)

    st.download_button(
        "⬇️ Baixar comparação (CSV)",
        data=comp.to_csv(index=False).encode("utf-8"),
        file_name="comparacao_encartes.csv",
        mime="text/csv",
    )
