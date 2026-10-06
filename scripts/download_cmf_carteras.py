#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Descarga reproducible de carteras de Fondos Mutuos desde CMF Chile.

Fuente:
https://www.cmfchile.cl/institucional/estadisticas/ffm_cartera.php

El formulario publica por POST a:
https://www.cmfchile.cl/institucional/estadisticas/ffm_download.php

Campos:
- mm      : 01..12
- aa      : año
- cartera : NACI | EXTR | OPCI | FUTU | OPLA

Ventana por defecto: 2021-01 a 2025-12
Tipos por defecto: todos los anteriores.

Características:
- preflight de una descarga
- reintentos y backoff
- pausa respetuosa configurable
- resume: no redescarga archivos válidos
- conserva bytes RAW
- SHA256
- manifest.csv
- log de errores
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Tuple

import requests

BASE_PAGE = "https://www.cmfchile.cl/institucional/estadisticas/ffm_cartera.php"
DOWNLOAD_URL = "https://www.cmfchile.cl/institucional/estadisticas/ffm_download.php"

TYPE_LABELS = {
    "NACI": "nacional",
    "EXTR": "extranjera",
    "OPCI": "opciones",
    "FUTU": "futuros",
    "OPLA": "opciones_lanzador",
}

DEFAULT_OUT = r"C:\Workspace\projects\magister-seminario1\data\raw\cmf\ffmm_carteras_2021_2025"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Safari/537.36"
)


def month_iter(start: str, end: str) -> Iterable[Tuple[int, int]]:
    sy, sm = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    y, m = sy, sm
    while (y, m) <= (ey, em):
        yield y, m
        m += 1
        if m == 13:
            y += 1
            m = 1


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def looks_like_html(b: bytes, content_type: str) -> bool:
    prefix = b[:1000].lstrip().lower()
    ct = (content_type or "").lower()
    return (
        "text/html" in ct
        or prefix.startswith(b"<!doctype html")
        or prefix.startswith(b"<html")
        or b"<html" in prefix[:500]
    )


def filename_from_cd(cd: str) -> str | None:
    if not cd:
        return None
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, flags=re.I)
    if not m:
        return None
    name = m.group(1).strip()
    name = name.replace("\\", "_").replace("/", "_")
    return name or None


def ensure_manifest(path: Path):
    if path.exists():
        return
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp",
                "periodo",
                "year",
                "month",
                "cartera_code",
                "cartera_label",
                "status",
                "http_status",
                "content_type",
                "content_disposition",
                "bytes",
                "sha256",
                "filename",
                "path",
                "attempts",
                "elapsed_sec",
                "error",
            ],
        )
        w.writeheader()


def append_manifest(path: Path, row: dict):
    with path.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        w.writerow(row)


