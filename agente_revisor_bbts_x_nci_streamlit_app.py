"""
Agente Revisor de Planilhas BBTS x NCI – Streamlit App (com fallback CLI e testes)

Este módulo foi refatorado para **não quebrar** quando `streamlit` não estiver instalado.
- Se `streamlit` estiver disponível, a **UI do Streamlit** é carregada normalmente.
- Se `streamlit` não estiver disponível (ambientes sandbox/CI), o módulo **não importa `streamlit`** e disponibiliza:
  - **funções de core** para leitura/normalização/comparação e exportação;
  - **CLI de demonstração** (`python agente_revisor.py --demo`) que executa um fluxo mínimo em memória;
  - **testes unitários** embutidos (`python agente_revisor.py --test`) para garantir a estabilidade do core.

Requisitos (para produção com Streamlit):
- streamlit, pandas, numpy, openpyxl (leitura .xlsx), xlsxwriter (escrita .xlsx)
- opcional: reportlab (PDF)

Observação: Para escrever .xlsx usamos XlsxWriter. Para LER .xlsx, o pandas usa openpyxl.
"""

from __future__ import annotations
import io
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Tuple, Optional

# Dependências principais
import pandas as pd
import numpy as np

# ------------------------------------------------------------------
# Streamlit (opcional). Se indisponível, seguimos sem UI.
# ------------------------------------------------------------------
try:
    import streamlit as st  # type: ignore
    STREAMLIT_AVAILABLE = True
except Exception:  # ModuleNotFoundError ou outros
    st = None  # type: ignore
    STREAMLIT_AVAILABLE = False

# ------------------------------------------------------------------
# PDF (opcional)
# ------------------------------------------------------------------
try:
    from reportlab.lib.pagesizes import A4  # type: ignore
    from reportlab.pdfgen import canvas  # type: ignore
    REPORTLAB_OK = True
except Exception:
    REPORTLAB_OK = False

APP_TITLE = "Agente Revisor de Planilhas BBTS x NCI"

# =============================
# Utilitários de IO e parsing
# =============================

def _bytesio_copy(uploaded_file) -> io.BytesIO:
    """Copia o arquivo enviado (UploadedFile) para BytesIO novo e resetado."""
    data = uploaded_file.read()
    bio = io.BytesIO(data)
    bio.seek(0)
    return bio


def listar_sheets_xlsx(bio: io.BytesIO) -> List[str]:
    try:
        xls = pd.ExcelFile(bio)
        return xls.sheet_names
    except Exception:
        return []


def ler_tabela(
    uploaded_file,
    tipo: str,
    sheet: Optional[str],
    sep: Optional[str],
    encoding: Optional[str],
) -> pd.DataFrame:
    """Lê CSV ou XLSX com opções de sheet/sep/encoding e retorna DataFrame."""
    bio = _bytesio_copy(uploaded_file)
    ext = (uploaded_file.name.split(".")[-1] or "").lower()
    if ext in ("xlsx", "xlsm", "xltx", "xltm"):
        try:
            df = pd.read_excel(bio, sheet_name=sheet if sheet else 0, dtype=str)
        except Exception as e:
            raise RuntimeError(f"Erro lendo Excel ({tipo}): {e}")
    elif ext in ("csv",):
        sep_eff = sep or ","
        enc_eff = encoding or "utf-8"
        try:
            df = pd.read_csv(bio, sep=sep_eff, encoding=enc_eff, dtype=str)
        except Exception as e:
            raise RuntimeError(f"Erro lendo CSV ({tipo}): {e}")
    else:
        raise RuntimeError(f"Formato não suportado para {tipo}: .{ext}")

    # Remove colunas totalmente vazias e aparas brancos
    df = df.dropna(how="all", axis=1)
    df = df.applymap(lambda x: x.strip() if isinstance(x, str) else x)
    return df

# =============================
# Normalização e validação
# =============================

def normalizar_nf(valor) -> str:
    """Normaliza Número da NF-e mantendo apenas dígitos; preserva como string.
    Se não houver dígitos, retorna a string aparada.
    """
    if pd.isna(valor):
        return ""
    s = str(valor)
    dig = re.sub(r"\D+", "", s)
    return dig or s.strip()


