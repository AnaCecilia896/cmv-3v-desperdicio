"""Dashboard de Desperdício — Grupo 3V (acesso público pros gerentes).

Standalone — não depende de Atlas, Cantucci OS, nem planilhas locais.
Lê só o CSV do Google Forms (público via export) + snapshot de receitas.

Deploy: Streamlit Community Cloud (free).
"""
from __future__ import annotations

import io
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path

import httpx
import pandas as pd
import plotly.express as px
import streamlit as st


# ============================================================
# CONFIG
# ============================================================
SHEET_ID = "1qX36AZptjemPuwzoYq9n3QB7AD3NibhSizLXG9BtFKM"
GID = "2068526568"
URL_CSV = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={GID}"

RECEITAS_CSV = Path(__file__).parent / "data" / "receitas_custo.csv"

UNIDADE_MAP = {
    "ASA NORTE": "Cantucci Asa Norte",
    "ASA SUL": "Cantucci Asa Sul",
    "AGUAS CLARAS": "Cantucci Águas Claras",
    "ÁGUAS CLARAS": "Cantucci Águas Claras",
    "MANÉ": "Mané Brasília",
    "MANE": "Mané Brasília",
    "SUPERQUADRA": "Superquadra Norte",
    "SUPERQUADRA NORTE": "Superquadra Norte",
    "KOJI": "Koji",
}

# Cluster de motivos
_CLUSTERS = {
    "🔧 Equipamento": ["GELADEIRA", "FREEZER", "CAMARA", "DESCONGELAMENTO",
                       "DESCONGELOU", "FERMENTOU", "RESISTENCIA", "PRODUTO CONGELOU"],
    "🍽️ Cliente/iFood": ["IFOOD", "CLIENTE", "RECLAMACAO", "RECLAMAÇÃO", "CORTESIA"],
    "📅 Validade": ["VALIDADE", "VENCIDO", "VENCEU", "VENCEND"],
    "👨‍🍳 Erro produção": ["ERRO DE PRODUCAO", "ERRO PRODUCAO", "ERROU", "ERRO DA COZINHA",
                          "PRODUTO ERRADO", "DESCONFIGUROU", "DESCONFIGURADO",
                          "PREPARADO A MAIS", "PREPARO ERRADO"],
    "🍽️ Erro de salão": ["GARCOM", "GARÇOM", "DERRUBOU", "DERRAMOU", "PEDIDO ERRADO"],
    "💥 Acidente": ["CAIU", "QUEBROU", "ACIDENTE", "QUEIMOU"],
    "👃 Cheiro/cor alterada": ["CHEIRO", "COR ALTERADA", "ALTERADO"],
    "🔪 Pré-preparo": ["DESCARTE NORMAL", "DESCARTE DE PROCESSAMENTO", "APARA"],
    "❓ Outro": [],
}

# Conversão pra gramas (pra valorizar com base no rendimento da receita em kg)
_FATOR_GRAMAS = {
    "kg": 1000.0, "kilo": 1000.0, "kilos": 1000.0, "k": 1000.0,
    "g": 1.0, "grama": 1.0, "gramas": 1.0, "gr": 1.0,
    "l": 1000.0, "litro": 1000.0, "litros": 1000.0, "ml": 1.0,
}


