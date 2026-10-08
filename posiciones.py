"""Posiciones de picking: venta diaria por SKU vs capacidad de su posición SURTID.

No depende de Streamlit: el reporte automático (fase 3) usará este mismo módulo.
"""
import io

import numpy as np
import pandas as pd

import config as cfg

DIAGNOSTICOS = [
    ("Crítico: doble posición",
     "Asignar segunda posición de picking o ampliar capacidad: en un día alto necesita más de 2 reposiciones."),
    ("Revisar: repone en turno",
     "En días altos se vacía dentro del turno: revisar mínimo/máximo, o doble posición si es clase A."),
    ("Sin posición de picking",
     "Vende y no tiene preferencia asignada: asignar ubicación de picking, partiendo por los clase A."),
    ("Capacidad por revisar", "Capacidad 0 o 1 en el maestro: corregir el dato."),
    ("Posición sin venta",
     "Sin venta en el período: candidata a liberar para SKUs críticos o sin posición."),
    ("Sobredimensionada",
     "Clase C que en días altos usa menos del 20% de su capacidad: candidata a reducir o reubicar."),
    ("OK", "Sin acción."),
]
ORDEN = [d for d, _ in DIAGNOSTICOS]
ACCION = dict(DIAGNOSTICOS)

COLS_VENTA = ["fecha contabiliz.", "material", "texto breve material", "un.medida de entrada",
              "cantidad", "documento material"]


# ---------------------------------------------------------------- lectura

def leer_tabla(contenido: bytes, nombre: str, columnas=None) -> pd.DataFrame:
    """Lee CSV o Excel con nombres de columna en minúsculas.

    columnas: lista de columnas (en minúsculas) a conservar; en CSV grandes evita cargar
    columnas que no se usan y ahorra mucha memoria.
    """
    usar = (lambda c: str(c).strip().lower() in columnas) if columnas else None
    if nombre.lower().endswith(".csv"):
        primera = contenido[:2000].split(b"\n", 1)[0]
        sep = ";" if primera.count(b";") > primera.count(b",") else ","
        for codificacion in ("utf-8-sig", "latin-1"):
            try:
                df = pd.read_csv(io.BytesIO(contenido), sep=sep, dtype=str, usecols=usar,
                                 encoding=codificacion)
                break
            except UnicodeDecodeError:
                continue
    else:
        df = pd.read_excel(io.BytesIO(contenido), dtype=str, usecols=usar)
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def preparar_venta(v: pd.DataFrame) -> pd.DataFrame:
    faltan = [c for c in COLS_VENTA if c not in v.columns]
    if faltan:
        raise ValueError(f"Al archivo de venta le faltan columnas: {', '.join(faltan)}.")
    txt = v["cantidad"].astype(str).str.strip()
    # Formato chileno (1.200 o 3,5): punto de miles y coma decimal
    chileno = txt.str.contains(",") | txt.str.fullmatch(r"-?\d{1,3}(\.\d{3})+")
    txt = txt.where(~chileno, txt.str.replace(".", "", regex=False).str.replace(",", ".", regex=False))
    cantidad = pd.to_numeric(txt, errors="coerce")
    fecha_txt = v["fecha contabiliz."].astype(str).str.strip()
    fecha = pd.to_datetime(fecha_txt, format="ISO8601", errors="coerce")
    sin_iso = fecha.isna()
    fecha[sin_iso] = pd.to_datetime(fecha_txt[sin_iso], dayfirst=True, errors="coerce")
    d = pd.DataFrame({
        "fecha": fecha.dt.normalize(),
        "sku": v["material"].astype(str).str.strip(),
        "descripcion": v["texto breve material"].astype(str).str.strip(),
        "unidad": v["un.medida de entrada"].astype(str).str.strip(),
        "cajas": -cantidad.fillna(0),
        "documento": v["documento material"].astype(str),
    })
    return d[d["fecha"].notna() & (d["cajas"] > 0)]


def preparar_maestro(hojas: dict) -> pd.DataFrame:
    """hojas: {'ubicaciones': df, 'preferencia': df, 'zm': df} con columnas en minúsculas."""
    u = hojas["ubicaciones"].rename(columns={"nombre ubicación": "ubicacion", "area": "area",
                                             "pasillo": "pasillo", "capacidad maxima": "capacidad_txt"})
    u["capacidad"] = pd.to_numeric(u["capacidad_txt"].astype(str).str.split().str[0], errors="coerce")
    u["pasillo"] = pd.to_numeric(u["pasillo"], errors="coerce")
    p = hojas["preferencia"].rename(columns={"ubicación": "ubicacion", "número de artículo": "sku"})
    z = hojas["zm"].rename(columns={"ubicación": "ubicacion", "zona de trabajo": "zona_trabajo",
                                    "zona de movimiento": "zona_movimiento"})
    for df in (u, p, z):
        for c in ("ubicacion", "sku"):
            if c in df.columns:
                df[c] = df[c].astype(str).str.strip()
    pos = (p[["ubicacion", "sku"]]
           .merge(u[["ubicacion", "area", "pasillo", "capacidad"]], on="ubicacion", how="left")
           .merge(z[["ubicacion", "zona_trabajo", "zona_movimiento"]], on="ubicacion", how="left"))
    return pos