def normalizar_cfop(valor) -> str:
    if pd.isna(valor):
        return ""
    s = str(valor).strip()
    s = re.sub(r"[^0-9]", "", s)
    return s


def detectar_duplicatas(df: pd.DataFrame, col: str) -> pd.DataFrame:
    mask = df[col].duplicated(keep=False)
    dups = df.loc[mask].copy()
    return dups.sort_values(by=[col])

# =============================
# Preparação e comparação
# =============================

def preparar_df(
    df: pd.DataFrame,
    mapeamento: Dict[str, str],
    origem: str,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Seleciona e renomeia colunas, cria chaves normalizadas e retorna (df_norm, duplicatas)."""
    req = ["data", "numero", "cfop", "valor"]
    faltantes = [k for k in req if k not in mapeamento or mapeamento[k] not in df.columns]
    if faltantes:
        raise ValueError(
            f"Mapeamento incompleto para {origem}. Faltando: {', '.join(faltantes)}"
        )

    df2 = df[[mapeamento["data"], mapeamento["numero"], mapeamento["cfop"], mapeamento["valor"]]].copy()
    df2.columns = ["Data", "Numero_NFe", "CFOP", "Valor_Contabil"]

    # Normalizações
    df2["NF_KEY"] = df2["Numero_NFe"].map(normalizar_nf)
    df2["CFOP_N"] = df2["CFOP"].map(normalizar_cfop)

    # Duplicatas por NF_KEY
    dups = detectar_duplicatas(df2, "NF_KEY")

    return df2, dups


def comparar_bbts_nci(df_bbts: pd.DataFrame, df_nci: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """Gera resultados: ausentes (em cada base) e CFOP divergente."""
    set_b = set(df_bbts["NF_KEY"]) - {""}
    set_n = set(df_nci["NF_KEY"]) - {""}

    # Ausentes
    bbts_somente_keys = sorted(set_b - set_n)
    nci_somente_keys = sorted(set_n - set_b)

    df_bbts_somente = df_bbts[df_bbts["NF_KEY"].isin(bbts_somente_keys)].copy()
    df_nci_somente = df_nci[df_nci["NF_KEY"].isin(nci_somente_keys)].copy()

    # CFOP divergente (apenas onde ambas possuem a NF)
    comum_b = df_bbts[df_bbts["NF_KEY"].isin(set_b & set_n)][["NF_KEY", "CFOP_N", "CFOP", "Data", "Valor_Contabil"]]
    comum_n = df_nci[df_nci["NF_KEY"].isin(set_b & set_n)][["NF_KEY", "CFOP_N", "CFOP", "Data", "Valor_Contabil"]]
    comum_b = comum_b.rename(columns={"CFOP_N": "CFOP_N_BBTS", "CFOP": "CFOP_BBTS", "Data": "Data_BBTS", "Valor_Contabil": "Valor_BBTS"})
    comum_n = comum_n.rename(columns={"CFOP_N": "CFOP_N_NCI", "CFOP": "CFOP_NCI", "Data": "Data_NCI", "Valor_Contabil": "Valor_NCI"})

    cfop_merge = pd.merge(comum_b, comum_n, on="NF_KEY", how="inner")
    cfop_div = cfop_merge[cfop_merge["CFOP_N_BBTS"] != cfop_merge["CFOP_N_NCI"]].copy()

    return {
        "bbts_somente": df_bbts_somente,
        "nci_somente": df_nci_somente,
        "cfop_divergente": cfop_div,
    }

# =============================
# Relatórios
# =============================

def resumo_interpretativo(resultados: Dict[str, pd.DataFrame], dups_b: pd.DataFrame, dups_n: pd.DataFrame) -> str:
    qt_bbts = len(resultados["bbts_somente"]) if not resultados["bbts_somente"].empty else 0
    qt_nci = len(resultados["nci_somente"]) if not resultados["nci_somente"].empty else 0
    qt_cfop = len(resultados["cfop_divergente"]) if not resultados["cfop_divergente"].empty else 0
    qt_db = len(dups_b)
    qt_dn = len(dups_n)

    partes = [
        f"Foram identificadas {qt_bbts} NF-e presentes apenas na base BBTS e {qt_nci} presentes apenas na base NCI.",
        f"Detectaram-se {qt_cfop} ocorrências de CFOP divergente para notas existentes em ambas as bases.",
    ]
    if qt_db or qt_dn:
        partes.append(
            f"Há {qt_db} registros duplicados por número de NF-e na base BBTS e {qt_dn} na base NCI, os quais podem impactar a reconciliação."
        )
    if qt_bbts or qt_nci or qt_cfop:
        partes.append(
            "Recomenda-se priorizar a regularização das notas ausentes e, em seguida, revisar os CFOPs em desacordo, verificando a natureza da operação e a documentação suporte."
        )
    else:
        partes.append("Não foram encontradas divergências relevantes entre as bases analisadas.")

    return " ".join(partes)


def gerar_excel_consolidado(
    resultados: Dict[str, pd.DataFrame],
    df_bbts: pd.DataFrame,
    df_nci: pd.DataFrame,
    dups_b: pd.DataFrame,
    dups_n: pd.DataFrame,
    mapeamentos: Dict[str, Dict[str, str]],
    logs: List[str],
) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
        # Resumo em uma sheet
        resumo_df = pd.DataFrame({
            "Item": [
                "BBTS somente", "NCI somente", "CFOP divergente",
                "Duplicatas BBTS", "Duplicatas NCI", "Data/Hora Geração"
            ],
            "Quantidade": [
                len(resultados["bbts_somente"]),
                len(resultados["nci_somente"]),
                len(resultados["cfop_divergente"]),
                len(dups_b),
                len(dups_n),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ],
        })
        resumo_df.to_excel(writer, sheet_name="Resumo", index=False)

        # Tabelas
        resultados["bbts_somente"].to_excel(writer, sheet_name="BBTS_somente", index=False)
        resultados["nci_somente"].to_excel(writer, sheet_name="NCI_somente", index=False)
        resultados["cfop_divergente"].to_excel(writer, sheet_name="CFOP_divergente", index=False)
        dups_b.to_excel(writer, sheet_name="Duplicatas_BBTS", index=False)
        dups_n.to_excel(writer, sheet_name="Duplicatas_NCI", index=False)

        # Mapeamentos e Logs
        map_rows = []
        for origem, mp in mapeamentos.items():
            for k, v in mp.items():
                map_rows.append({"Origem": origem, "Campo_Padrão": k, "Coluna_Origem": v})
        pd.DataFrame(map_rows).to_excel(writer, sheet_name="Mapeamento_Colunas", index=False)
        pd.DataFrame({"Log": logs}).to_excel(writer, sheet_name="Logs", index=False)

    buf.seek(0)
    return buf.getvalue()


def gerar_pdf_resumo(
    resultados: Dict[str, pd.DataFrame], dups_b: pd.DataFrame, dups_n: pd.DataFrame
) -> bytes:
    if not REPORTLAB_OK:
        raise RuntimeError("Biblioteca reportlab não disponível no ambiente.")

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    titulo = APP_TITLE
    from reportlab.pdfbase.pdfmetrics import stringWidth  # type: ignore
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, height - 50, titulo)

    c.setFont("Helvetica", 10)
    y = height - 90

    def line(text):
        nonlocal y
        c.drawString(40, y, text)
        y -= 16

    line(datetime.now().strftime("Gerado em %Y-%m-%d %H:%M:%S"))
    line("")

    # Resumo simples
    qt_bbts = len(resultados["bbts_somente"]) if hasattr(resultados["bbts_somente"], "__len__") else 0
    qt_nci = len(resultados["nci_somente"]) if hasattr(resultados["nci_somente"], "__len__") else 0
    qt_cfop = len(resultados["cfop_divergente"]) if hasattr(resultados["cfop_divergente"], "__len__") else 0
    qt_db = len(dups_b) if hasattr(dups_b, "__len__") else 0
    qt_dn = len(dups_n) if hasattr(dups_n, "__len__") else 0

    line(f"BBTS somente: {qt_bbts}")
    line(f"NCI somente: {qt_nci}")
    line(f"CFOP divergente: {qt_cfop}")
    line(f"Duplicatas BBTS: {qt_db}")
    line(f"Duplicatas NCI: {qt_dn}")

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.getvalue()

# =============================
# UI Streamlit (somente se disponível)
# =============================
if STREAMLIT_AVAILABLE:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    st.caption("Revisão automática de divergências entre planilhas de duas fontes (BBTS x NCI)")

    with st.expander("Instruções", expanded=False):
        st.markdown(
            """
            **Fluxo recomendado**
            1) Faça upload das duas bases (BBTS e NCI) em **.xlsx** ou **.csv**.
            2) Selecione a *aba* do Excel (se aplicável) ou o *separador* e *encoding* para CSV.
            3) Mapeie as colunas para os campos padrão: **Data**, **Número NF-e**, **CFOP**, **Valor Contábil**.
            4) Clique em **Processar** para gerar os relatórios de divergência.
            5) Baixe o consolidado em **.xlsx** (e opcionalmente **.pdf** se habilitado).
            """
        )

    st.sidebar.header("Arquivos de Entrada")

    col_up1, col_up2 = st.sidebar.columns(2)
    with col_up1:
        file_bbts = st.file_uploader("BBTS (.xlsx/.csv)", type=["xlsx", "csv"], key="bbts")
    with col_up2:
        file_nci = st.file_uploader("NCI (.xlsx/.csv)", type=["xlsx", "csv"], key="nci")

    # Config CSV
    st.sidebar.subheader("Opções para CSV")
    sep = st.sidebar.selectbox("Separador", options=[",", ";", "\t"], index=0)
    encoding = st.sidebar.selectbox("Encoding", options=["utf-8", "latin-1", "cp1252"], index=0)

    # Seleção de sheet quando Excel
    sheet_bbts: Optional[str] = None
    sheet_nci: Optional[str] = None

    if file_bbts is not None and file_bbts.name.lower().endswith(".xlsx"):
        sheets = listar_sheets_xlsx(_bytesio_copy(file_bbts))
        if sheets:
            sheet_bbts = st.sidebar.selectbox("Sheet BBTS", sheets, index=0)

    if file_nci is not None and file_nci.name.lower().endswith(".xlsx"):
        sheets = listar_sheets_xlsx(_bytesio_copy(file_nci))
        if sheets:
            sheet_nci = st.sidebar.selectbox("Sheet NCI", sheets, index=0)

    btn_processar = st.sidebar.button("Processar", type="primary")

    logs: List[str] = []

    if btn_processar:
        if not file_bbts or not file_nci:
            st.error("Envie os dois arquivos (BBTS e NCI).")
            st.stop()

        try:
            df_b = ler_tabela(
                file_bbts,
                "BBTS",
                sheet_bbts,
                sep if file_bbts.name.endswith(".csv") else None,
                encoding if file_bbts.name.endswith(".csv") else None,
            )
            logs.append("BBTS lido com sucesso")
            df_n = ler_tabela(
                file_nci,
                "NCI",
                sheet_nci,
                sep if file_nci.name.endswith(".csv") else None,
                encoding if file_nci.name.endswith(".csv") else None,
            )
            logs.append("NCI lido com sucesso")
        except Exception as e:
            st.exception(e)
            st.stop()

        st.success("Arquivos carregados.")

        # Mapeamento de colunas
        st.subheader("Mapeamento de Colunas")
        cols_b = df_b.columns.tolist()
        cols_n = df_n.columns.tolist()

        def mapa_ui(prefixo: str, cols: List[str]):
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                data = st.selectbox(f"{prefixo} → Data", options=cols, index=0, key=f"{prefixo}_data")
            with c2:
                numero = st.selectbox(f"{prefixo} → Número NF-e", options=cols, index=min(1, len(cols)-1), key=f"{prefixo}_numero")
            with c3:
                cfop = st.selectbox(f"{prefixo} → CFOP", options=cols, index=min(2, len(cols)-1), key=f"{prefixo}_cfop")
            with c4:
                valor = st.selectbox(f"{prefixo} → Valor Contábil", options=cols, index=min(3, len(cols)-1), key=f"{prefixo}_valor")
            return {"data": data, "numero": numero, "cfop": cfop, "valor": valor}

        st.markdown("**BBTS**")
        mapa_b = mapa_ui("BBTS", cols_b)
        st.markdown("**NCI**")
        mapa_n = mapa_ui("NCI", cols_n)

        # Preparação
        try:
            df_b2, dups_b = preparar_df(df_b, mapa_b, "BBTS")
            logs.append("BBTS normalizado e chaves geradas")
            df_n2, dups_n = preparar_df(df_n, mapa_n, "NCI")
            logs.append("NCI normalizado e chaves geradas")
        except Exception as e:
            st.exception(e)
            st.stop()

        # Comparação
        resultados = comparar_bbts_nci(df_b2, df_n2)
        logs.append("Comparação concluída")

        # Métricas
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("BBTS somente", len(resultados["bbts_somente"]))
        m2.metric("NCI somente", len(resultados["nci_somente"]))
        m3.metric("CFOP divergente", len(resultados["cfop_divergente"]))
        m4.metric("Duplicatas (BBTS/NCI)", f"{len(dups_b)}/{len(dups_n)}")

        # Tabelas
        st.subheader("Resultados")
        st.markdown("**BBTS somente**")
        st.dataframe(resultados["bbts_somente"], use_container_width=True)

        st.markdown("**NCI somente**")
        st.dataframe(resultados["nci_somente"], use_container_width=True)

        st.markdown("**CFOP divergente**")
        st.dataframe(resultados["cfop_divergente"], use_container_width=True)

        with st.expander("Detalhe de duplicatas", expanded=False):
            st.markdown("**Duplicatas BBTS**")
            st.dataframe(dups_b, use_container_width=True)
            st.markdown("**Duplicatas NCI**")
            st.dataframe(dups_n, use_container_width=True)

        # Resumo interpretativo
        st.subheader("Resumo interpretativo")
        resumo_txt = resumo_interpretativo(resultados, dups_b, dups_n)
        st.write(resumo_txt)

        # Exports
        st.subheader("Exportar")
        xlsx_bytes = gerar_excel_consolidado(
            resultados, df_b2, df_n2, dups_b, dups_n,
            mapeamentos={"BBTS": mapa_b, "NCI": mapa_n}, logs=logs
        )
        st.download_button(
            label="Baixar consolidado (.xlsx)",
            data=xlsx_bytes,
            file_name="revisao_bbts_x_nci.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        if REPORTLAB_OK:
            try:
                pdf_bytes = gerar_pdf_resumo(resultados, dups_b, dups_n)
                st.download_button(
                    label="Baixar resumo (.pdf)",
                    data=pdf_bytes,
                    file_name="resumo_revisao_bbts_x_nci.pdf",
                    mime="application/pdf",
                )
            except Exception as e:
                st.warning(f"Falha ao gerar PDF: {e}")
        else:
            st.info(
                "Geração de PDF desabilitada (biblioteca reportlab não instalada). "
                "Para habilitar, adicione 'reportlab' ao requirements.txt."
            )

    else:
        st.info("Carregue os arquivos e clique em **Processar** para iniciar a análise.")

# =============================
# CLI de demonstração e Testes
# =============================
@dataclass
class _DemoDFs:
    bbts: pd.DataFrame
    nci: pd.DataFrame
    map_bbts: Dict[str, str]
    map_nci: Dict[str, str]


def _demo_data() -> _DemoDFs:
    """Cria dataframes sintéticos para demonstração/teste rápido sem Streamlit."""
    bbts = pd.DataFrame(
        {
            "Data": ["2025-07-01", "2025-07-02", "2025-07-03", "2025-07-03"],
            "Numero NF-e": ["1709356", "743153", "743551", "743551"],  # dup intencional
            "CFOP": ["2154", "2154", "5102", "5102"],
            "Valor Contábil": ["1445.06", "22491.95", "100.00", "100.00"],
        }
    )
    nci = pd.DataFrame(
        {
            "data_emissao": ["2025-07-01", "2025-07-04", "2025-07-03"],
            "numero": ["1709356", "999999", "743551"],
            "cfop_codigo": ["2154", "2154", "5103"],  # divergência com BBTS (5102 vs 5103)
            "valor": ["1445.06", "10.00", "100.00"],
        }
    )
    map_b = {"data": "Data", "numero": "Numero NF-e", "cfop": "CFOP", "valor": "Valor Contábil"}
    map_n = {"data": "data_emissao", "numero": "numero", "cfop": "cfop_codigo", "valor": "valor"}
    return _DemoDFs(bbts, nci, map_b, map_n)


def run_demo_cli() -> None:
    demo = _demo_data()
    df_b2, dups_b = preparar_df(demo.bbts, demo.map_bbts, "BBTS")
    df_n2, dups_n = preparar_df(demo.nci, demo.map_nci, "NCI")
    resultados = comparar_bbts_nci(df_b2, df_n2)
    print("=== DEMO / RESUMO ===")
    print(resumo_interpretativo(resultados, dups_b, dups_n))
    print("\nBBTS somente:\n", resultados["bbts_somente"])  # noqa: T201
    print("\nNCI somente:\n", resultados["nci_somente"])   # noqa: T201
    print("\nCFOP divergente:\n", resultados["cfop_divergente"])  # noqa: T201


# -----------------------------
# Testes unitários
# -----------------------------
import unittest

class TestNormalizacao(unittest.TestCase):
    def test_normalizar_nf_digitos(self):
        self.assertEqual(normalizar_nf(" 001.234/567-89 "), "00123456789")
        self.assertEqual(normalizar_nf("ABC"), "ABC")  # sem dígitos
        self.assertEqual(normalizar_nf(None), "")

    def test_normalizar_cfop(self):
        self.assertEqual(normalizar_cfop("5.102"), "5102")
        self.assertEqual(normalizar_cfop("  2154-"), "2154")
        self.assertEqual(normalizar_cfop(None), "")

class TestComparacao(unittest.TestCase):
    def setUp(self):
        demo = _demo_data()
        self.df_b2, self.dups_b = preparar_df(demo.bbts, demo.map_bbts, "BBTS")
        self.df_n2, self.dups_n = preparar_df(demo.nci, demo.map_nci, "NCI")
        self.resultados = comparar_bbts_nci(self.df_b2, self.df_n2)

    def test_ausentes(self):
        # Em demo: NCI tem 999999 que não existe em BBTS; BBTS não tem correspondente
        self.assertIn("999999", set(self.resultados["nci_somente"]["NF_KEY"]))
        # Em demo: BBTS não possui notas exclusivas? (tem sim: nenhuma extra além das comuns)
        # Aqui garantimos que não esteja vazia por engano quando não deveria
        self.assertTrue("1709356" in set(self.df_b2["NF_KEY"]))

    def test_cfop_divergente(self):
        # 743551 existe nas duas bases com CFOP diferente (5102 vs 5103)
        df = self.resultados["cfop_divergente"]
        keys = set(df["NF_KEY"]) if not df.empty else set()
        self.assertIn("743551", keys)

    def test_duplicatas(self):
        # BBTS tem duplicata proposital de 743551
        self.assertGreaterEqual(len(self.dups_b), 2)

class TestPrepararDF(unittest.TestCase):
    def test_mapeamento_incompleto(self):
        demo = _demo_data()
        bad_map = {"data": "Data", "numero": "Numero NF-e", "cfop": "CFOP"}  # faltando valor
        with self.assertRaises(ValueError):
            preparar_df(demo.bbts, bad_map, "BBTS")


def _run_tests():
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    # Saída de código de retorno útil para CI
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    if "--test" in sys.argv:
        _run_tests()
    elif "--demo" in sys.argv:
        run_demo_cli()
    else:
        # Execução direta sem flags: apenas informa modos disponíveis
        print(
            "Módulo carregado. Use:\n"
            "  python agente_revisor.py --test  # roda testes unitários\n"
            "  python agente_revisor.py --demo  # executa demonstração CLI\n"
            "Para UI, execute via: streamlit run agente_revisor.py\n"
        )
