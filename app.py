# app.py — Comparador de Encartes — v11 (PT-BR 3 colunas fixas + interseção)
import sys, subprocess, importlib, io, os, re, unicodedata, shutil, platform
from pathlib import Path

# --- bootstrap ---
REQS=[("streamlit","streamlit>=1.34"),("pandas","pandas>=2.0"),("numpy","numpy>=1.26"),
      ("Pillow","Pillow>=10.0"),("pymupdf","pymupdf>=1.23"),("rapidfuzz","rapidfuzz>=3.0")]
def pipq(s):
    try: subprocess.check_call([sys.executable,"-m","pip","install","--quiet",s]); return True
    except Exception: return False
for m,s in REQS:
    try: importlib.import_module(m)
    except Exception: pipq(s)

import streamlit as st, pandas as pd, numpy as np
from PIL import Image
import fitz
from rapidfuzz import fuzz

# --- OCR ---
def _try(n):
    try: return importlib.import_module(n)
    except Exception: return None
def _easy():
    if _try("easyocr"): return True
    return pipq("easyocr>=1.7.1") and _try("easyocr")
def _tess():
    ok = _try("pytesseract") or (pipq("pytesseract>=0.3.10") and _try("pytesseract"))
    if not ok: return False
    if shutil.which("tesseract"): return True
    try:
        if "linux" in platform.system().lower():
            subprocess.run(["bash","-lc","sudo apt-get update -y && sudo apt-get install -y tesseract-ocr"],check=False)
    except Exception: pass
    return bool(shutil.which("tesseract"))
OCR_MODE = "easyocr" if _easy() else ("tesseract" if _tess() else None)

def ocr_image(img:Image.Image)->str:
    if OCR_MODE=="easyocr":
        try:
            import easyocr, numpy as np
            return "\n".join(easyocr.Reader(["pt"], gpu=False).readtext(np.array(img), detail=0))
        except Exception: return ""
    if OCR_MODE=="tesseract":
        try:
            import pytesseract
            return pytesseract.image_to_string(img, lang="por")
        except Exception: return ""
    return ""

# --- utils ---
def norm_txt(s:str)->str:
    s = unicodedata.normalize("NFD", s or "").lower()
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace() or ch in "-_/.,+")
    return " ".join(s.split())

# aceita "R$ 1,78", "R$ 1 , 78", "R$\n1,78", "3 . 49"
PRICE_RE = re.compile(r"(?:r\$\s*)?((?:\d{1,3}(?:[\.\s]\d{3})*|\d+)\s*[,.]\s*\d{2})", re.I)

STOP = {"kg","un","unidade","unidades","lt","l","ml","g","gr","gramas","litro","litros",
        "cada","pacote","bandeja","caixa","garrafa","lata","sache","sachê","pct","pcte",
        "ou","e","de","da","do","dos","das","para","congelado","resfriado","tipo","sabores",
        "varios","vários","promo","promoção","promocao","oferta","ofertas","clube",
        "economia","leve","pague","leve2","pague1","rs","r$"}

KNOWN_STORES = [
    "frangolandia","frangolândia",
    "mix mateus","mix matheus","mateus","matheus",
    "centerbox","sao luiz","são luiz","carrefour","assai","atacadão","atacadao",
]
NORMALIZE_STORE = {"frangolândia":"frangolandia","são luiz":"sao luiz",
                   "mateus":"mix mateus","matheus":"mix mateus","mix matheus":"mix mateus"}
STORE_PATTERNS = [
    (re.compile(r"\bfrango?landia\b", re.I), "frangolandia"),
    (re.compile(r"\bmix\s*mat(e|é|he)us\b", re.I), "mix mateus"),
    (re.compile(r"\bmat(e|é|he)us\b", re.I), "mix mateus"),
]

def to_price(s:str):
    s = (s or "").replace("R$","").replace("r$","")
    s = re.sub(r"\s","",s)
    if "," in s and "." in s: s = s.replace(".","").replace(",",".")
    elif "," in s: s = s.replace(",",".")
    try:
        v=float(s)
        return v if 0 < v < 100000 else None
    except: return None

def canonical(raw:str):
    n = norm_txt(raw)
    n = re.sub(PRICE_RE," ",n)
    n = re.sub(r"^[\-\•\·\–\—\·\*]+","", n)   # tira bullets e hífens no começo
    n = n.replace(" gr "," g ").replace(" litro "," l ").replace(" litros "," l ")
    n = " ".join(w for w in n.split() if w not in STOP and not w.isdigit())
    return n.strip(" -._")

def similar(a,b,th=80):
    return max(fuzz.token_set_ratio(a,b), fuzz.partial_ratio(a,b)) >= th