def _norm(s: str) -> str:
    if not s:
        return ""
    s = str(s).upper().strip()
    s = "".join(c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()


def _cluster_motivo(motivo: str) -> str:
    if not motivo:
        return "❓ Outro"
    n = _norm(motivo)
    for cluster, kws in _CLUSTERS.items():
        if any(kw in n for kw in kws):
            return cluster
    return "❓ Outro"


# ============================================================
# RECEITAS — pra valorizar
# ============================================================
@st.cache_data
def carregar_receitas() -> dict:
    """Retorna {nome_norm: (custo_unit, unidade_canonica, qty_rendimento)}.
    Custo unit é em R$ por unidade do rendimento (kg, l, und).
    """
    if not RECEITAS_CSV.exists():
        return {}
    df = pd.read_csv(RECEITAS_CSV)
    out = {}
    for _, r in df.iterrows():
        nome = str(r.get("Nome da Receita", "")).strip()
        if not nome:
            continue
        rend = str(r.get("Rendimento", "1 und")).strip().lower()
        # Parse "0,92 KG" → (0.92, "kg")
        m = re.match(r"([\d.,]+)\s*([a-z]*)", rend)
        if not m:
            qty_rend, un_rend = 1.0, "und"
        else:
            try:
                qty_rend = float(m.group(1).replace(",", "."))
            except ValueError:
                qty_rend = 1.0
            un_raw = m.group(2)
            if un_raw in ("kg", "k", "kilo", "kilos"):
                un_rend = "kg"
            elif un_raw in ("g", "gr", "grama", "gramas"):
                un_rend = "g"
            elif un_raw in ("l", "litro", "litros"):
                un_rend = "l"
            elif un_raw == "ml":
                un_rend = "ml"
            else:
                un_rend = "und"
        try:
            custo_unit = float(r.get("Custo Unitário (R$)", 0) or 0)
        except (TypeError, ValueError):
            custo_unit = 0.0
        out[_norm(nome)] = {
            "custo_unit": custo_unit,
            "unidade": un_rend,
            "qty_rend": qty_rend,
        }
    return out


def _qty_pra_unidade_receita(qty: float, qty_unidade: str, un_receita: str) -> float | None:
    """Converte qty_desperdiçada (em qty_unidade) pra unidade da receita."""
    if pd.isna(qty) or qty <= 0:
        return None
    qun = (qty_unidade or "").lower().strip()
    rec = (un_receita or "und").lower()

    # Mesma unidade direto
    if qun == rec:
        return qty
    # Compara em gramas
    fator_qun = _FATOR_GRAMAS.get(qun)
    fator_rec = _FATOR_GRAMAS.get(rec)
    if fator_qun and fator_rec:
        return qty * fator_qun / fator_rec
    # "und" / "unidades" / "unid"
    if qun in ("und", "unidades", "unidade", "unid", "u") and rec in ("und",):
        return qty
    return None


def valorizar_linha(produto: str, qty: float, qty_unidade: str, receitas: dict) -> tuple[float | None, str]:
    """Retorna (valor_rs, status) onde status: 'ok', 'sem_match', 'sem_qty'."""
    if not produto:
        return None, "sem_match"
    n = _norm(produto)
    receita = receitas.get(n)
    if not receita:
        return None, "sem_match"
    qty_conv = _qty_pra_unidade_receita(qty, qty_unidade, receita["unidade"])
    if qty_conv is None:
        return None, "sem_qty"
    return qty_conv * receita["custo_unit"], "ok"


# ============================================================
# CARREGAR DESPERDÍCIO
# ============================================================
@st.cache_data(ttl=600, show_spinner="📥 Carregando dados...")
def carregar_desperdicio() -> pd.DataFrame:
    r = httpx.get(URL_CSV, follow_redirects=True, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))

    # Rename robusto (ordem importa — ver nota no commit anterior)
    REGRAS = [
        ("carimbo", "timestamp"), ("timestamp", "timestamp"),
        ("medida", "qty_unidade"),
        ("observa", "observacao"),
        ("motivo", "motivo"),
        ("quantidade", "qty_raw"),
        ("qual o produto", "produto"),
        ("produto que foi", "produto"),
        ("descartado", "produto"),
        ("unidade", "unidade_raw"),
    ]
    rename = {}
    for c in df.columns:
        norm = _norm(c).lower()
        for kw, target in REGRAS:
            if kw in norm and target not in rename.values():
                rename[c] = target
                break
    df = df.rename(columns=rename)

    df["data"] = pd.to_datetime(df["timestamp"], errors="coerce", dayfirst=True)
    df = df.dropna(subset=["data"])

    df["unidade_norm"] = df["unidade_raw"].apply(_norm)
    df["unidade"] = df["unidade_norm"].map(UNIDADE_MAP).fillna(df["unidade_raw"])
    df["cluster"] = df["motivo"].apply(_cluster_motivo)

    def _parse_qty(v):
        if pd.isna(v):
            return None
        s = str(v).replace(",", ".")
        m = re.search(r"\d+(?:\.\d+)?", s)
        return float(m.group()) if m else None
    df["qty"] = df["qty_raw"].apply(_parse_qty)

    # Valorizar
    receitas = carregar_receitas()
    valor_rs = []
    status = []
    for _, r in df.iterrows():
        v, s = valorizar_linha(r["produto"], r["qty"], r["qty_unidade"], receitas)
        valor_rs.append(v)
        status.append(s)
    df["valor_rs"] = valor_rs
    df["status_valor"] = status

    return df[["data", "unidade", "produto", "qty", "qty_unidade",
               "motivo", "cluster", "observacao", "valor_rs", "status_valor"]].copy()


