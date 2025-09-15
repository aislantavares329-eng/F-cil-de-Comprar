# app.py — Comparador de encartes (PDF/JPG) por supermercado
# - Upload de 2+ encartes (PDF/JPG/PNG)
# - Extrai texto (PyMuPDF). Para imagens, tenta OCR (pytesseract ou easyocr, se disponíveis)
# - Heurística para detectar "produto + preço" (R$ 3,59 / 2,99 / 12.99 etc.)
# - Normaliza nomes, une variações e compara por supermercado
# - Vence quem tiver MAIOR QUANTIDADE de produtos com menor preço
# - Mostra o vencedor + lista dos menores preços por produto + tabelas

import sys, subprocess, importlib, io, os, re, unicodedata, math, tempfile
from pathlib import Path

# ---------- bootstrap leve de dependências ----------
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
    import easyocr  # pesado; só cria reader se for usar
    HAS_EASYOCR = True
except Exception:
    pass

# ---------------- utils ----------------
STOP = {
    "kg","un","unidade","unidades","lt","l","ml","g","gr","gramas","litro","litros",
    "cada","pacote","bandeja","caixa","garrafa","lata","sachê","sache","pct","pcte",
    "ou","e","de","da","do","dos","das","para","pronta","pronto","congelado","resfriado",
    "tipo","sabores","vários","varios","pre-frita","pré-frita","integral","tradicional"
}
def norm_txt(s:str)->str:
    s = unicodedata.normalize("NFD", s or "").lower()
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace() or ch in "-_/.,+")
    s = " ".join(s.split())
    return s

PRICE_RE = re.compile(
    r"(?:r\$\s*)?((?:\d{1,3}(?:[\.\s]\d{3})*|\d+)[,\.]\d{2})",
    re.I
)
# também aceitar inteiros tipo "2,99" (já cobre) e "129,90"
# nota: a vírgula/ ponto decimal será normalizada depois

def to_float_price(s:str):
    if not s: return None
    s = s.replace(" ", "")
    s = s.replace("R$", "").replace("r$", "")
    # 1.234,56 -> 1234.56
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        v = float(s)
        if 0 < v < 100000:
            return v
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
    # tira "ruído" comum de encarte
    n = re.sub(r"\b(r|rs)\$?\s*\d+[.,]\d{2}\b", " ", n)
    n = re.sub(r"\bpromo(ção|cao)?\b", " ", n)
    n = " ".join(w for w in n.split() if w not in STOP and not w.isdigit())
    n = " ".join(n.split())
    return n

def fuse_keys(a, b, thresh=90):
    """decide se a e b são o 'mesmo' produto (nome e tamanho próximos)"""
    s = fuzz.token_set_ratio(a, b)
    return s >= thresh

# ---------------- extração de texto ----------------
def pdf_to_text(file_bytes: bytes):
    text_parts=[]
    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        for pg in doc:
            # tenta texto nativo
            t = pg.get_text("text")
            t = t.strip()
            if t:
                text_parts.append(t)
                continue
            # fallback: render + OCR
            pix = pg.get_pixmap(dpi=220)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            text_parts.append(ocr_image(img))
    return "\n".join(text_parts)

def ocr_image(img: Image.Image) -> str:
    if HAS_TESS:
        try:
            return pytesseract.image_to_string(img, lang="por")
        except Exception:
            pass
    if HAS_EASYOCR:
        try:
            reader = easyocr.Reader(["pt"], gpu=False)
            res = reader.readtext(np.array(img), detail=0)
            return "\n".join(res)
        except Exception:
            pass
    return ""  # sem OCR disponível

def image_to_text(file_bytes: bytes):
    try:
        img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    except Exception:
        return ""
    t = ocr_image(img)
    return t