# --- leitura rápida 1ª página (detecção de nome) ---
def first_page_text(bts:bytes, is_pdf:bool):
    if is_pdf:
        with fitz.open(stream=bts, filetype="pdf") as doc:
            pg = doc[0]
            d = pg.get_text("dict")
            page=[]; spans=[]
            for block in d.get("blocks", []):
                for line in block.get("lines", []):
                    for sp in line.get("spans", []):
                        s = sp.get("text","").strip()
                        if not s: continue
                        page.append(s)
                        if len(s)<=64: spans.append((s, sp.get("size",0)))
            raw="\n".join(page).strip()
            if len(raw)<15:
                pix=pg.get_pixmap(dpi=230)
                img=Image.frombytes("RGB",[pix.width,pix.height],pix.samples)
                raw=ocr_image(img)
            return raw, spans
    else:
        img = Image.open(io.BytesIO(bts)).convert("RGB")
        t = ocr_image(img)
        lines=[ln for ln in (t or "").splitlines() if ln.strip()][:18]
        return t, [(ln,32) for ln in lines if ln]

def detect_market(text:str, header_spans:list, filename:str):
    nfull = norm_txt(text or "")
    for rx,label in STORE_PATTERNS:
        if rx.search(nfull): return label
    spans = sorted([(s,sz) for s,sz in (header_spans or []) if sz>=18], key=lambda x:-x[1])[:40]
    cands = [s for s,_ in spans] + (text.splitlines()[:120] if text else [])
    best=None; score=0
    for s in cands:
        n=norm_txt(s)
        for ref in KNOWN_STORES:
            sc=fuzz.token_set_ratio(n,ref)
            if sc>score: score=sc; best=ref
    if score>=80: return NORMALIZE_STORE.get(best,best)
    stem = norm_txt(Path(filename).stem.replace("_"," ").replace("-"," "))
    if stem:
        for rx,label in STORE_PATTERNS:
            if rx.search(stem): return label
        best=None; score=0
        for ref in KNOWN_STORES:
            sc=fuzz.token_set_ratio(stem,ref)
            if sc>score: score=sc; best=ref
        if score>=82: return NORMALIZE_STORE.get(best,best)
    return (stem or "desconhecido").strip()

# --- parsing completo (todas as páginas) ---
def full_text_from_pdf(data:bytes)->str:
    parts=[]
    with fitz.open(stream=data, filetype="pdf") as doc:
        for pg in doc:
            t = pg.get_text()
            if t and len(t.strip())>=3:
                parts.append(t)
            else:
                pix=pg.get_pixmap(dpi=230)
                img=Image.frombytes("RGB",[pix.width,pix.height],pix.samples)
                parts.append(ocr_image(img))
    return "\n".join(parts)

def parse_items(full_text:str):
    if not full_text: return []
    lines=[ln.strip() for ln in full_text.splitlines()]
    out=[]
    for i,ln in enumerate(lines):
        if not ln: continue
        ctx=" ".join([
            lines[i-2] if i-2>=0 else "", lines[i-1] if i-1>=0 else "",
            ln,
            lines[i+1] if i+1<len(lines) else "", lines[i+2] if i+2<len(lines) else "",
        ])
        for m in PRICE_RE.finditer(ctx):
            price=to_price(m.group(1))
            if not price: continue
            left=ctx[:m.start()].strip()[-220:]
            right=ctx[m.end():].strip()[:160]
            name=re.sub(PRICE_RE," ", (left+" "+right)).strip(" -._")
            if len(name)<3:
                name=re.sub(PRICE_RE," ",ln).strip(" -._")
            ncan=canonical(name)
            if len(ncan)<4: continue
            if re.fullmatch(r"(?:\d+(?:g|ml|kg|l)\s*){1,3}", ncan): continue
            out.append({"name_raw":name, "price":float(price)})
    seen=set(); dedup=[]
    for it in out:
        k=(it["name_raw"], round(it["price"],2))
        if k in seen: continue
        seen.add(k); dedup.append(it)
    return dedup

def build_key(raw:str):
    base=canonical(raw)
    base=re.sub(r"\b(\d+(?:g|ml|kg|l))\b(?:\s+\1\b)+","\\1", base)
    return base or raw

def unify_keys(keys, th=78):
    roots=[]; mp={}
    for k in keys:
        hit=None
        for r in roots:
            if similar(k,r,th): hit=r; break
        if hit is None: roots.append(k); mp[k]=k
        else: mp[k]=hit
    return mp

def compare(all_rows):
    markets=sorted({r["market"] for r in all_rows})
    prods=sorted({r["key_root"] for r in all_rows})
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

# ================= UI =================
st.set_page_config(page_title="Comparador de Encartes", layout="wide")
st.title("🧾🛒 Comparador de encartes — menor preço por supermercado")

uploads = st.file_uploader(
    "Envie 2+ encartes (PDF/JPG/PNG). O nome do supermercado é detectado automaticamente (editável).",
    type=["pdf","jpg","jpeg","png"], accept_multiple_files=True
)