# ============================================================
# UI
# ============================================================
st.set_page_config(
    page_title="Desperdício · Grupo 3V",
    page_icon="🗑️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
    .block-container {padding-top: 1rem; padding-bottom: 1rem;}
    [data-testid="stMetricValue"] {font-size: 1.8rem !important;}
    h1 {font-size: 1.8rem !important; margin-bottom: 0.5rem;}
    h2, h3 {font-size: 1.2rem !important;}
    @media (max-width: 768px) {
        [data-testid="stMetricValue"] {font-size: 1.4rem !important;}
        h1 {font-size: 1.4rem !important;}
    }
</style>
""", unsafe_allow_html=True)

st.markdown("# 🗑️ Desperdício · Grupo 3V")

with st.sidebar:
    st.markdown("### Controles")
    if st.button("🔄 Atualizar dados", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.caption("Cache de 10 min. Forçar reload no botão acima.")

# Carregar
try:
    df = carregar_desperdicio()
except Exception as e:
    st.error(f"❌ Erro: {e}")
    st.stop()

if df.empty:
    st.warning("Nenhum dado.")
    st.stop()

# ============================================================
# FILTROS
# ============================================================
col1, col2, col3 = st.columns([2, 2, 2])
with col1:
    unidades = ["🌎 Todas"] + sorted(df["unidade"].dropna().unique().tolist())
    unidade_sel = st.selectbox("🏢 Unidade", unidades, index=0)
with col2:
    opcoes_periodo = {
        "📆 Mês atual": "mes_atual",
        "📅 Mês anterior": "mes_anterior",
        "🗓️ Últimos 7 dias": 7,
        "🗓️ Últimos 30 dias": 30,
        "🗓️ Últimos 90 dias": 90,
        "♾️ Tudo": None,
    }
    periodo_sel = st.selectbox("⏱️ Período", list(opcoes_periodo.keys()), index=3)
with col3:
    clusters = ["🌎 Todos"] + sorted(df["cluster"].dropna().unique().tolist())
    cluster_sel = st.selectbox("🏷️ Tipo de perda", clusters, index=0)

# Aplicar
df_f = df.copy()
hoje = date.today()
val = opcoes_periodo[periodo_sel]
if val == "mes_atual":
    df_f = df_f[(df_f["data"].dt.month == hoje.month) & (df_f["data"].dt.year == hoje.year)]
elif val == "mes_anterior":
    mes_ant = hoje.month - 1 or 12
    ano_ant = hoje.year if hoje.month > 1 else hoje.year - 1
    df_f = df_f[(df_f["data"].dt.month == mes_ant) & (df_f["data"].dt.year == ano_ant)]
elif isinstance(val, int):
    df_f = df_f[df_f["data"].dt.date >= hoje - timedelta(days=val)]

if unidade_sel != "🌎 Todas":
    df_f = df_f[df_f["unidade"] == unidade_sel]
if cluster_sel != "🌎 Todos":
    df_f = df_f[df_f["cluster"] == cluster_sel]

if df_f.empty:
    st.info("Sem lançamentos nos filtros selecionados.")
    st.stop()

st.markdown("---")

# ============================================================
# KPIs PRINCIPAIS (igual ao interno)
# ============================================================
total_rs = df_f["valor_rs"].sum(skipna=True) if df_f["valor_rs"].notna().any() else 0
n_lanc = len(df_f)
n_valorizados = int(df_f["valor_rs"].notna().sum())
pct_cobertura = (100 * n_valorizados / n_lanc) if n_lanc else 0
top_cluster = df_f["cluster"].value_counts().index[0] if n_lanc else "—"
n_alertas_eq = int((df_f["cluster"] == "🔧 Equipamento").sum())

col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric(
        "💰 Total desperdiçado",
        f"R$ {total_rs:,.0f}".replace(",", "X").replace(".", ",").replace("X", "."),
        help=f"{n_valorizados} de {n_lanc} lançamentos valorizados ({pct_cobertura:.0f}%)",
    )
with col2:
    st.metric("📋 Nº de lançamentos", f"{n_lanc:,}".replace(",", "."))
with col3:
    st.metric("🎯 Top motivo", top_cluster)
with col4:
    st.metric(
        "⚠️ Alertas equipamento",
        n_alertas_eq,
        help="Lançamentos por falha de equipamento (geladeira, freezer, câmara, forno, etc.)",
    )

if pct_cobertura < 100:
    st.caption(
        f"ℹ️ {n_lanc - n_valorizados} lançamentos sem custo R$ "
        f"(produto não está no snapshot de receitas, ou unidade de medida não bate). "
        f"Total R$ pode estar subestimado."
    )

st.markdown("---")

# ============================================================
# ABAS
# ============================================================
tab1, tab2, tab3, tab4 = st.tabs([
    "🔍 Por motivo", "📦 Top produtos", "📈 Tendência", "📋 Lançamentos"
])

# ---- Tab 1: Cluster ----
with tab1:
    st.markdown("##### Breakdown por motivo")
    ag = (df_f.groupby("cluster")
          .agg(n=("produto", "size"),
               valor_rs=("valor_rs", lambda s: float(s.dropna().sum())))
          .reset_index()
          .sort_values("valor_rs", ascending=False))
    total_motivo = ag["valor_rs"].sum() or 1
    ag["pct_rs"] = (100 * ag["valor_rs"] / total_motivo).round(1)

    col_g, col_t = st.columns([1, 1])
    with col_g:
        # Donut
        fig = px.pie(ag, values="valor_rs", names="cluster", hole=0.55,
                     title=None)
        fig.update_traces(textposition="inside", textinfo="percent")
        fig.update_layout(height=380, margin=dict(t=10, b=10, l=10, r=10),
                          legend=dict(font=dict(size=11)))
        st.plotly_chart(fig, use_container_width=True)
    with col_t:
        ag_show = ag.rename(columns={
            "cluster": "Motivo", "n": "Nº",
            "valor_rs": "R$", "pct_rs": "% do R$",
        })
        ag_show["R$"] = ag_show["R$"].apply(
            lambda v: f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
        ag_show["% do R$"] = ag_show["% do R$"].apply(lambda v: f"{v:.1f}%")
        st.dataframe(ag_show, hide_index=True, use_container_width=True, height=380)

# ---- Tab 2: Top produtos ----
with tab2:
    n_top = st.slider("Mostrar top N produtos", 5, 30, 15)
    ag_p = (df_f.groupby("produto", dropna=False)
            .agg(n=("produto", "size"),
                 valor_rs=("valor_rs", lambda s: float(s.dropna().sum())),
                 motivo_top=("cluster", lambda s: s.value_counts().index[0]))
            .reset_index()
            .sort_values("valor_rs", ascending=False)
            .head(n_top))
    fig = px.bar(ag_p.iloc[::-1], x="valor_rs", y="produto", orientation="h",
                 color="motivo_top", text="valor_rs",
                 labels={"valor_rs": "R$ desperdiçado", "produto": "", "motivo_top": "Motivo"})
    fig.update_traces(texttemplate="R$ %{text:,.0f}",
                      textposition="outside")
    fig.update_layout(height=max(350, 25 * n_top), margin=dict(t=10, b=10, l=10, r=10))
    st.plotly_chart(fig, use_container_width=True)

# ---- Tab 3: Tendência ----
with tab3:
    df_t = df_f.copy()
    df_t["data_d"] = df_t["data"].dt.date
    ag_t = (df_t.groupby("data_d")
            .agg(n=("produto", "size"),
                 valor_rs=("valor_rs", lambda s: float(s.dropna().sum())))
            .reset_index())

    fig = px.bar(ag_t, x="data_d", y="valor_rs", text="valor_rs",
                 labels={"data_d": "Data", "valor_rs": "R$"})
    fig.update_traces(texttemplate="R$ %{text:,.0f}", textposition="outside")
    fig.update_layout(height=350, margin=dict(t=10, b=10, l=10, r=10))
    st.plotly_chart(fig, use_container_width=True)

    # Comparativo unidades (se Todas)
    if unidade_sel == "🌎 Todas":
        df_t2 = (df_t.groupby(["data_d", "unidade"])
                 .agg(valor_rs=("valor_rs", lambda s: float(s.dropna().sum())))
                 .reset_index())
        if not df_t2.empty:
            fig2 = px.line(df_t2, x="data_d", y="valor_rs", color="unidade",
                           markers=True,
                           labels={"data_d": "Data", "valor_rs": "R$", "unidade": "Unidade"})
            fig2.update_layout(height=400, margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig2, use_container_width=True)

# ---- Tab 4: Lançamentos detalhados ----
with tab4:
    busca = st.text_input("🔍 Buscar produto, motivo ou observação",
                           placeholder="Ex: pão, geladeira, cliente")
    df_show = df_f.copy()
    if busca:
        b = _norm(busca)
        mask = (
            df_show["produto"].astype(str).apply(_norm).str.contains(b, na=False) |
            df_show["motivo"].astype(str).apply(_norm).str.contains(b, na=False) |
            df_show["observacao"].astype(str).apply(_norm).str.contains(b, na=False)
        )
        df_show = df_show[mask]

    df_show = df_show.sort_values("data", ascending=False).copy()
    df_show["data"] = df_show["data"].dt.strftime("%d/%m/%Y %H:%M")
    df_show["valor_rs_fmt"] = df_show["valor_rs"].apply(
        lambda v: f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if pd.notna(v) else "—"
    )
    st.dataframe(
        df_show[["data", "unidade", "produto", "qty", "qty_unidade",
                  "valor_rs_fmt", "cluster", "motivo", "observacao"]].rename(columns={
            "data": "Data", "unidade": "Unidade", "produto": "Produto",
            "qty": "Qty", "qty_unidade": "Un.", "valor_rs_fmt": "R$",
            "cluster": "Tipo", "motivo": "Motivo", "observacao": "Observação",
        }),
        hide_index=True,
        use_container_width=True,
        height=500,
    )
    st.caption(f"{len(df_show)} de {len(df_f)} lançamentos.")

# ============================================================
# RODAPÉ
# ============================================================
st.markdown("---")
st.caption(
    f"Total no banco: {len(df):,} lançamentos · ".replace(",", ".") +
    f"Cache 10 min · Receitas valorizadas: {len(carregar_receitas())} no snapshot"
)
