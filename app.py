# app.py — Comparador de encartes (PDF/JPG) por supermercado
# v2: detecção automática do nome do supermercado + amostra PT-BR + match mais robusto

import sys, subprocess, importlib, io, re, unicodedata
from pathlib import Path

# ---------- deps ----------
REQS = [
    ("streamlit", "streamlit>=1.34"),
    ("pandas", "pandas>=2.0"),
    ("numpy", "numpy>=1.26"),
    ("Pillow", "Pillow>=10.0"),
    ("pymupdf", "pymupdf>=1.23"),     # fitz
    ("rapidfuzz", "rapidfuzz>=3.0"),  # fuzzy grouping de nomes
]
def pip_install(spec):
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", spec])
    except Exception:
        pass
for mod, spec in REQS:
    try: importlib.import_module(mod)
    except Exception: pip_install(spec)

import streamlit as st, pandas as pd, numpy as np
from PIL import Image
import fitz  # PyMuPDF
from rapidfuzz import fuzz

# OCR opcional
HAS_TESS = HAS_EASYOCR = False
try:
    import pytesseract
    HAS_TESS = True
except Exception:
    pass
try:
    import easyocr
    HAS_EASYOCR = True
except Exception:
    pass

# ---------- utils ----------
STOP = {
    "kg","un","unidade","unidades","lt","l","ml","g","gr","gramas","litro","litros",
    "cada","pacote","bandeja","caixa","garrafa","lata","sachê","sache","pct","pcte",
    "ou","e","de","da","do","dos","das","para","pronta","pronto","congelado","resfriado",
    "tipo","sabores","vários","varios","pré-frita","pre-frita","integral","tradicional",
    "oferta","ofertas","promo","promoção","promocao","clube","clube de descontos","economia"
}
def norm_txt(s:str)->str:
    s = unicodedata.normalize("NFD", s or "").lower()
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace() or ch in "-_/.,+")
    s = " ".join(s.split())
    return s

PRICE_RE = re.compile(r"(?:r\$\s*)?((?:\d{1,3}(?:[\.\s]\d{3})*|\d+)[,\.]\d{2})", re.I)

def to_float_price(s:str):
    if not s: return None
    s = s.replace(" ", "").replace("R$","").replace("r$","")
    if "," in s and "." in s: s = s.replace(".","").replace(",",".")
    elif "," in s: s = s.replace(",",".")
    try:
        v = float(s)
        if 0 < v < 100000: return v
    except Exception:
        return None
    return None

SIZE_RE = re.compile(r"(?:(\d{1,3})\s*[xX]\s*)?(\d+(?:[\.,]\d+)?)\s*(kg|g|l|ml)\b")
def parse_size(text:str):
    text = norm_txt(text)
    g=None; ml=None
    for m in SIZE_RE.finditer(text):
        mult = int(m.group(1)) if m.group(1) else 1
        val = float(m.group(2).replace(",", "."))
        unit = m.group(3)
        if unit=="kg": g = max(g or 0, val*1000*mult)
        elif unit=="g": g = max(g or 0, val*mult)
        elif unit=="l": ml = max(ml or 0, val*1000*mult)
        elif unit=="ml": ml = max(ml or 0, val*mult)
    return g, ml

def canonical_name(raw:str):
    n = norm_txt(raw)
    n = re.sub(PRICE_RE, " ", n)
    n = n.replace(" r ", " ")
    n = " ".join(w for w in n.split() if w not in STOP and not w.isdigit())
    n = n.strip("-_., ")
    return n

def fuse_keys(a, b, thresh=90):
    return fuzz.token_set_ratio(a, b) >= thresh

# ---------- extração ----------
def pdf_to_text_and_img0(file_bytes: bytes):
    """Retorna texto completo e, para detecção de marca, também o texto da primeira página em alta prioridade."""
    text_parts=[]
    header_text=""
    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        for idx, pg in enumerate(doc):
            t = pg.get_text("text").strip()
            if t:
                text_parts.append(t)
                if idx == 0: header_text = t
                continue
            # fallback OCR
            pix = pg.get_pixmap(dpi=230)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            ocrt = ocr_image(img)
            text_parts.append(ocrt)
            if idx == 0: header_text = ocrt
    return "\n".join(text_parts), header_text

def ocr_image(img: Image.Image) -> str:
    if HAS_TESS:
        try: return pytesseract.image_to_string(img, lang="por")
        except Exception: pass
    if HAS_EASYOCR:
        try:
            reader = easyocr.Reader(["pt"], gpu=False)
            res = reader.readtext(np.array(img), detail=0)
            return "\n".join(res)
        except Exception: pass
    return ""

