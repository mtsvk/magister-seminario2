"""
04_inspect_cmf_sqlite.py

Visor y auditor interactivo para:

    data/database/cmf_carteras_2000_2026.sqlite

Objetivos:
- Inspeccionar tablas y esquemas.
- Navegar filas sin cargar la base completa en memoria.
- Filtrar y buscar registros.
- Revisar cobertura temporal.
- Perfilar columnas.
- Detectar NULL, vacíos, caracteres sospechosos y duplicados.
- Ejecutar consultas SQL de diagnóstico.
- Exportar muestras sospechosas.
- Registrar hallazgos para corregir el pipeline aguas arriba.

IMPORTANTE:
La conexión SQLite se abre en modo READ ONLY.
Este script NO modifica la base de datos.

Ejecución:

    py -m pip install streamlit pandas
    streamlit run 04_inspect_cmf_sqlite.py
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st


# ======================================================================
# CONFIGURACIÓN
# ======================================================================

ROOT = Path(r"C:\Workspace\projects\magister-seminario2")

DB_PATH = (
    ROOT
    / "data"
    / "database"
    / "cmf_carteras_2000_2026.sqlite"
)

EXPORT_DIR = ROOT / "data" / "exports" / "inspection"
LOG_DIR = ROOT / "logs"

FINDINGS_PATH = LOG_DIR / "cmf_sqlite_inspection_findings.csv"

EXPORT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


st.set_page_config(
    page_title="CMF · Inspector SQLite",
    page_icon="🔎",
    layout="wide",
)


# ======================================================================
# UTILIDADES
# ======================================================================

def qident(name: str) -> str:
    """Escapa un identificador SQLite."""
    return '"' + name.replace('"', '""') + '"'


@st.cache_resource
def get_connection() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"No existe la base SQLite:\n{DB_PATH}"
        )

    uri = DB_PATH.resolve().as_uri() + "?mode=ro"

    conn = sqlite3.connect(
        uri,
        uri=True,
        check_same_thread=False,
    )

    conn.execute("PRAGMA query_only = ON;")
    return conn


def scalar(sql: str, params=None):
    conn = get_connection()
    cur = conn.execute(sql, params or [])
    row = cur.fetchone()
    return row[0] if row else None


def query_df(sql: str, params=None) -> pd.DataFrame:
    return pd.read_sql_query(
        sql,
        get_connection(),
        params=params or [],
    )


@st.cache_data(show_spinner=False)
def get_tables() -> list[str]:
    df = query_df(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    )
    return df["name"].tolist()


@st.cache_data(show_spinner=False)
def get_columns(table: str) -> pd.DataFrame:
    return query_df(f"PRAGMA table_info({qident(table)})")


@st.cache_data(show_spinner=False)
def row_count(table: str) -> int:
    return int(
        scalar(
            f"SELECT COUNT(*) FROM {qident(table)}"
        )
    )


def table_has_column(table: str, column: str) -> bool:
    cols = get_columns(table)["name"].tolist()
    return column in cols


def format_int(n) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", ".")


def format_bytes(n: int) -> str:
    value = float(n)

    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024:
            return f"{value:,.2f} {unit}"
        value /= 1024

    return f"{value:,.2f} PB"


def append_finding(
    table: str,
    category: str,
    description: str,
    sql_query: str,
    upstream_action: str,
):
    exists = FINDINGS_PATH.exists()

    with FINDINGS_PATH.open(
        "a",
        newline="",
        encoding="utf-8-sig",
    ) as f:
        writer = csv.writer(f, delimiter=";")

        if not exists:
            writer.writerow(
                [
                    "timestamp_utc",
                    "tabla",
                    "categoria",
                    "descripcion",
                    "consulta_sql",
                    "accion_upstream",
                ]
            )

        writer.writerow(
            [
                datetime.now(timezone.utc).isoformat(),
                table,
                category,
                description,
                sql_query,
                upstream_action,
            ]
        )


# ======================================================================
# FILTROS
# ======================================================================

def build_filter(
    column: str,
    operator: str,
    value: str,
):
    col = qident(column)

    if operator == "=":
        return f"CAST({col} AS TEXT) = ?", [value]

    if operator == "≠":
        return f"CAST({col} AS TEXT) <> ?", [value]

    if operator == "contiene":
        return f"CAST({col} AS TEXT) LIKE ?", [f"%{value}%"]

    if operator == "empieza con":
        return f"CAST({col} AS TEXT) LIKE ?", [f"{value}%"]

    if operator == "termina con":
        return f"CAST({col} AS TEXT) LIKE ?", [f"%{value}"]

    if operator == "NULL":
        return f"{col} IS NULL", []

    if operator == "no NULL":
        return f"{col} IS NOT NULL", []

    if operator == "vacío":
        return f"TRIM(CAST({col} AS TEXT)) = ''", []

    if operator == "no vacío":
        return (
            f"{col} IS NOT NULL "
            f"AND TRIM(CAST({col} AS TEXT)) <> ''",
            [],
        )

    raise ValueError(operator)


# ======================================================================
# CABECERA
# ======================================================================

st.title("🔎 CMF · Inspector SQLite")

st.caption(
    "Auditoría exploratoria de la base histórica de carteras CMF. "
    "La conexión está abierta en modo solo lectura."
)

if not DB_PATH.exists():
    st.error(f"No existe:\n\n{DB_PATH}")
    st.stop()

tables = get_tables()

db_size = DB_PATH.stat().st_size

c1, c2, c3 = st.columns(3)

c1.metric("Tamaño SQLite", format_bytes(db_size))
c2.metric("Tablas", format_int(len(tables)))
c3.metric(
    "Modo",
    "READ ONLY",
)

st.code(str(DB_PATH), language=None)


# ======================================================================
# TABS
# ======================================================================

(
    tab_resumen,
    tab_explorar,
    tab_cobertura,
    tab_perfil,
    tab_calidad,
    tab_sql,
    tab_hallazgos,
) = st.tabs(
    [
        "Resumen",
        "Explorar datos",
        "Cobertura",
        "Perfil columnas",
        "Calidad",
        "SQL",
        "Hallazgos",
    ]
)


# ======================================================================
# 1. RESUMEN
# ======================================================================

with tab_resumen:

    st.subheader("Estructura de la base")

    resumen = []

    for table in tables:
        cols = get_columns(table)

        resumen.append(
            {
                "tabla": table,
                "columnas": len(cols),
            }
        )

    st.dataframe(
        pd.DataFrame(resumen),
        use_container_width=True,
        hide_index=True,
    )

    if st.button(
        "Calcular número de filas por tabla",
        key="count_all_tables",
    ):
        rows = []

        progress = st.progress(0)

        for i, table in enumerate(tables):
            n = row_count(table)

            rows.append(
                {
                    "tabla": table,
                    "filas": n,
                }
            )

            progress.progress(
                (i + 1) / len(tables)
            )

        result = pd.DataFrame(rows)

        result["filas_formateadas"] = (
            result["filas"]
            .map(format_int)
        )

        st.dataframe(
            result,
            use_container_width=True,
            hide_index=True,
        )

        st.bar_chart(
            result.set_index("tabla")["filas"]
        )

    st.divider()

    st.subheader("Chequeo SQLite")

    col1, col2 = st.columns(2)

    if col1.button("PRAGMA quick_check"):
        with st.spinner("Ejecutando quick_check..."):
            result = scalar("PRAGMA quick_check")

        if result == "ok":
            st.success("SQLite quick_check: OK")
        else:
            st.error(str(result))

    if col2.button("PRAGMA integrity_check completo"):
        with st.spinner(
            "Ejecutando integrity_check. Puede tardar..."
        ):
            result = scalar("PRAGMA integrity_check")

        if result == "ok":
            st.success("SQLite integrity_check: OK")
        else:
            st.error(str(result))

    st.divider()

    st.subheader("Esquema de una tabla")

    schema_table = st.selectbox(
        "Tabla",
        tables,
        key="schema_table",
    )

    schema = get_columns(schema_table)

    st.dataframe(
        schema[
            [
                "cid",
                "name",
                "type",
                "notnull",
                "dflt_value",
                "pk",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )


# ======================================================================
# 2. EXPLORAR DATOS
# ======================================================================

with tab_explorar:

    st.subheader("Navegador de registros")

    table = st.selectbox(
        "Tabla",
        tables,
        key="explore_table",
    )

    columns = get_columns(table)["name"].tolist()

    selected_columns = st.multiselect(
        "Columnas a mostrar",
        columns,
        default=columns[: min(12, len(columns))],
        key="explore_columns",
    )

    if not selected_columns:
        selected_columns = columns

    st.markdown("#### Filtros")

    filter_sql = []
    filter_params = []

    operators = [
        "=",
        "≠",
        "contiene",
        "empieza con",
        "termina con",
        "NULL",
        "no NULL",
        "vacío",
        "no vacío",
    ]

    for i in range(3):

        c1, c2, c3 = st.columns(
            [2, 2, 4]
        )

        active = c1.checkbox(
            f"Filtro {i + 1}",
            key=f"filter_active_{i}",
        )

        column = c2.selectbox(
            "Columna",
            columns,
            key=f"filter_column_{i}",
            label_visibility="collapsed",
        )

        operator = c3.selectbox(
            "Operador",
            operators,
            key=f"filter_operator_{i}",
            label_visibility="collapsed",
        )

        value = ""

        if operator not in {
            "NULL",
            "no NULL",
            "vacío",
            "no vacío",
        }:
            value = st.text_input(
                f"Valor filtro {i + 1}",
                key=f"filter_value_{i}",
            )

        if active:
            clause, params = build_filter(
                column,
                operator,
                value,
            )

            filter_sql.append(clause)
            filter_params.extend(params)

    where_clause = ""

    if filter_sql:
        where_clause = (
            " WHERE "
            + " AND ".join(filter_sql)
        )

    st.markdown("#### Orden y paginación")

    c1, c2, c3, c4 = st.columns(4)

    order_col = c1.selectbox(
        "Ordenar por",
        ["(sin orden)"] + columns,
    )

    direction = c2.selectbox(
        "Dirección",
        ["ASC", "DESC"],
    )

    page_size = int(
        c3.selectbox(
            "Filas",
            [50, 100, 250, 500, 1000, 5000],
            index=2,
        )
    )

    offset = int(
        c4.number_input(
            "OFFSET",
            min_value=0,
            value=0,
            step=page_size,
        )
    )

    select_clause = ", ".join(
        qident(c)
        for c in selected_columns
    )

    sql = (
        f"SELECT rowid AS __rowid__, "
        f"{select_clause} "
        f"FROM {qident(table)}"
        f"{where_clause}"
    )

    if order_col != "(sin orden)":
        sql += (
            f" ORDER BY {qident(order_col)} "
            f"{direction}"
        )

    sql += " LIMIT ? OFFSET ?"

    params = (
        filter_params
        + [page_size, offset]
    )

    try:
        df = query_df(sql, params)

        st.caption(
            f"Mostrando {format_int(len(df))} registros "
            f"desde OFFSET {format_int(offset)}."
        )

        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            height=600,
        )

        csv_bytes = df.to_csv(
            index=False,
            sep=";",
        ).encode("utf-8-sig")

        st.download_button(
            "Descargar esta vista como CSV",
            csv_bytes,
            file_name=(
                f"{table}_offset_{offset}.csv"
            ),
            mime="text/csv",
        )

        with st.expander("Ver SQL utilizado"):
            st.code(sql, language="sql")
            st.write("Parámetros:", params)

    except Exception as e:
        st.exception(e)


# ======================================================================
# 3. COBERTURA TEMPORAL
# ======================================================================

with tab_cobertura:

    st.subheader("Cobertura temporal")

    temporal_tables = [
        t
        for t in tables
        if table_has_column(t, "periodo")
    ]

    if not temporal_tables:
        st.warning(
            "No encontré tablas con una columna llamada `periodo`."
        )

    else:
        table = st.selectbox(
            "Tabla",
            temporal_tables,
            key="coverage_table",
        )

        summary = query_df(
            f"""
            SELECT
                MIN(periodo) AS periodo_min,
                MAX(periodo) AS periodo_max,
                COUNT(DISTINCT periodo) AS periodos,
                COUNT(*) AS filas
            FROM {qident(table)}
            """
        )

        c1, c2, c3, c4 = st.columns(4)

        c1.metric(
            "Primer período",
            summary.loc[0, "periodo_min"],
        )

        c2.metric(
            "Último período",
            summary.loc[0, "periodo_max"],
        )

        c3.metric(
            "Períodos",
            format_int(
                summary.loc[0, "periodos"]
            ),
        )

        c4.metric(
            "Filas",
            format_int(
                summary.loc[0, "filas"]
            ),
        )

        coverage = query_df(
            f"""
            SELECT
                periodo,
                COUNT(*) AS filas
            FROM {qident(table)}
            GROUP BY periodo
            ORDER BY periodo
            """
        )

        st.dataframe(
            coverage,
            use_container_width=True,
            hide_index=True,
        )

        if not coverage.empty:
            chart = coverage.copy()
            chart = chart.set_index("periodo")

            st.line_chart(chart["filas"])

        st.markdown("#### Períodos con formato sospechoso")

        invalid_period_sql = f"""
        SELECT
            periodo,
            COUNT(*) AS filas
        FROM {qident(table)}
        WHERE
            periodo IS NULL
            OR LENGTH(TRIM(periodo)) <> 7
            OR SUBSTR(periodo, 5, 1) <> '-'
            OR CAST(SUBSTR(periodo, 6, 2) AS INTEGER)
                NOT BETWEEN 1 AND 12
        GROUP BY periodo
        ORDER BY filas DESC
        LIMIT 500
        """

        invalid = query_df(invalid_period_sql)

        if invalid.empty:
            st.success(
                "No se detectaron períodos con formato evidentemente inválido."
            )
        else:
            st.warning(
                f"Se detectaron {len(invalid)} valores sospechosos."
            )
            st.dataframe(
                invalid,
                use_container_width=True,
                hide_index=True,
            )


# ======================================================================
# 4. PERFIL DE COLUMNAS
# ======================================================================

with tab_perfil:

    st.subheader("Perfil de una columna")

    table = st.selectbox(
        "Tabla",
        tables,
        key="profile_table",
    )

    columns = get_columns(table)["name"].tolist()

    column = st.selectbox(
        "Columna",
        columns,
        key="profile_column",
    )

    col = qident(column)

    if st.button(
        "Analizar columna",
        key="profile_column_button",
    ):

        with st.spinner("Analizando..."):

            profile_sql = f"""
            SELECT
                COUNT(*) AS total,
                SUM(
                    CASE WHEN {col} IS NULL
                    THEN 1 ELSE 0 END
                ) AS n_null,
                SUM(
                    CASE
                    WHEN {col} IS NOT NULL
                     AND TRIM(CAST({col} AS TEXT)) = ''
                    THEN 1 ELSE 0
                    END
                ) AS n_vacios,
                MIN({col}) AS minimo,
                MAX({col}) AS maximo
            FROM {qident(table)}
            """

            profile = query_df(profile_sql)

        st.dataframe(
            profile,
            use_container_width=True,
            hide_index=True,
        )

        if st.checkbox(
            "Calcular COUNT DISTINCT "
            "(puede tardar en columnas grandes)",
            key="distinct_checkbox",
        ):
            with st.spinner(
                "Calculando valores distintos..."
            ):
                n_distinct = scalar(
                    f"""
                    SELECT COUNT(DISTINCT {col})
                    FROM {qident(table)}
                    """
                )

            st.metric(
                "Valores distintos",
                format_int(n_distinct),
            )

        st.markdown("#### Valores más frecuentes")

        top_sql = f"""
        SELECT
            {col} AS valor,
            COUNT(*) AS frecuencia
        FROM {qident(table)}
        GROUP BY {col}
        ORDER BY frecuencia DESC
        LIMIT 100
        """

        top = query_df(top_sql)

        st.dataframe(
            top,
            use_container_width=True,
            hide_index=True,
        )

        st.markdown(
            "#### Tipos SQLite efectivamente almacenados"
        )

        types = query_df(
            f"""
            SELECT
                typeof({col}) AS tipo_sqlite,
                COUNT(*) AS filas
            FROM {qident(table)}
            GROUP BY typeof({col})
            ORDER BY filas DESC
            """
        )

        st.dataframe(
            types,
            use_container_width=True,
            hide_index=True,
        )


# ======================================================================
# 5. CALIDAD
# ======================================================================

with tab_calidad:

    st.subheader("Pruebas de calidad")

    table = st.selectbox(
        "Tabla",
        tables,
        key="quality_table",
    )

    columns = get_columns(table)["name"].tolist()

    # ------------------------------------------------------------------
    # NULL / VACÍOS
    # ------------------------------------------------------------------

    st.markdown("### NULL y valores vacíos")

    if st.button(
        "Revisar todas las columnas",
        key="quality_missing",
    ):

        expressions = []

        for i, colname in enumerate(columns):
            col = qident(colname)

            expressions.append(
                f"""
                SUM(
                    CASE WHEN {col} IS NULL
                    THEN 1 ELSE 0 END
                ) AS null_{i}
                """
            )

            expressions.append(
                f"""
                SUM(
                    CASE
                    WHEN {col} IS NOT NULL
                     AND TRIM(CAST({col} AS TEXT)) = ''
                    THEN 1 ELSE 0
                    END
                ) AS empty_{i}
                """
            )

        sql = (
            "SELECT "
            + ", ".join(expressions)
            + f" FROM {qident(table)}"
        )

        with st.spinner(
            "Escaneando la tabla completa..."
        ):
            result = query_df(sql).iloc[0]

        n_total = row_count(table)

        rows = []

        for i, colname in enumerate(columns):
            n_null = int(
                result.get(f"null_{i}", 0) or 0
            )

            n_empty = int(
                result.get(f"empty_{i}", 0) or 0
            )

            rows.append(
                {
                    "columna": colname,
                    "null": n_null,
                    "vacíos": n_empty,
                    "problemáticos": (
                        n_null + n_empty
                    ),
                    "% problemáticos": (
                        100
                        * (n_null + n_empty)
                        / n_total
                        if n_total
                        else 0
                    ),
                }
            )

        missing_df = pd.DataFrame(rows)

        missing_df = missing_df.sort_values(
            "problemáticos",
            ascending=False,
        )

        st.dataframe(
            missing_df,
            use_container_width=True,
            hide_index=True,
        )

    st.divider()

    # ------------------------------------------------------------------
    # CARACTERES SOSPECHOSOS
    # ------------------------------------------------------------------

    st.markdown("### Caracteres sospechosos")

    suspicious_col = st.selectbox(
        "Columna a inspeccionar",
        columns,
        key="suspicious_col",
    )

    suspicious_patterns = [
        " ",
        "Ã",
        "Â",
        "\u00a0",
    ]

    chosen_pattern = st.selectbox(
        "Patrón",
        suspicious_patterns,
        format_func=lambda x: {
            " ": "   replacement character",
            "Ã": "Ã  posible mojibake UTF-8",
            "Â": "Â  posible mojibake UTF-8",
            "\u00a0": "espacio no separable",
        }.get(x, repr(x)),
    )

    if st.button(
        "Buscar patrón",
        key="search_encoding",
    ):
        col = qident(suspicious_col)

        sql = f"""
        SELECT rowid AS __rowid__, *
        FROM {qident(table)}
        WHERE CAST({col} AS TEXT) LIKE ?
        LIMIT 1000
        """

        df = query_df(
            sql,
            [f"%{chosen_pattern}%"],
        )

        if df.empty:
            st.success(
                "No encontré coincidencias en la muestra buscada."
            )
        else:
            st.warning(
                f"Se encontraron {len(df)} registros."
            )

            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
            )

    st.divider()

    # ------------------------------------------------------------------
    # ESPACIOS
    # ------------------------------------------------------------------

    st.markdown(
        "### Espacios al inicio o al final"
    )

    whitespace_col = st.selectbox(
        "Columna",
        columns,
        key="whitespace_col",
    )

    if st.button(
        "Buscar whitespace",
        key="whitespace_button",
    ):
        col = qident(whitespace_col)

        sql = f"""
        SELECT rowid AS __rowid__, *
        FROM {qident(table)}
        WHERE
            {col} IS NOT NULL
            AND CAST({col} AS TEXT)
                <> TRIM(CAST({col} AS TEXT))
        LIMIT 1000
        """

        df = query_df(sql)

        if df.empty:
            st.success(
                "No se detectaron valores con espacios extremos."
            )
        else:
            st.warning(
                f"Se encontraron {len(df)} registros."
            )

            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
            )

    st.divider()

    # ------------------------------------------------------------------
    # DUPLICADOS
    # ------------------------------------------------------------------

    st.markdown("### Duplicados")

    st.caption(
        "Selecciona las columnas que deberían identificar "
        "un registro de manera única. "
        "No selecciones toda la tabla salvo que sea necesario."
    )

    duplicate_cols = st.multiselect(
        "Clave candidata",
        columns,
        key="duplicate_cols",
    )

    if st.button(
        "Buscar duplicados",
        key="duplicate_button",
        disabled=not duplicate_cols,
    ):

        group_cols = ", ".join(
            qident(c)
            for c in duplicate_cols
        )

        sql = f"""
        SELECT
            {group_cols},
            COUNT(*) AS repeticiones
        FROM {qident(table)}
        GROUP BY {group_cols}
        HAVING COUNT(*) > 1
        ORDER BY repeticiones DESC
        LIMIT 1000
        """

        with st.spinner(
            "Buscando grupos duplicados..."
        ):
            df = query_df(sql)

        if df.empty:
            st.success(
                "No se detectaron duplicados para esa clave."
            )
        else:
            st.warning(
                f"Se muestran hasta {len(df)} "
                "grupos duplicados."
            )

            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
            )

            csv_bytes = df.to_csv(
                index=False,
                sep=";",
            ).encode("utf-8-sig")

            st.download_button(
                "Descargar duplicados",
                csv_bytes,
                file_name=(
                    f"{table}_duplicados.csv"
                ),
            )

    st.divider()

    # ------------------------------------------------------------------
    # DISTRIBUCIÓN POR PERÍODO
    # ------------------------------------------------------------------

    if "periodo" in columns:

        st.markdown(
            "### Saltos en cantidad de registros por período"
        )

        if st.button(
            "Calcular distribución",
            key="quality_period_distribution",
        ):

            df = query_df(
                f"""
                SELECT
                    periodo,
                    COUNT(*) AS filas
                FROM {qident(table)}
                GROUP BY periodo
                ORDER BY periodo
                """
            )

            if not df.empty:

                df["cambio_pct"] = (
                    df["filas"]
                    .pct_change()
                    .mul(100)
                )

                st.dataframe(
                    df,
                    use_container_width=True,
                    hide_index=True,
                )

                st.line_chart(
                    df.set_index("periodo")[
                        "filas"
                    ]
                )


# ======================================================================
# 6. SQL
# ======================================================================

with tab_sql:

    st.subheader("Consola SQL de diagnóstico")

    st.info(
        "La conexión está en modo `query_only` y la base se abrió "
        "con `mode=ro`: esta consola sirve para consultas, no para "
        "modificar registros."
    )

    default_sql = """SELECT
    name,
    type
