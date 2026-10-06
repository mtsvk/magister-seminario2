#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Seminario II — Construcción reproducible de base SQLite desde RAW CMF.

Entrada:
    C:\Workspace\projects\magister-seminario2\data\_staging\cmf_carteras_2000_2026\raw

Salida:
    C:\Workspace\projects\magister-seminario2\data\database\cmf_carteras_2000_2026.sqlite

Principios:
- Los archivos RAW no se modifican.
- Las columnas CMF se preservan exactamente como texto.
- Se agregan sólo metadatos de procedencia (_periodo, _source_file_id, _source_row).
- Los archivos cuyo contenido es "Sin información" se registran, pero no generan posiciones.
- El script valida que el encabezado sea estable dentro de cada tipo de cartera.
- Se genera inventario, diccionario de esquema, vistas de cobertura y un CSV-resumen.
- Usa sólo la biblioteca estándar de Python.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_ROOT = Path(r"C:\Workspace\projects\magister-seminario2")

TYPE_INFO = {
    "NACI": ("nacional", "cartera_nacional"),
    "EXTR": ("extranjera", "cartera_extranjera"),
    "OPCI": ("opciones", "cartera_opciones"),
    "FUTU": ("futuros", "cartera_futuros"),
    "OPLA": ("opciones_lanzadas", "cartera_opciones_lanzadas"),
}

TYPE_RE = re.compile(r"^(NACI|EXTR|OPCI|FUTU|OPLA)_", re.I)
PERIOD_RE = re.compile(r"periodo=(\d{4}-\d{2})", re.I)


def q(identifier: str) -> str:
    """Quote a SQLite identifier safely."""
    return '"' + identifier.replace('"', '""') + '"'


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def first_nonempty_line(path: Path) -> str:
    with path.open("r", encoding="utf-8-sig", errors="strict", newline="") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if line.strip():
                return line
    return ""


def parse_source_metadata(path: Path, raw_root: Path) -> tuple[str, str]:
    rel = str(path.relative_to(raw_root))
    period_match = PERIOD_RE.search(rel)
    type_match = TYPE_RE.match(path.name)

    if not period_match:
        raise ValueError(f"No se pudo extraer periodo desde: {rel}")
    if not type_match:
        raise ValueError(f"No se pudo extraer tipo desde: {path.name}")

    return period_match.group(1), type_match.group(1).upper()


def read_header(path: Path) -> tuple[str, list[str] | None]:
    first = first_nonempty_line(path)
    if first.strip().lower() == "sin información":
        return "no_information", None
    if ";" not in first:
        raise ValueError(f"Encabezado sin delimitador ';' en {path}: {first[:200]!r}")
    return "data", next(csv.reader([first], delimiter=";"))


