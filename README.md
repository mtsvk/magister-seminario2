# Seminario II — Magíster en Business Analytics UC

Proyecto de Seminario de Graduación II.

## Objetivo

Estimación del riesgo de fondos mutuos chilenos a partir de la composición contemporánea de cartera, utilizando información histórica publicada por la CMF.

## Pipeline

- Descarga reproducible de carteras históricas CMF.
- Auditoría de archivos RAW.
- Construcción de base SQLite histórica.
- Inspección y control de calidad.
- Construcción de variables a nivel fondo × fecha.
- Modelación y validación temporal.

## Inspector SQLite

Ejecutar localmente:

```powershell
streamlit run .\03.1_inspect_cmf_sqlite.py
```

La base SQLite y los datos RAW no se almacenan en GitHub.