FROM sqlite_master
WHERE type = 'table'
ORDER BY name;
"""

    sql_text = st.text_area(
        "Consulta SQL",
        value=default_sql,
        height=250,
    )

    limit_result = int(
        st.number_input(
            "Máximo de filas a mostrar",
            min_value=10,
            max_value=100000,
            value=5000,
            step=100,
        )
    )

    if st.button(
        "Ejecutar SQL",
        key="execute_sql",
    ):

        try:
            stripped = sql_text.strip().rstrip(";")

            wrapped = (
                f"SELECT * FROM ({stripped}) "
                f"LIMIT {limit_result}"
            )

            with st.spinner(
                "Ejecutando consulta..."
            ):
                result = query_df(wrapped)

            st.success(
                f"{format_int(len(result))} filas devueltas."
            )

            st.dataframe(
                result,
                use_container_width=True,
                hide_index=True,
                height=600,
            )

            csv_bytes = result.to_csv(
                index=False,
                sep=";",
            ).encode("utf-8-sig")

            st.download_button(
                "Descargar resultado CSV",
                csv_bytes,
                file_name="consulta_sql.csv",
            )

        except Exception as e:
            st.exception(e)


# ======================================================================
# 7. HALLAZGOS
# ======================================================================

with tab_hallazgos:

    st.subheader(
        "Registro de problemas encontrados"
    )

    st.markdown(
        """