def existing_valid(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def post_download(
    session: requests.Session,
    year: int,
    month: int,
    cartera: str,
    timeout: int,
    retries: int,
):
    payload = {
        "mm": f"{month:02d}",
        "aa": str(year),
        "cartera": cartera,
    }

    last_error = None
    for attempt in range(1, retries + 1):
        t0 = time.perf_counter()
        try:
            r = session.post(
                DOWNLOAD_URL,
                data=payload,
                timeout=timeout,
                allow_redirects=True,
            )
            elapsed = time.perf_counter() - t0

            if r.status_code == 200 and r.content and not looks_like_html(
                r.content, r.headers.get("Content-Type", "")
            ):
                return r, attempt, elapsed, None

            snippet = r.content[:500].decode("latin-1", errors="replace")
            last_error = (
                f"Respuesta no válida. HTTP={r.status_code}; "
                f"Content-Type={r.headers.get('Content-Type')}; "
                f"primeros_bytes={snippet!r}"
            )
        except Exception as e:
            elapsed = time.perf_counter() - t0
            last_error = repr(e)

        if attempt < retries:
            time.sleep(min(2 ** (attempt - 1), 8))

    return None, retries, elapsed, last_error


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01")
    ap.add_argument("--end", default="2025-12")
    ap.add_argument(
        "--types",
        nargs="+",
        default=list(TYPE_LABELS),
        choices=list(TYPE_LABELS),
        help="NACI EXTR OPCI FUTU OPLA",
    )
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--preflight-only",
        action="store_true",
        help="Prueba 2025-12 NACI y termina.",
    )
    args = ap.parse_args()

    out = Path(args.out)
    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    manifest = out / "manifest.csv"
    ensure_manifest(manifest)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
            "Referer": BASE_PAGE,
            "Origin": "https://www.cmfchile.cl",
            "Connection": "keep-alive",
        }
    )

    # GET inicial para obtener cookies/sesión del sitio.
    print("=== CMF FFMM CARTERAS — PREFLIGHT ===")
    try:
        g = session.get(BASE_PAGE, timeout=args.timeout)
        print("GET página:", g.status_code, g.headers.get("Content-Type"))
        g.raise_for_status()
    except Exception as e:
        print("ERROR en GET inicial:", repr(e))
        sys.exit(2)

    # Smoke test.
    print("POST prueba: 2025-12 NACI ...")
    r, attempts, elapsed, err = post_download(
        session, 2025, 12, "NACI", args.timeout, args.retries
    )
    if r is None:
        print("PREFLIGHT FALLÓ:", err)
        sys.exit(3)

    print(
        "PREFLIGHT OK:",
        f"HTTP={r.status_code}",
        f"bytes={len(r.content):,}",
        f"type={r.headers.get('Content-Type')}",
        f"cd={r.headers.get('Content-Disposition')}",
    )

    if args.preflight_only:
        test_path = out / "preflight_2025-12_NACI.bin"
        test_path.write_bytes(r.content)
        print("Guardado:", test_path)
        return

    periods = list(month_iter(args.start, args.end))
    jobs = [(y, m, t) for y, m in periods for t in args.types]
    print("")
    print("=== DESCARGA ===")
    print("Períodos:", len(periods))
    print("Tipos:", ", ".join(args.types))
    print("Solicitudes máximas:", len(jobs))
    print("Salida:", out)
    print("Delay:", args.delay, "seg")
    print("")

    ok = skip = fail = 0
    t_all = time.perf_counter()

    for idx, (year, month, cartera) in enumerate(jobs, start=1):
        periodo = f"{year}-{month:02d}"
        label = TYPE_LABELS[cartera]
        period_dir = raw / f"periodo={periodo}"
        period_dir.mkdir(parents=True, exist_ok=True)

        # nombre canónico. Si CMF entrega un nombre específico, se conserva en manifest.
        dest = period_dir / f"{cartera}_{label}.txt"

        if existing_valid(dest) and not args.force:
            skip += 1
            print(
                f"[{idx:03d}/{len(jobs):03d}] SKIP {periodo} {cartera} "
                f"{dest.stat().st_size:,} bytes"
            )
            continue

        print(f"[{idx:03d}/{len(jobs):03d}] GET  {periodo} {cartera} ... ", end="", flush=True)

        r, attempts, elapsed, err = post_download(
            session, year, month, cartera, args.timeout, args.retries
        )

        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "periodo": periodo,
            "year": year,
            "month": f"{month:02d}",
            "cartera_code": cartera,
            "cartera_label": label,
            "status": "",
            "http_status": "",
            "content_type": "",
            "content_disposition": "",
            "bytes": 0,
            "sha256": "",
            "filename": "",
            "path": "",
            "attempts": attempts,
            "elapsed_sec": round(elapsed, 3),
            "error": "",
        }

        if r is None:
            fail += 1
            row["status"] = "ERROR"
            row["error"] = err or "unknown"
            append_manifest(manifest, row)
            print("ERROR")
            print("   ", err)
        else:
            b = r.content
            cmf_name = filename_from_cd(r.headers.get("Content-Disposition", ""))
            dest.write_bytes(b)

            ok += 1
            row.update(
                {
                    "status": "OK",
                    "http_status": r.status_code,
                    "content_type": r.headers.get("Content-Type", ""),
                    "content_disposition": r.headers.get(
                        "Content-Disposition", ""
                    ),
                    "bytes": len(b),
                    "sha256": sha256_bytes(b),
                    "filename": cmf_name or dest.name,
                    "path": str(dest),
                }
            )
            append_manifest(manifest, row)
            print(f"OK {len(b):,} bytes")

        if idx < len(jobs) and args.delay > 0:
            time.sleep(args.delay)

    elapsed_all = time.perf_counter() - t_all

    print("")
    print("=" * 72)
    print("TERMINADO")
    print("OK   :", ok)
    print("SKIP :", skip)
    print("ERROR:", fail)
    print("Tiempo:", round(elapsed_all / 60, 2), "min")
    print("RAW:", raw)
    print("Manifest:", manifest)
    print("=" * 72)

    if fail:
        sys.exit(4)


if __name__ == "__main__":
    main()
