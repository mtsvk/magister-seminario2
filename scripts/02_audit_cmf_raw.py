from pathlib import Path
import csv
import hashlib
import re
from collections import defaultdict

ROOT = Path(
    r"C:\Workspace\projects\magister-seminario2"
)

RAW = ROOT / "data" / "_staging" / "cmf_carteras_2000_2026" / "raw"
OUT = ROOT / "data" / "_staging" / "cmf_carteras_2000_2026" / "_audit"
SAMPLES = OUT / "samples"

OUT.mkdir(parents=True, exist_ok=True)
SAMPLES.mkdir(parents=True, exist_ok=True)

DELIMITERS = [";", "\t", "|", ","]


def detect_encoding(path):
    data = path.read_bytes()[:100_000]

    for enc in ["utf-8-sig", "utf-8", "cp1252", "latin-1"]:
        try:
            data.decode(enc)
            return enc
        except UnicodeDecodeError:
            pass

    return "latin-1"


def first_lines(path, encoding, n=10):
    lines = []

    with path.open(
        "r",
        encoding=encoding,
        errors="replace"
    ) as f:
        for _ in range(n):
            line = f.readline()

            if not line:
                break

            lines.append(line.rstrip("\r\n"))

    return lines


def detect_delimiter(header):
    counts = {
        d: header.count(d)
        for d in DELIMITERS
    }

    best = max(counts, key=counts.get)

    if counts[best] == 0:
        return None

    return best


def extract_metadata(path):
    relative = path.relative_to(RAW)
    text = str(relative)

    period_match = re.search(
        r"periodo=(\d{4}-\d{2})",
        text,
        re.I
    )

    type_match = re.search(
        r"tipo=([A-Za-z]+)",
        text,
        re.I
    )

    period = (
        period_match.group(1)
        if period_match
        else ""
    )

    tipo = (
        type_match.group(1).upper()
        if type_match
        else ""
    )

    return period, tipo


records = []
schemas = {}
schema_periods = defaultdict(list)

files = sorted(
    p
    for p in RAW.rglob("*")
    if p.is_file()
)

print(f"Archivos encontrados: {len(files):,}")

for i, path in enumerate(files, 1):

    period, tipo = extract_metadata(path)

    encoding = detect_encoding(path)
    lines = first_lines(path, encoding, 10)

    header = lines[0] if lines else ""
    delimiter = detect_delimiter(header)

    if delimiter:
        columns = header.split(delimiter)
    else:
        columns = [header] if header else []

    normalized_header = "|".join(
        c.strip().lower()
        for c in columns
    )

    schema_hash = hashlib.sha256(
        normalized_header.encode("utf-8")
    ).hexdigest()[:16]

    records.append({
        "periodo": period,
        "tipo": tipo,
        "relative_path": str(path.relative_to(RAW)),
        "filename": path.name,
        "bytes": path.stat().st_size,
        "encoding": encoding,
        "delimiter": repr(delimiter),
        "n_columns": len(columns),
        "schema_hash": schema_hash,
        "header": header,
    })

    schema_periods[(tipo, schema_hash)].append(period)

    key = (tipo, schema_hash)

    if key not in schemas:
        schemas[key] = {
            "tipo": tipo,
            "schema_hash": schema_hash,
            "encoding": encoding,
            "delimiter": repr(delimiter),
            "n_columns": len(columns),
            "header": header,
            "columns": columns,
            "sample_path": path,
            "sample_lines": lines,
        }

    if i % 100 == 0:
        print(f"Procesados: {i:,}/{len(files):,}")


# ---------------------------------------------------------
# INVENTARIO COMPLETO
# ---------------------------------------------------------

inventory_path = OUT / "inventory.csv"

with inventory_path.open(
    "w",
    newline="",
    encoding="utf-8-sig"
) as f:

    fields = [
        "periodo",
        "tipo",
        "relative_path",
        "filename",
        "bytes",
        "encoding",
        "delimiter",
        "n_columns",
        "schema_hash",
        "header",
    ]

    w = csv.DictWriter(
        f,
        fieldnames=fields
    )

    w.writeheader()
    w.writerows(records)


# ---------------------------------------------------------
# RESUMEN DE ESQUEMAS
# ---------------------------------------------------------

summary_path = OUT / "schema_summary.csv"

with summary_path.open(
    "w",
    newline="",
    encoding="utf-8-sig"
) as f:

    fields = [
        "tipo",
        "schema_hash",
        "n_files",
        "first_period",
        "last_period",
        "encoding",
        "delimiter",
        "n_columns",
        "header",
    ]

    w = csv.DictWriter(
        f,
        fieldnames=fields
    )

    w.writeheader()

    for key, schema in sorted(schemas.items()):

        periods = sorted(
            p for p in schema_periods[key]
            if p
        )

        w.writerow({
            "tipo": schema["tipo"],
            "schema_hash": schema["schema_hash"],
            "n_files": len(schema_periods[key]),
            "first_period": periods[0] if periods else "",
            "last_period": periods[-1] if periods else "",
            "encoding": schema["encoding"],
            "delimiter": schema["delimiter"],
            "n_columns": schema["n_columns"],
            "header": schema["header"],
        })


# ---------------------------------------------------------
# COLUMNAS POR ESQUEMA
# ---------------------------------------------------------

columns_path = OUT / "schema_columns.csv"

with columns_path.open(
    "w",
    newline="",
    encoding="utf-8-sig"
) as f:

    fields = [
        "tipo",
        "schema_hash",
        "column_position",
        "column_name",
    ]

    w = csv.DictWriter(
        f,
        fieldnames=fields
    )

    w.writeheader()

    for key, schema in sorted(schemas.items()):

        for pos, column in enumerate(
            schema["columns"],
            start=1
        ):
            w.writerow({
                "tipo": schema["tipo"],
                "schema_hash": schema["schema_hash"],
                "column_position": pos,
                "column_name": column,
            })


# ---------------------------------------------------------
# MUESTRA DE CADA ESQUEMA DISTINTO
# ---------------------------------------------------------

for key, schema in schemas.items():

    tipo, schema_hash = key

    sample_file = (
        SAMPLES
        / f"{tipo or 'UNKNOWN'}__{schema_hash}.txt"
    )

    with sample_file.open(
        "w",
        encoding="utf-8"
    ) as f:

        f.write(
            f"SOURCE: {schema['sample_path']}\n"
        )
        f.write(
            f"ENCODING: {schema['encoding']}\n"
        )
        f.write(
            f"DELIMITER: {schema['delimiter']}\n"
        )
        f.write(
            f"COLUMNS: {schema['n_columns']}\n"
        )
        f.write(
            f"SCHEMA_HASH: {schema_hash}\n\n"
        )

        for line in schema["sample_lines"]:
            f.write(line + "\n")


print()
print("=" * 70)
print("AUDITORÍA TERMINADA")
print("=" * 70)
print(f"Archivos físicos : {len(files):,}")
print(f"Esquemas distintos: {len(schemas):,}")
print(f"Inventario       : {inventory_path}")
print(f"Resumen esquemas : {summary_path}")
print(f"Columnas         : {columns_path}")
print(f"Muestras         : {SAMPLES}")