detected_names = {}
if uploads:
    st.subheader("Nomeie cada encarte (supermercado)")
    for i, f in enumerate(uploads):
        data = f.getvalue() if hasattr(f,"getvalue") else f.read()
        is_pdf = Path(f.name).suffix.lower()==".pdf"
        prev_text, prev_header = first_page_text(data, is_pdf)
        detected = NORMALIZE_STORE.get(detect_market(prev_text, prev_header, f.name), None) or detect_market(prev_text, prev_header, f.name)
        key = f"market_name_{i}"
        if key not in st.session_state: st.session_state[key]=detected
        detected_names[f.name] = st.text_input(f"Nome do mercado para {f.name}", value=st.session_state[key], key=key)

if uploads and st.button("Comparar preços"):
    all_rows=[]; mercados_detectados=[]
    por_mercado_contagem={}
    for i, f in enumerate(uploads):
        data = f.getvalue() if hasattr(f,"getvalue") else f.read()
        ext = Path(f.name).suffix.lower()
        if ext==".pdf": text = full_text_from_pdf(data)
        else:
            img=Image.open(io.BytesIO(data)).convert("RGB")
            text=ocr_image(img)
        market = st.session_state.get(f"market_name_{i}") or detected_names.get(f.name) or detect_market(text, [], f.name)
        market = NORMALIZE_STORE.get(market, market)
        mercados_detectados.append(f"• {Path(f.name).name} → **{market}**")

        items = parse_items(text)
        por_mercado_contagem[market] = por_mercado_contagem.get(market,0) + len(items)
        if not items: continue

        rows=[]
        for it in items:
            key=build_key(it["name_raw"])
            rows.append({"market":market,"key":key,"name_raw":it["name_raw"],"price":it["price"]})
        mp_in = unify_keys({r["key"] for r in rows}, th=84)
        for r in rows: r["key_root"]=mp_in[r["key"]]
        all_rows.extend(rows)

    if not all_rows:
        st.error("Nada detectado. Se OCR não instalou, use PDF com texto embutido.")
        st.stop()

    cross = unify_keys({r["key_root"] for r in all_rows}, th=72)
    for r in all_rows: r["key_root"]=cross[r["key_root"]]

    st.subheader("Mercados detectados")
    st.markdown("\n".join(mercados_detectados))

    # ---------- AMOSTRA: APENAS 3 COLUNAS (PT-BR) ----------
    df_all=pd.DataFrame(all_rows)
    amostra = df_all.rename(columns={"market":"Supermercado","name_raw":"Produto","price":"Preço"})[
        ["Supermercado","Produto","Preço"]
    ].copy()
    amostra["Preço"]=amostra["Preço"].map(lambda v: f"R$ {float(v):.2f}")
    st.subheader("Amostra de itens detectados (PT-BR: 3 colunas)")
    st.dataframe(amostra.head(120), use_container_width=True)

    # ---------- Diagnóstico rápido ----------
    st.caption("📊 Itens extraídos por mercado")
    st.write(pd.DataFrame([por_mercado_contagem]))

    intersec = df_all.groupby(["key_root","market"]).size().unstack(fill_value=0)
    inter_count = (intersec>0).sum(axis=1)
    st.caption(f"🔗 Produtos em comum entre mercados: {int((inter_count>=2).sum())}")

    # ---------- Comparação ----------
    comp, placar, vencedor = compare(all_rows)
    st.markdown("## 🏁 Resultado")
    if vencedor: st.success(f"**Supermercado vencedor:** {vencedor}")
    else: st.warning("Sem vencedor claro (baixa interseção).")

    st.markdown("### Placar (quantidade de menores preços)")
    st.write(pd.DataFrame([placar]))

    st.markdown("### Tabela comparativa (menores preços por produto)")
    comp_fmt=comp.copy()
    for c in comp.columns:
        if c not in ("Produto","Vencedor(es)"):
            comp_fmt[c]=comp[c].apply(lambda v: f"R$ {v:.2f}" if pd.notna(v) else "—")
    st.dataframe(comp_fmt, use_container_width=True)

    st.markdown("### 🧾 Lista final — produto, menor preço e supermercado(s)")
    linhas=[]
    for _,r in comp.iterrows():
        ws=r["Vencedor(es)"]; 
        if not ws: continue
        cols=[c for c in comp.columns if c not in ("Produto","Vencedor(es)")]
        valores=[r[c] for c in cols if pd.notna(r[c])]
        if not valores: continue
        mn=float(min(valores))
        linhas.append({"Produto":r["Produto"],"Menor preço":f"R$ {mn:.2f}","Supermercado(s)":", ".join(ws)})
    st.dataframe(pd.DataFrame(linhas), use_container_width=True)