La idea es no corregir silenciosamente la SQLite.

Cuando aparezca un problema, registra:

1. dónde está;
2. por qué parece incorrecto;
3. la consulta que lo demuestra;
4. qué habría que corregir en el RAW o en el pipeline.
"""
    )

    finding_table = st.selectbox(
        "Tabla",
        tables,
        key="finding_table",
    )

    category = st.selectbox(
        "Categoría",
        [
            "dato faltante",
            "duplicado",
            "encoding",
            "período",
            "esquema",
            "valor improbable",
            "identificador",
            "inconsistencia entre tablas",
            "otro",
        ],
    )

    description = st.text_area(
        "Descripción del hallazgo"
    )

    sql_query = st.text_area(
        "SQL que permite reproducirlo"
    )

    upstream_action = st.text_area(
        "Corrección sugerida aguas arriba",
        placeholder=(
            "Ej.: revisar RAW 2021-03 NACI, "
            "normalizar columna X y reconstruir SQLite."
        ),
    )

    if st.button(
        "Guardar hallazgo",
        type="primary",
    ):

        if not description.strip():
            st.error(
                "Escribe una descripción."
            )
        else:
            append_finding(
                table=finding_table,
                category=category,
                description=description.strip(),
                sql_query=sql_query.strip(),
                upstream_action=upstream_action.strip(),
            )

            st.success(
                f"Hallazgo guardado en:\n{FINDINGS_PATH}"
            )

    if FINDINGS_PATH.exists():

        st.divider()
        st.markdown("### Hallazgos registrados")

        try:
            findings = pd.read_csv(
                FINDINGS_PATH,
                sep=";",
                encoding="utf-8-sig",
            )

            st.dataframe(
                findings,
                use_container_width=True,
                hide_index=True,
            )

        except Exception as e:
            st.exception(e)