def maestro_desde_adc(x: pd.ExcelFile, nombres: dict) -> pd.DataFrame:
    """Posiciones de picking desde el ADC de Puerto Montt.

    Pestaña 25: SKU -> ubicación de picking. Pestaña 16: pasillo, capacidad máxima (piezas = cajas),
    zona de trabajo y de movimiento. Pestaña 6: código de zona de trabajo -> nombre del exporte.
    """
    def hoja(prefijo):
        for k, v in nombres.items():
            if k.startswith(prefijo):
                return v
        raise ValueError(f"Al ADC le falta la pestaña {prefijo}.")

    u = pd.read_excel(x, sheet_name=hoja("16-"), dtype=str)
    u = u[u["Ubicacion"].notna()]
    u = pd.DataFrame({
        "ubicacion": u["Ubicacion"].str.strip(),
        "area": u["CÓDIGO DE ÁREA"].str.strip(),
        "pasillo": u["Pasillo"].str.strip(),
        "capacidad": pd.to_numeric(u["Capacidad máxima"], errors="coerce"),
        "zona_trabajo": u["Zona de Trabajo"].str.strip(),
        "zona_movimiento": u["Zona de Movimiento"].str.strip(),
    })
    zt = pd.read_excel(x, sheet_name=hoja("6-"), header=None, dtype=str)
    zt = zt[zt[0].astype(str).str.strip().str.upper() != "ALMACEN"]
    nombres_zt = dict(zip(zt[1].astype(str).str.strip(), zt[2].astype(str).str.strip()))
    u["zona_trabajo"] = u["zona_trabajo"].map(nombres_zt).fillna(u["zona_trabajo"])

    a = pd.read_excel(x, sheet_name=hoja("25-"), header=1, dtype=str)
    a = a[a["sku"].notna()]
    p = pd.DataFrame({"ubicacion": a["Ubicación Inicial"].astype(str).str.strip(),
                      "sku": a["sku"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)})
    return p.merge(u, on="ubicacion", how="left")


def leer_maestro(contenido: bytes) -> pd.DataFrame:
    x = pd.ExcelFile(io.BytesIO(contenido))
    nombres = {s.strip().upper(): s for s in x.sheet_names}
    if any(k.startswith("16-") for k in nombres) and any(k.startswith("25-") for k in nombres):
        return maestro_desde_adc(x, nombres)
    hojas = {}
    for clave in ("UBICACIONES", "PREFERENCIA", "ZM"):
        if clave not in nombres:
            raise ValueError(f"Al maestro de ubicaciones le falta la hoja {clave}.")
        df = pd.read_excel(x, sheet_name=nombres[clave], dtype=str)
        df.columns = [str(c).strip().lower() for c in df.columns]
        hojas[clave.lower()] = df
    return preparar_maestro(hojas)


def leer_factor_pallet(contenido: bytes, nombre: str) -> pd.Series:
    """Cajas por pallet por SKU (columnas ID_SKU_INV y CAJAS_POR_PALLET)."""
    df = leer_tabla(contenido, nombre, columnas=["id_sku_inv", "cajas_por_pallet"])
    faltan = [c for c in ("id_sku_inv", "cajas_por_pallet") if c not in df.columns]
    if faltan:
        raise ValueError(f"Al archivo de cajas por pallet le faltan columnas: {', '.join(faltan)}.")
    f = pd.to_numeric(df["cajas_por_pallet"], errors="coerce")
    f.index = df["id_sku_inv"].astype(str).str.strip()
    f = f[f > 0]
    return f[~f.index.duplicated()]


def separar_pallet(venta: pd.DataFrame, factor: pd.Series | None) -> pd.DataFrame:
    """Separa cada línea en pallets completos y cajas que salen de la posición de picking.

    Supone que hay stock en ZT ALMACENAMIENTO para los pallets completos; si no lo hay,
    esas cajas también salen de picking y la demanda real sobre la posición es mayor.
    """
    v = venta.copy()
    if factor is None:
        v["cajas_pallet"] = 0.0
    else:
        cpp = v["sku"].map(factor)
        v["cajas_pallet"] = (np.floor(v["cajas"] / cpp) * cpp).fillna(0)
    v["cajas_picking"] = v["cajas"] - v["cajas_pallet"]
    return v