# ---------------- parsing de produtos/preços ----------------
def parse_products(text: str):
    """
    Heurística: vasculha linhas; para cada preço encontrado, captura
    o trecho de nome mais próximo (na mesma linha ou vizinhas).
    Retorna lista de dicts {name, price, size_g, size_ml, line}
    """
    items=[]
    lines = [ln.strip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        if not ln: continue
        # junta uma “linha estendida” com a anterior e a próxima (muito comum no encarte)
        ctx = " ".join([lines[i-1] if i>0 else "", ln, lines[i+1] if i+1<len(lines) else ""])
        for m in PRICE_RE.finditer(ctx):
            rawp = m.group(1)
            price = to_float_price(rawp)
            if not price: continue
            # nome candidato = remove preço do contexto e pega ~8-12 palavras em volta
            left = ctx[:m.start()].strip()[-120:]
            right = ctx[m.end():].strip()[:80]
            # favorece termos do lado esquerdo
            name = (left + " " + right).strip()
            name = re.sub(PRICE_RE, " ", name)
            name = re.sub(r"\b(?:cada|quilo|kg|unidade|un|pacote|bandeja)\b", " ", name, flags=re.I)
            name = " ".join(name.split())
            # política: nome mínimo
            if len(name) < 3: 
                # tenta somente a linha atual
                name = re.sub(PRICE_RE, " ", ln).strip()
            size_g, size_ml = parse_size(ctx)
            items.append({
                "name_raw": name,
                "price": price,
                "size_g": size_g,
                "size_ml": size_ml,
                "line": ln
            })
    # limpeza básica: descarta absurdos e duplicatas idênticas
    dedup=[]
    seen=set()
    for it in items:
        if it["price"]<=0 or it["price"]>10000: 
            continue
        k=(it["name_raw"], round(it["price"],2))
        if k in seen: 
            continue
        seen.add(k); dedup.append(it)
    return dedup

def build_key(name, size_g, size_ml):
    base = canonical_name(name)
    sz=""
    if size_g: sz += f" {int(round(size_g))}g"
    if size_ml: sz += f" {int(round(size_ml))}ml"
    key = (base + sz).strip()
    return key or base or name

def unify_keys(rows, thresh=90):
    """
    Agrupa chaves (nomes canônicos) muito parecidas.
    Retorna mapeamento key->root_key.
    """
    keys = list({r["key"] for r in rows})
    roots=[]
    mapping={}
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

# ---------------- comparação ----------------
def compare_price_table(all_rows):
    """
    all_rows: list de dicts com:
      market, key_root, name_raw, price
    """
    # pivot: produto x supermercado (menor preço de cada)
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
    # vencedores por linha
    winners=[]
    for _,r in df.iterrows():
        vals = [(m, r[m]) for m in markets if pd.notna(r[m])]
        if not vals: 
            winners.append(None); continue
        minp = min(v for _,v in vals)
        ws = sorted([m for m,v in vals if v==minp])
        winners.append(ws)
    df["Vencedor(es)"] = winners
    # placar
    placar={m:0 for m in markets}
    for ws in winners:
        if not ws: continue
        # critério do usuário: ganha quem tiver MAIOR QUANTIDADE de menores preços.
        if len(ws)==1:
            placar[ws[0]] += 1
        else:
            # empate naquela linha: dá 0.5 pra cada pra não enviesar
            for m in ws:
                placar[m] += 0.5
    # vencedor geral
    best = max(placar.items(), key=lambda kv: kv[1])[0] if placar else None
    return df, placar, best

# ---------------- UI ----------------
st.set_page_config(page_title="Comparador de Encartes (PDF/JPG)", layout="wide")
st.title("🧾🛒 Comparador de encartes — PDF/JPG → menor preço por supermercado")

st.markdown("Carregue **2 ou mais** encartes (PDF/JPG/PNG). Dê um nome para cada supermercado.")

uploads = st.file_uploader("Arraste seus encartes aqui (PDF/JPG/PNG)", 
                           type=["pdf","jpg","jpeg","png"], accept_multiple_files=True)

if uploads:
    st.subheader("Nomeie cada encarte (supermercado)")
    market_names={}
    for f in uploads:
        default = Path(f.name).stem[:32]
        market_names[f.name] = st.text_input(f"Nome do mercado para **{f.name}**", value=default)

    if st.button("Comparar preços"):
        all_rows=[]
        with st.spinner("Lendo encartes e extraindo preços…"):
            for f in uploads:
                market = (market_names.get(f.name) or Path(f.name).stem).strip()
                data = f.read()
                # extrai texto
                ext = Path(f.name).suffix.lower()
                if ext==".pdf":
                    text = pdf_to_text(data)
                else:
                    text = image_to_text(data)
                # parse
                items = parse_products(text)
                # monta chaves
                rows=[]
                for it in items:
                    key = build_key(it["name_raw"], it["size_g"], it["size_ml"])
                    rows.append({
                        "market": market,
                        "key": key,
                        "name_raw": it["name_raw"],
                        "price": float(it["price"]),
                    })
                # unifica chaves parecidas DENTRO do encarte (evita duplicados do mesmo item)
                mapping = unify_keys(rows, thresh=92)
                for r in rows:
                    r["key_root"] = mapping[r["key"]]
                all_rows.extend(rows)

        if not all_rows:
            st.error("Não achei preços nos arquivos (se forem só-imagem, instale OCR: *tesseract-ocr* + `pytesseract`, ou `easyocr`).")
            st.stop()

        # unifica chaves parecidas ACROSS encartes (quase mesmo produto entre mercados)
        cross_map = unify_keys(all_rows, thresh=90)
        for r in all_rows:
            r["key_root"] = cross_map[r["key_root"]]

        df_all = pd.DataFrame(all_rows)
        st.write("**Amostra de itens detectados (limpeza + normalização aplicada):**")
        st.dataframe(df_all.head(40), use_container_width=True)

        comp, placar, vencedor = compare_price_table(all_rows)

        st.markdown("## 🏁 Resultado")
        if vencedor:
            st.success(f"**Supermercado vencedor:** {vencedor} — (mais itens com menor preço).")
        else:
            st.warning("Não deu pra decidir um vencedor (pouca interseção entre os encartes).")

        st.markdown("### Placar (quantidade de menores preços)")
        st.write(pd.DataFrame([placar]))

        st.markdown("### Tabela comparativa (menores preços por produto)")
        # formatação amigável de preços
        comp_fmt = comp.copy()
        for col in comp.columns:
            if col not in ("Produto","Vencedor(es)"):
                comp_fmt[col] = comp[col].apply(lambda v: f"R$ {v:.2f}" if pd.notna(v) else "—")
        st.dataframe(comp_fmt, use_container_width=True)

        # lista final pedida: “nome do supermercado com menor preço” + cada produto com preço baixo
        st.markdown("### 🧾 Lista final — produtos e respectivos menores preços")
        linhas=[]
        for _, r in comp.iterrows():
            # pega menor preço e o(s) vencedor(es)
            ws = r["Vencedor(es)"]
            if not ws: 
                continue
            cols = [c for c in comp.columns if c not in ("Produto","Vencedor(es)")]
            # menor preço
            minp = np.nanmin([r[c] for c in cols if pd.notna(r[c])])
            # se houver múltiplos vencedores, lista todos
            linhas.append({
                "Produto": r["Produto"],
                "Menor preço": f"R$ {minp:.2f}",
                "Supermercado(s)": ", ".join(ws)
            })
        st.dataframe(pd.DataFrame(linhas), use_container_width=True)

        # download CSV
        csv = comp.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Baixar comparação (CSV)", data=csv, file_name="comparacao_encartes.csv", mime="text/csv")

else:
    st.info("Carregue os encartes para começar.")