def image_to_text(file_bytes: bytes):
    try:
        img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    except Exception:
        return "", ""
    t = ocr_image(img)
    return t, t  # usa o mesmo texto como "header"

# ---------- detecção do nome do supermercado ----------
KNOWN_STORES = [
    "frangolandia","frangolândia","mix mateus","mateus","centerbox","são luiz","sao luiz",
    "carrefour","assai","atacadão","atacadao","pao de acucar","pão de açúcar",
    "super lagoa","guanabara","bahamas","mart minas","bh supermercados","comper",
    "cometa","mundial","angeloni","big","macro","fort atacadista","dia","extra"
]
def detect_market_name(full_text:str, header_text:str, filename:str):
    def pick(lines):
        # 1) bate nomes conhecidos
        for ln in lines:
            n = norm_txt(ln)
            for k in KNOWN_STORES:
                if k in n:
                    return k
        # 2) heurística: linha com poucas palavras, quase toda maiúscula e sem números
        best=None; score=0
        for ln in lines:
            raw = ln.strip()
            if len(raw)<3 or len(raw)>40: continue
            if any(ch.isdigit() for ch in raw): continue
            upper_ratio = sum(1 for c in raw if c.isalpha() and c==c.upper()) / max(1,sum(1 for c in raw if c.isalpha()))
            if upper_ratio>0.6:
                s = len(raw)
                if s>score:
                    score=s; best=raw
        if best: return norm_txt(best)
        return None

    lines_header = [ln.strip() for ln in (header_text or "").splitlines() if ln.strip()]
    lines_full   = [ln.strip() for ln in (full_text or "").splitlines()[:150] if ln.strip()]

    name = pick(lines_header) or pick(lines_full)
    if name: 
        # polir: "mix mateus" > "mix mateus", "frangolandia" etc
        return name
    # fallback: nome do arquivo
    return norm_txt(Path(filename).stem.replace("_"," ").replace("-"," "))