# ---------------------------------------------------------------- análisis

def analizar(venta: pd.DataFrame, pos: pd.DataFrame, factor: pd.Series | None = None):
    """Devuelve (detalle por SKU, días operativos considerados).

    Con factor de paletizado, la demanda sobre la posición considera solo las cajas que
    no salen como pallet completo, y una línea cuenta como visita solo si deja cajas sueltas.
    """
    if cfg.VENTANA_VENTA_SEMANAS:
        corte = venta["fecha"].max() - pd.Timedelta(weeks=cfg.VENTANA_VENTA_SEMANAS)
        venta = venta[venta["fecha"] > corte]
    lineas_dia = venta.groupby("fecha").size()
    dias_op = lineas_dia[lineas_dia >= cfg.MIN_LINEAS_DIA_OPERATIVO].index
    v = separar_pallet(venta[venta["fecha"].isin(dias_op)], factor)
    v["visita"] = (v["cajas_picking"] > 0).astype(int)

    diario = v.groupby(["sku", "fecha"]).agg(cajas=("cajas_picking", "sum"), lineas=("visita", "sum"))
    cajas_d = diario["cajas"].unstack(fill_value=0).reindex(columns=dias_op, fill_value=0)
    lineas_d = diario["lineas"].unstack(fill_value=0).reindex(columns=dias_op, fill_value=0)

    s = pd.DataFrame({
        "cajas_total": cajas_d.sum(axis=1),
        "lineas_total": lineas_d.sum(axis=1),
        "cajas_dia_prom": cajas_d.mean(axis=1),
        "cajas_dia_p90": cajas_d.quantile(cfg.PERCENTIL_DIA_ALTO, axis=1),
        "cajas_dia_max": cajas_d.max(axis=1),
        "lineas_dia_prom": lineas_d.mean(axis=1),
        "pct_dias_con_venta": (cajas_d > 0).mean(axis=1),
    }).join(v.groupby("sku").agg(descripcion=("descripcion", "first"), unidad=("unidad", "first"),
                                 cajas_venta=("cajas", "sum"), cajas_pallet=("cajas_pallet", "sum")))
    s["pct_pallet"] = np.where(s["cajas_venta"] > 0, s["cajas_pallet"] / s["cajas_venta"], 0)
    if factor is not None:
        s["cajas_por_pallet"] = s.index.map(factor)

    # ABC por frecuencia de visitas (líneas), no por volumen
    s = s.sort_values("lineas_total", ascending=False)
    acum = s["lineas_total"].cumsum() / s["lineas_total"].sum()
    s["abc"] = np.select([acum <= 0.8, acum <= 0.95], ["A", "B"], default="C")

    por_sku = pos.groupby("sku").agg(
        posiciones=("ubicacion", "size"), ubicaciones=("ubicacion", lambda x: ", ".join(sorted(x))),
        capacidad=("capacidad", "sum"), pasillo=("pasillo", "first"),
        zona_trabajo=("zona_trabajo", "first"), zona_movimiento=("zona_movimiento", "first"))
    r = s.join(por_sku, how="outer")
    r.index.name = "sku"
    r = r.reset_index()
    numericas = ["cajas_total", "lineas_total", "cajas_dia_prom", "cajas_dia_p90", "cajas_dia_max",
                 "lineas_dia_prom", "pct_dias_con_venta", "cajas_venta", "cajas_pallet", "pct_pallet"]
    r[numericas] = r[numericas].fillna(0)
    r["abc"] = r["abc"].fillna("-")
    r["reposiciones_dia_p90"] = np.where(r["capacidad"] > 0, r["cajas_dia_p90"] / r["capacidad"], np.nan)
    r["reposiciones_dia_prom"] = np.where(r["capacidad"] > 1, r["cajas_dia_prom"] / r["capacidad"], 0)
    r["cajas_por_linea"] = np.where(r["lineas_total"] > 0, r["cajas_total"] / r["lineas_total"], np.nan)
    cap_pos = r["capacidad"] / r["posiciones"]
    r["posiciones_necesarias_p90"] = np.where(cap_pos > 1, np.ceil(r["cajas_dia_p90"] / cap_pos), np.nan)
    # Sin posición y sin venta desde picking (todo sale en pallet) no es un problema de posición
    r.loc[r["posiciones"].isna() & (r["cajas_total"] == 0), "abc"] = "-"

    r["diagnostico"] = np.select(
        [r["posiciones"].isna() & (r["cajas_total"] > 0),
         r["cajas_total"] == 0,
         r["capacidad"].fillna(0) <= 1,
         r["reposiciones_dia_p90"] > 2,
         r["reposiciones_dia_p90"] > 1,
         (r["reposiciones_dia_p90"] < 0.2) & (r["abc"] == "C")],
        ["Sin posición de picking", "Posición sin venta", "Capacidad por revisar",
         "Crítico: doble posición", "Revisar: repone en turno", "Sobredimensionada"],
        default="OK")
    r["accion"] = r["diagnostico"].map(ACCION)
    r["orden"] = r["diagnostico"].map({d: i for i, d in enumerate(ORDEN)})
    r = r.sort_values(["orden", "lineas_total"], ascending=[True, False]).drop(columns="orden")
    return r.reset_index(drop=True), dias_op