def schema_hash(columns: list[str]) -> str:
    normalized = "|".join(c.strip().lower() for c in columns)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def create_core_tables(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE build_info (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE source_files (
            source_file_id INTEGER PRIMARY KEY AUTOINCREMENT,
            periodo TEXT NOT NULL,
            cartera_code TEXT NOT NULL,
            cartera_label TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            filename TEXT NOT NULL,
            bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            row_count INTEGER NOT NULL DEFAULT 0,
            n_columns INTEGER,
            schema_hash TEXT,
            UNIQUE(periodo, cartera_code)
        );

        CREATE TABLE schema_columns (
            cartera_code TEXT NOT NULL,
            cartera_label TEXT NOT NULL,
            schema_hash TEXT NOT NULL,
            column_position INTEGER NOT NULL,
            column_name TEXT NOT NULL,
            PRIMARY KEY(cartera_code, schema_hash, column_position)
        );

        CREATE TABLE import_errors (
            error_id INTEGER PRIMARY KEY AUTOINCREMENT,
            periodo TEXT,
            cartera_code TEXT,
            relative_path TEXT,
            source_row INTEGER,
            error TEXT NOT NULL
        );
        """
    )


def create_data_table(
    con: sqlite3.Connection,
    table: str,
    columns: list[str],
) -> None:
    source_cols = ",\n            ".join(f"{q(c)} TEXT" for c in columns)
    sql = f"""
        CREATE TABLE {q(table)} (
            {source_cols},
            _periodo TEXT NOT NULL,
            _source_file_id INTEGER NOT NULL,
            _source_row INTEGER NOT NULL,
            FOREIGN KEY (_source_file_id) REFERENCES source_files(source_file_id)
        )
    """
    con.execute(sql)


def load_download_manifest(con: sqlite3.Connection, manifest: Path) -> None:
    if not manifest.exists():
        return

    with manifest.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        if not fields:
            return

        con.execute("DROP TABLE IF EXISTS download_manifest")
        con.execute(
            "CREATE TABLE download_manifest ("
            + ", ".join(f"{q(c)} TEXT" for c in fields)
            + ")"
        )
        placeholders = ",".join("?" for _ in fields)
        sql = (
            f"INSERT INTO download_manifest ({','.join(q(c) for c in fields)}) "
            f"VALUES ({placeholders})"
        )
        batch = []
        for row in reader:
            batch.append(tuple(row.get(c, "") for c in fields))
            if len(batch) >= 5000:
                con.executemany(sql, batch)
                batch.clear()
        if batch:
            con.executemany(sql, batch)


def build_views_and_indexes(con: sqlite3.Connection) -> None:
    for code, (_, table) in TYPE_INFO.items():
        con.execute(
            f"CREATE INDEX {q('idx_' + table + '_periodo')} "
            f"ON {q(table)} (_periodo)"
        )
        con.execute(
            f"CREATE INDEX {q('idx_' + table + '_run_periodo')} "
            f"ON {q(table)} ({q('Run Fondo')}, _periodo)"
        )

    union_parts = []
    for _, (_, table) in TYPE_INFO.items():
        union_parts.append(
            f"SELECT _periodo AS periodo, {q('Run Fondo')} AS run_fondo, "
            f"{q('Nombre Fondo')} AS nombre_fondo FROM {q(table)}"
        )

    con.execute(
        "CREATE VIEW v_fondos_mes AS "
        "SELECT periodo, run_fondo, MAX(nombre_fondo) AS nombre_fondo "
        "FROM (" + " UNION ALL ".join(union_parts) + ") "
        "GROUP BY periodo, run_fondo"
    )

    con.execute(
        """
        CREATE VIEW v_cobertura_periodos AS
        SELECT
            periodo,
            MAX(CASE WHEN cartera_code='NACI' THEN status END) AS naci_status,
            MAX(CASE WHEN cartera_code='EXTR' THEN status END) AS extr_status,
            MAX(CASE WHEN cartera_code='OPCI' THEN status END) AS opci_status,
            MAX(CASE WHEN cartera_code='FUTU' THEN status END) AS futu_status,
            MAX(CASE WHEN cartera_code='OPLA' THEN status END) AS opla_status,
            SUM(CASE WHEN cartera_code='NACI' THEN row_count ELSE 0 END) AS naci_rows,
            SUM(CASE WHEN cartera_code='EXTR' THEN row_count ELSE 0 END) AS extr_rows,
            SUM(CASE WHEN cartera_code='OPCI' THEN row_count ELSE 0 END) AS opci_rows,
            SUM(CASE WHEN cartera_code='FUTU' THEN row_count ELSE 0 END) AS futu_rows,
            SUM(CASE WHEN cartera_code='OPLA' THEN row_count ELSE 0 END) AS opla_rows,
            SUM(row_count) AS total_rows
        FROM source_files
        GROUP BY periodo
        ORDER BY periodo
        """
    )

    con.execute(
        """
        CREATE VIEW v_resumen_tipos AS
        SELECT
            cartera_code,
            cartera_label,
            COUNT(*) AS archivos,
            SUM(CASE WHEN status='data' THEN 1 ELSE 0 END) AS archivos_con_datos,
            SUM(CASE WHEN status='no_information' THEN 1 ELSE 0 END) AS archivos_sin_informacion,
            MIN(periodo) AS primer_periodo_solicitado,
            MAX(periodo) AS ultimo_periodo_solicitado,
            MIN(CASE WHEN status='data' THEN periodo END) AS primer_periodo_con_datos,
            MAX(CASE WHEN status='data' THEN periodo END) AS ultimo_periodo_con_datos,
            SUM(row_count) AS filas
        FROM source_files
        GROUP BY cartera_code, cartera_label
        ORDER BY cartera_code
        """
    )


def export_query_to_csv(
    con: sqlite3.Connection,
    query: str,
    destination: Path,
) -> None:
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    with destination.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(headers)
        while True:
            rows = cur.fetchmany(10000)
            if not rows:
                break
            w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument(
        "--force",
        action="store_true",
        help="Elimina y reconstruye la base SQLite si ya existe.",
    )
    args = ap.parse_args()

    root = args.root.resolve()
    stage = root / "data" / "_staging" / "cmf_carteras_2000_2026"
    raw = stage / "raw"
    manifest = stage / "manifest.csv"
    db_dir = root / "data" / "database"
    exports = root / "data" / "exports"
    logs = root / "logs"

    db_dir.mkdir(parents=True, exist_ok=True)
    exports.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    db_path = db_dir / "cmf_carteras_2000_2026.sqlite"

    if not raw.exists():
        raise SystemExit(f"No existe RAW: {raw}")

    if db_path.exists():
        if not args.force:
            raise SystemExit(
                f"La base ya existe: {db_path}\n"
                "Use --force sólo si desea reconstruirla desde cero."
            )
        db_path.unlink()

    files = sorted(p for p in raw.rglob("*.txt") if p.is_file())
    if not files:
        raise SystemExit(f"No se encontraron TXT bajo: {raw}")

    # ---------- Primera pasada: inventario y validación de esquemas ----------
    schemas: dict[str, list[str]] = {}
    file_meta = []

    for path in files:
        periodo, code = parse_source_metadata(path, raw)
        status, header = read_header(path)
        if status == "data":
            assert header is not None
            if code not in schemas:
                schemas[code] = header
            elif schemas[code] != header:
                raise SystemExit(
                    f"Cambio de esquema detectado en {code} / {periodo}:\n{path}"
                )
        file_meta.append((path, periodo, code, status, header))

    missing_schemas = sorted(set(TYPE_INFO) - set(schemas))
    if missing_schemas:
        raise SystemExit(f"No se encontró ningún archivo con datos para: {missing_schemas}")

    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    t_build = time.perf_counter()

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-200000")  # ~200 MB

    try:
        create_core_tables(con)

        build_info = {
            "run_id": run_id,
            "started_utc": started.isoformat(),
            "root": str(root),
            "raw": str(raw),
            "database": str(db_path),
            "python": sys.version.replace("\n", " "),
            "platform": platform.platform(),
            "sqlite_version": sqlite3.sqlite_version,
            "source_files_expected": str(len(files)),
        }
        con.executemany(
            "INSERT INTO build_info(key,value) VALUES (?,?)",
            build_info.items(),
        )

        load_download_manifest(con, manifest)

        for code, columns in schemas.items():
            label, table = TYPE_INFO[code]
            create_data_table(con, table, columns)
            shash = schema_hash(columns)
            con.executemany(
                """
                INSERT INTO schema_columns(
                    cartera_code, cartera_label, schema_hash,
                    column_position, column_name
                ) VALUES (?,?,?,?,?)
                """,
                [
                    (code, label, shash, i, col)
                    for i, col in enumerate(columns, start=1)
                ],
            )

        con.commit()

        totals = {code: 0 for code in TYPE_INFO}
        no_info = {code: 0 for code in TYPE_INFO}

        for idx, (path, periodo, code, status, header) in enumerate(file_meta, start=1):
            label, table = TYPE_INFO[code]
            rel = str(path.relative_to(raw))
            digest = sha256_file(path)
            ncols = len(header) if header else None
            shash = schema_hash(header) if header else None

            cur = con.execute(
                """
                INSERT INTO source_files(
                    periodo, cartera_code, cartera_label,
                    relative_path, filename, bytes, sha256,
                    status, row_count, n_columns, schema_hash
                ) VALUES (?,?,?,?,?,?,?,?,0,?,?)
                """,
                (
                    periodo, code, label, rel, path.name,
                    path.stat().st_size, digest, status, ncols, shash,
                ),
            )
            source_file_id = cur.lastrowid

            if status == "no_information":
                no_info[code] += 1
                con.commit()
                print(f"[{idx:4}/{len(file_meta)}] SIN INFO {periodo} {code}")
                continue

            columns = schemas[code]
            insert_columns = columns + ["_periodo", "_source_file_id", "_source_row"]
            placeholders = ",".join("?" for _ in insert_columns)
            insert_sql = (
                f"INSERT INTO {q(table)} "
                f"({','.join(q(c) for c in insert_columns)}) "
                f"VALUES ({placeholders})"
            )

            row_count = 0
            batch = []

            with path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f, delimiter=";")
                actual_header = next(reader, None)
                if actual_header != columns:
                    raise RuntimeError(f"Encabezado inesperado al importar: {path}")

                for source_row, row in enumerate(reader, start=2):
                    if not row or not any(cell != "" for cell in row):
                        continue

                    if len(row) != len(columns):
                        con.execute(
                            """
                            INSERT INTO import_errors(
                                periodo, cartera_code, relative_path,
                                source_row, error
                            ) VALUES (?,?,?,?,?)
                            """,
                            (
                                periodo, code, rel, source_row,
                                f"Esperadas {len(columns)} columnas; recibidas {len(row)}",
                            ),
                        )
                        continue

                    batch.append(tuple(row) + (periodo, source_file_id, source_row))
                    row_count += 1

                    if len(batch) >= 10000:
                        con.executemany(insert_sql, batch)
                        batch.clear()

                if batch:
                    con.executemany(insert_sql, batch)

            con.execute(
                "UPDATE source_files SET row_count=? WHERE source_file_id=?",
                (row_count, source_file_id),
            )
            totals[code] += row_count
            con.commit()

            print(
                f"[{idx:4}/{len(file_meta)}] OK       {periodo} {code} "
                f"{row_count:>9,} filas"
            )

        build_views_and_indexes(con)
        con.execute("ANALYZE")
        con.commit()

        export_query_to_csv(
            con,
            "SELECT * FROM v_resumen_tipos",
            exports / "cmf_carteras_resumen_tipos.csv",
        )
        export_query_to_csv(
            con,
            "SELECT * FROM v_cobertura_periodos",
            exports / "cmf_carteras_cobertura_periodos.csv",
        )

        finished = datetime.now(timezone.utc)
        elapsed = time.perf_counter() - t_build

        end_info = {
            "finished_utc": finished.isoformat(),
            "elapsed_seconds": f"{elapsed:.3f}",
            "database_bytes": str(db_path.stat().st_size),
            "source_files_loaded": str(len(file_meta)),
            "rows_NACI": str(totals["NACI"]),
            "rows_EXTR": str(totals["EXTR"]),
            "rows_OPCI": str(totals["OPCI"]),
            "rows_FUTU": str(totals["FUTU"]),
            "rows_OPLA": str(totals["OPLA"]),
            "no_information_NACI": str(no_info["NACI"]),
            "no_information_EXTR": str(no_info["EXTR"]),
            "no_information_OPCI": str(no_info["OPCI"]),
            "no_information_FUTU": str(no_info["FUTU"]),
            "no_information_OPLA": str(no_info["OPLA"]),
        }
        con.executemany(
            "INSERT OR REPLACE INTO build_info(key,value) VALUES (?,?)",
            end_info.items(),
        )
        con.commit()

        summary = {
            **build_info,
            **end_info,
            "schemas": {
                code: {
                    "table": TYPE_INFO[code][1],
                    "columns": len(columns),
                    "schema_hash": schema_hash(columns),
                }
                for code, columns in schemas.items()
            },
        }
        log_path = logs / f"build_cmf_sqlite_{run_id}.json"
        with log_path.open("w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        # Cerrar WAL en el archivo principal.
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
        con.commit()

    finally:
        con.close()

    print("\n" + "=" * 72)
    print("BASE SQLITE CONSTRUIDA")
    print("=" * 72)
    print(f"Base     : {db_path}")
    print(f"Archivos : {len(file_meta):,}")
    for code in TYPE_INFO:
        print(
            f"{code:4}: {totals[code]:>12,} filas | "
            f"sin información: {no_info[code]:>3}"
        )
    print(f"Log      : {log_path}")
    print(f"Export   : {exports / 'cmf_carteras_resumen_tipos.csv'}")
    print(f"Export   : {exports / 'cmf_carteras_cobertura_periodos.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