# ---------- parsing de produtos/preços ----------
def parse_products(text: str):
    items=[]
    if not text: return items
    lines = [ln.strip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        if not ln: continue
        ctx = " ".join([lines[i-1] if i>0 else "", ln, lines[i+1] if i+1<len(lines) else ""])
        for m in PRICE_RE.finditer(ctx):
            price = to_float_price(m.group(1))
            if not price: continue
            # montar nome candidato
            left = ctx[:m.start()].strip()[-140:]
            right = ctx[m.end():].strip()[:90]
            name = (left + " " + right).strip()
            name = re.sub(PRICE_RE, " ", name)
            name = re.sub(r"\b(?:cada|quilo|kg|unidade|un|pacote|bandeja)\b", " ", name, flags=re.I)
            name = name.strip(" -._")
            # se ficou curto demais, tenta linha pura
            if len(name) < 4:
                name = re.sub(PRICE_RE, " ", ln).strip(" -._")
            # se o "nome" virou só tamanho (ex: "100g 100g"), descarta
            ncan = canonical_name(name)
            size_g, size_ml = parse_size(name)
            only_size = (len(re.sub(r"[^\w]", "", ncan)) < 3) and (size_g or size_ml)
            if only_size: 
                continue
            items.append({
                "name_raw": name,
                "price": price,
                "size_g": size_g,
                "size_ml": size_ml,
                "line": ln
            })
    # dedup simples
    out=[]; seen=set()
    for it in items:
        k=(it["name_raw"], round(it["price"],2))
        if k in seen: continue
        seen.add(k); out.append(it)
    return out

def build_key(name, size_g, size_ml):
    base = canonical_name(name)
    sz=""
    if size_g: sz += f" {int(round(size_g))}g"
    if size_ml: sz += f" {int(round(size_ml))}ml"
    key = (base + sz).strip()
    return key or base or name

def unify_keys(rows, thresh=90):
    keys = list({r["key"] for r in rows})
    roots=[]; mapping={}
    for k in keys:
        found=None
        for r in roots:
            if fuse_keys(k, r, thresh=thresh):
                found=r; break
        if found is None:
            roots.append(k); mapping[k]=k
        else:
            mapping[k]=found
    return mapping

# ---------- comparação ----------
def compare_price_table(all_rows):
    markets = sorted({r["market"] for r in all_rows})
    products = sorted({r["key_root"] for r in all_rows})
    table=[]
    for k in products:
        row={"Produto": k}
        for m in markets:
            vals = [r["price"] for r in all_rows if r["market"]==m and r["key_root"]==k]
            row[m] = min(vals) if vals else np.nan
        table.append(row)
    df = pd.DataFrame(table)
    winners=[]
    for _,r in df.iterrows():
        vals = [(m, r[m]) for m in markets if pd.notna(r[m])]
        if not vals: winners.append(None); continue
        minp = min(v for _,v in vals)
        ws = sorted([m for m,v in vals if v==minp])
        winners.append(ws)
    df["Vencedor(es)"] = winners
    placar={m:0 for m in markets}
    for ws in winners:
        if not ws: continue
        if len(ws)==1: placar[ws[0]] += 1
        else:
            for m in ws: placar[m]+=0.5
    best = max(placar.items(), key=lambda kv: kv[1])[0] if placar else None
    return df, placar, best

# ---------- UI ----------
st.set_page_config(page_title="Comparador de Encartes (PDF/JPG)", layout="wide")
st.title("🧾🛒 Comparador de encartes — menor preço por supermercado")

uploads = st.file_uploader(
    "Envie 2 ou mais encartes (PDF/JPG/PNG). O app detecta o nome do supermercado automaticamente.",
    type=["pdf","jpg","jpeg","png"], accept_multiple_files=True
)

if uploads:
    if st.button("Comparar preços"):
        all_rows=[]
        with st.spinner("Lendo encartes e identificando mercados…"):
            market_of_file={}
            parsed_cache={}
            for f in uploads:
                data = f.read()
                ext = Path(f.name).suffix.lower()
                if ext==".pdf":
                    full_text, header = pdf_to_text_and_img0(data)
                else:
                    full_text, header = image_to_text(data)
                market = detect_market_name(full_text, header, f.name)
                market_of_file[f.name] = market or Path(f.name).stem
                parsed_cache[f.name] = (full_text, header)

        with st.spinner("Extraindo produtos e preços…"):
            for f in uploads:
                market = market_of_file[f.name]
                full_text, _ = parsed_cache[f.name]
                items = parse_products(full_text)

                rows=[]
                for it in items:
                    key = build_key(it["name_raw"], it["size_g"], it["size_ml"])
                    rows.append({
                        "market": market,
                        "key": key,
                        "name_raw": it["name_raw"],
                        "price": float(it["price"]),
                    })
                # unificação DENTRO do encarte (um pouco rígida)
                mapping = unify_keys(rows, thresh=90)
                for r in rows:
                    r["key_root"] = mapping[r["key"]]
                all_rows.extend(rows)

        if not all_rows:
            st.error("Não consegui achar preços nos encartes. Se forem só-imagem, instale OCR (pytesseract ou easyocr).")
            st.stop()

        # unificação ENTRE encartes (mais permissiva — era aqui que travava a comparação)
        cross_map = unify_keys(all_rows, thresh=82)
        for r in all_rows:
            r["key_root"] = cross_map[r["key_root"]]

        df_all = pd.DataFrame(all_rows)

        # ---- Amostra PT-BR: só Supermercado, Produto, Preço ----
        amostra = df_all.rename(columns={
            "market":"Supermercado",
            "name_raw":"Produto",
            "price":"Preço"
        })[["Supermercado","Produto","Preço"]].copy()
        amostra["Preço"] = amostra["Preço"].map(lambda v: f"R$ {v:.2f}")
        st.subheader("Amostra de itens detectados")
        st.dataframe(amostra.head(50), use_container_width=True)

        comp, placar, vencedor = compare_price_table(all_rows)

        st.markdown("## 🏁 Resultado")
        if vencedor:
            st.success(f"**Supermercado vencedor:** {vendedor} — mais itens com menor preço.")
        else:
            st.warning("Sem vencedor claro (pouca interseção entre os encartes).")

        st.markdown("### Placar (quantidade de menores preços)")
        st.write(pd.DataFrame([placar]))

        st.markdown("### Tabela comparativa (menores preços por produto)")
        comp_fmt = comp.copy()
        for col in comp.columns:
            if col not in ("Produto","Vencedor(es)"):
                comp_fmt[col] = comp[col].apply(lambda v: f"R$ {v:.2f}" if pd.notna(v) else "—")
        st.dataframe(comp_fmt, use_container_width=True)

        # Lista final: produto + menor preço + supermercado(s)
        st.markdown("### 🧾 Lista final — produtos e respectivos menores preços")
        linhas=[]
        for _, r in comp.iterrows():
            ws = r["Vencedor(es)"]
            if not ws: continue
            cols = [c for c in comp.columns if c not in ("Produto","Vencedor(es)")]
            minp = np.nanmin([r[c] for c in cols if pd.notna(r[c])])
            linhas.append({
                "Produto": r["Produto"],
                "Menor preço": f"R$ {minp:.2f}",
                "Supermercado(s)": ", ".join(ws)
            })
        st.dataframe(pd.DataFrame(linhas), use_container_width=True)

        csv = comp.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Baixar comparação (CSV)", data=csv, file_name="comparacao_encartes.csv", mime="text/csv")

else:
    st.info("Envie os encartes para começar.")