def pallet_esperado(venta: pd.DataFrame, factor: pd.Series) -> pd.DataFrame:
    """Cajas y pallets que deberían salir como pallet completo, por fecha de venta."""
    v = separar_pallet(venta, factor)
    v["pallets"] = np.floor(v["cajas_pallet"] / v["sku"].map(factor)).fillna(0)
    return v.groupby("fecha").agg(cajas_esperadas=("cajas_pallet", "sum"),
                                  pallets_esperados=("pallets", "sum")).reset_index()


def comparar_pallet(esperado: pd.DataFrame, lpns: pd.DataFrame, turnos) -> pd.DataFrame:
    """Pallet completo esperado según la venta vs el que salió realmente (LPN nivel L).

    La venta de un día se pickea al día siguiente, saltando los días de config.DIAS_SIN_PICKING.
    Solo se comparan días con picking cargado.
    """
    real = (lpns[(lpns["nivel"] == "L") & lpns["turno"].isin(turnos)]
            .groupby("fecha_op").agg(cajas_reales=("cajas", "sum"), pallets_reales=("lpn", "size")))
    if real.empty or esperado.empty:
        return pd.DataFrame()
    e = esperado.copy()
    e["fecha_op"] = e["fecha"] + pd.Timedelta(days=1)
    for _ in range(3):  # saltar días sin picking (por defecto, domingo)
        salta = e["fecha_op"].dt.dayofweek.isin(cfg.DIAS_SIN_PICKING)
        e.loc[salta, "fecha_op"] += pd.Timedelta(days=1)
    e = e.groupby("fecha_op")[["cajas_esperadas", "pallets_esperados"]].sum()
    c = e.join(real, how="inner").fillna(0).reset_index()
    c["cumplimiento"] = np.where(c["cajas_esperadas"] > 0, c["cajas_reales"] / c["cajas_esperadas"], np.nan)
    c["cajas_faltantes"] = (c["cajas_esperadas"] - c["cajas_reales"]).clip(lower=0)
    return c


def resumen_diagnostico(r):
    g = r.groupby("diagnostico").agg(
        skus=("sku", "size"), skus_a=("abc", lambda x: (x == "A").sum()),
        lineas_dia=("lineas_dia_prom", "sum")).reindex(ORDEN).fillna(0).reset_index()
    g["accion"] = g["diagnostico"].map(ACCION)
    return g


def resumen_zonas(r):
    con_pos = r[r["posiciones"].notna()]
    return con_pos.groupby("zona_trabajo").agg(
        skus=("sku", "size"), lineas_dia=("lineas_dia_prom", "sum"),
        reposiciones_dia=("reposiciones_dia_prom", "sum"),
        criticos=("diagnostico", lambda x: (x == ORDEN[0]).sum()),
        revisar=("diagnostico", lambda x: (x == ORDEN[1]).sum()),
        sin_venta=("diagnostico", lambda x: (x == "Posición sin venta").sum()),
    ).reset_index().sort_values("reposiciones_dia", ascending=False)


def a_excel(r, zonas) -> bytes:
    """Excel descargable con una hoja por tipo de acción."""
    buf = io.BytesIO()
    hojas = {
        "Acciones prioritarias": r[r["diagnostico"].isin(ORDEN[:2])],
        "Sin posición": r[r["diagnostico"] == "Sin posición de picking"],
        "Liberar o reducir": r[r["diagnostico"].isin(["Posición sin venta", "Sobredimensionada"])],
        "Detalle": r,
    }
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        resumen_diagnostico(r).to_excel(xw, sheet_name="Resumen", index=False)
        for nombre, df in hojas.items():
            df.to_excel(xw, sheet_name=nombre, index=False)
        zonas.to_excel(xw, sheet_name="Por zona", index=False)
    return buf.getvalue()
