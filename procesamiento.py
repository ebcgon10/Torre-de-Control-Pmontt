"""Limpieza de exportes del WMS y cálculo de indicadores.

No depende de Streamlit: el mismo módulo lo usará el reporte automático (fase 3).
"""
import io

import numpy as np
import pandas as pd

import config as cfg

COLS_PICKING = [
    "id_de_lista", "id_de_usuario_ultima_recogida", "descripcion_usuario",
    "h_inicio", "h_termino", "cajas", "lineas", "zona_de_trabajo",
]
COLS_GRUA = [
    "codigo_de_operacion", "codigo_de_actividad", "usuario",
    "nombre_usuario", "fecha_de_transaccion", "movimientos",
]
CLAVE_OPERARIO = ["fecha_op", "turno", "usuario"]


# ---------------------------------------------------------------- lectura

def leer_csv(contenido: bytes) -> pd.DataFrame:
    """Lee un CSV del WMS detectando codificación y separador."""
    for codificacion in ("utf-8-sig", "latin-1"):
        try:
            texto = contenido.decode(codificacion)
            break
        except UnicodeDecodeError:
            continue
    primera_linea = texto.split("\n", 1)[0]
    sep = ";" if primera_linea.count(";") > primera_linea.count(",") else ","
    df = pd.read_csv(io.StringIO(texto), sep=sep, dtype=str)
    df.columns = [c.strip().lower() for c in df.columns]
    return df


def _validar_columnas(df, requeridas, nombre):
    faltan = [c for c in requeridas if c not in df.columns]
    if faltan:
        raise ValueError(
            f"Al archivo de {nombre} le faltan columnas: {', '.join(faltan)}. "
            "Revisa que sea el exporte correcto."
        )


def _minutos(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def asignar_turno(ts: pd.Series):
    """Devuelve (turno, fecha operativa) según config.TURNOS."""
    minutos = ts.dt.hour * 60 + ts.dt.minute
    fecha = ts.dt.normalize()
    turno = pd.Series("Sin turno", index=ts.index, dtype="object")
    fecha_op = fecha.copy()
    for nombre, ini, fin in cfg.TURNOS:
        s, e = _minutos(ini), _minutos(fin)
        if s < e:
            m = (minutos >= s) & (minutos < e)
            turno[m] = nombre
        else:  # cruza medianoche
            antes = minutos >= s
            despues = minutos < e
            turno[antes | despues] = nombre
            if getattr(cfg, "FECHA_OPERATIVA_AL_TERMINO", False):
                fecha_op[antes] = fecha[antes] + pd.Timedelta(days=1)
            else:
                fecha_op[despues] = fecha[despues] - pd.Timedelta(days=1)
    return turno, fecha_op


def extender_turno(listas: pd.DataFrame) -> int:
    """El turno dura hasta que se termina de pickear todo, aunque pase la hora de fin.

    Para cada turno analizado (ej. TC), las listas que empiezan después de su hora de fin se
    siguen contando en ese turno mientras el CD no quede más de CONTINUIDAD_TURNO_MIN minutos
    sin ninguna lista en curso. El primer corte así marca el fin real del picking del turno; lo
    que empieza después ya es del turno siguiente. Modifica listas en el lugar y devuelve
    cuántas listas cambiaron de turno.
    """
    corte = getattr(cfg, "CONTINUIDAD_TURNO_MIN", 0)
    if not corte or listas.empty:
        return 0
    fin_turno = {t: _minutos(f) for t, _, f in cfg.TURNOS}
    cambios = 0
    for (fecha, turno), g in listas[listas["turno"].isin(cfg.TURNOS_ANALIZADOS)].groupby(["fecha_op", "turno"]):
        limite = fecha + pd.Timedelta(minutes=fin_turno[turno])
        while limite <= g["inicio"].min():
            limite += pd.Timedelta(days=1)
        fin_actual = g["termino"].max()
        cand = listas[(listas["inicio"] >= limite) & (listas["turno"] != turno)
                      & (listas["inicio"] < limite + pd.Timedelta(days=1))].sort_values("inicio")
        for i, r in cand.iterrows():
            if (r["inicio"] - fin_actual).total_seconds() / 60 > corte:
                break  # el CD quedó sin picking: terminó el turno
            listas.at[i, "turno"], listas.at[i, "fecha_op"] = turno, fecha
            fin_actual = max(fin_actual, r["termino"])
            cambios += 1
    return cambios


# ---------------------------------------------------------------- picking

MANUAL, PALLET = "Manual", "Pallet completo"


def clasificar_tipo(zona: pd.Series) -> pd.Series:
    """Pallet completo si la zona empieza con alguno de config.PREFIJOS_PALLET_COMPLETO."""
    z = zona.str.upper()
    es_pallet = pd.Series(False, index=zona.index)
    for prefijo in cfg.PREFIJOS_PALLET_COMPLETO:
        es_pallet |= z.str.startswith(prefijo.upper())
    return np.where(es_pallet, PALLET, MANUAL)

def preparar_picking(df: pd.DataFrame):
    """Limpia CAJA_PICKEADA y la deja a nivel de lista.

    Devuelve (listas, exclusiones, avisos, totales). Cada exclusión informa registros y cajas
    por nivel de LPN (S = surtido, L = pallet completo); totales son las cajas del archivo
    original por nivel, para conciliar con lo que muestra la app.
    """
    _validar_columnas(df, COLS_PICKING, "picking")
    exclusiones, avisos = [], []

    d = df.copy()
    d["cajas"] = pd.to_numeric(d["cajas"].str.strip(), errors="coerce").fillna(0)
    d["lineas"] = pd.to_numeric(d["lineas"].str.strip(), errors="coerce").fillna(0)
    tiene_nivel = "nivel_lpn" in d.columns
    if tiene_nivel:
        d["nivel"] = d["nivel_lpn"].fillna("").str.strip().str.upper().replace("", "S")
    else:
        d["nivel"] = np.where(clasificar_tipo(d["zona_de_trabajo"].fillna("")) == PALLET, "L", "S")
    d["cajas_s"] = np.where(d["nivel"] == "L", 0, d["cajas"])
    d["cajas_l"] = np.where(d["nivel"] == "L", d["cajas"], 0)
    totales = {"cajas_s": float(d["cajas_s"].sum()), "cajas_l": float(d["cajas_l"].sum()),
               "registros": len(d)}

    def excluir(mask, motivo):
        n = int(mask.sum())
        if n:
            sub = d_ref[mask]
            exclusiones.append({"motivo": motivo, "registros": n,
                                "cajas_s": float(sub["cajas_s"].sum()),
                                "cajas_l": float(sub["cajas_l"].sum())})

    # Solo filas 100% idénticas: una misma lista puede traer el mismo LPN en más de una fila
    dup = d.duplicated(subset=[c for c in df.columns])
    d_ref = d
    excluir(dup, "Filas duplicadas idénticas")
    d = d[~dup].copy()

    d["inicio"] = pd.to_datetime(d["h_inicio"].str.strip(), format="%d/%m/%Y %H:%M", errors="coerce")
    d["termino"] = pd.to_datetime(d["h_termino"].str.strip(), format="%d/%m/%Y %H:%M", errors="coerce")
    d_ref = d
    m = d["inicio"].isna() | d["termino"].isna()
    excluir(m, "Hora de inicio o término vacía o con formato inválido")
    d = d[~m].copy()

    d["usuario"] = d["id_de_usuario_ultima_recogida"].fillna("").str.strip()
    d_ref = d
    m = d["usuario"] == ""
    excluir(m, "Sin usuario")
    d = d[~m].copy()

    d["nombre"] = d["descripcion_usuario"].fillna("").str.strip()
    d.loc[d["nombre"] == "", "nombre"] = d["usuario"]
    d["zona"] = d["zona_de_trabajo"].fillna("").str.strip().replace("", "Sin zona")
    d["es_pallet"] = d["nivel"] == "L"
    d["lineas_s"] = np.where(d["es_pallet"], 0, d["lineas"])

    agregaciones = dict(
        usuario=("usuario", "first"), nombre=("nombre", "first"), zona=("zona", "first"),
        inicio=("inicio", "min"), termino=("termino", "max"),
        cajas=("cajas", "sum"), cajas_s=("cajas_s", "sum"), cajas_l=("cajas_l", "sum"),
        lineas=("lineas", "sum"), lineas_s=("lineas_s", "sum"),
        lpns=("usuario", "size"), lpns_pallet=("es_pallet", "sum"),
    )
    if "numero_de_viaje" in d.columns:
        agregaciones["viaje"] = ("numero_de_viaje", "first")
    listas = d.groupby("id_de_lista", as_index=False).agg(**agregaciones)

    listas["dur_min"] = (listas["termino"] - listas["inicio"]).dt.total_seconds() / 60
    d_ref = listas
    m = listas["dur_min"] < 0
    excluir(m, "Término anterior al inicio (listas)")
    listas = listas[~m].copy()
    d_ref = listas
    m = listas["dur_min"] > cfg.DURACION_MAX_LISTA_MIN
    excluir(m, f"Duración mayor a {cfg.DURACION_MAX_LISTA_MIN} min (listas)")
    listas = listas[~m].copy()

    n_cero = int((listas["dur_min"] == 0).sum())
    if n_cero:
        avisos.append(
            f"{n_cero} listas duran 0 minutos (el WMS registra solo hora:minuto). "
            f"Se les asignan {cfg.DURACION_MIN_LISTA_MIN} min."
        )
    listas["dur_ef_min"] = listas["dur_min"].clip(lower=cfg.DURACION_MIN_LISTA_MIN)

    mixtas = int(((listas["lpns_pallet"] > 0) & (listas["lpns_pallet"] < listas["lpns"])).sum())
    if mixtas:
        avisos.append(f"{mixtas} listas mezclan LPN de pallet completo (L) y de surtido (S). Sus cajas se "
                      "cuentan por nivel de LPN; su tiempo se asigna al tipo mayoritario.")
    if not tiene_nivel:
        avisos.append("El archivo no trae la columna nivel_lpn: el pallet completo se identificó "
                      "por el nombre de la zona (config.PREFIJOS_PALLET_COMPLETO).")
    listas["tipo_picking"] = np.where(listas["lpns_pallet"] > listas["lpns"] / 2, PALLET, MANUAL)
    listas["turno"], listas["fecha_op"] = asignar_turno(listas["inicio"])
    n_ext = extender_turno(listas)
    if n_ext:
        avisos.append(f"{n_ext} listas empezaron después de la hora de fin del turno, pero el picking siguió sin "
                      f"cortes de más de {cfg.CONTINUIDAD_TURNO_MIN} min: se cuentan en el turno hasta que se "
                      "terminó de pickear todo.")
    n_sin = int((listas["turno"] == "Sin turno").sum())
    if n_sin:
        avisos.append(f"{n_sin} listas empiezan fuera de los turnos definidos en config.py.")

    return listas.reset_index(drop=True), exclusiones, avisos, totales


def conciliar(totales, exclusiones, listas, en_alcance) -> pd.DataFrame:
    """Cuadratura de cajas: archivo original -> exclusiones -> fuera de alcance -> lo que usa la app."""
    filas = [{"concepto": "Cajas en el archivo (todas las fechas y turnos)",
              "surtido_S": totales["cajas_s"], "pallet_L": totales["cajas_l"]}]
    for e in exclusiones:
        filas.append({"concepto": f"(-) {e['motivo']}", "surtido_S": -e["cajas_s"], "pallet_L": -e["cajas_l"]})
    fuera = listas[~en_alcance]
    filas.append({"concepto": "(-) Otras fechas o turnos (fuera del período y turno elegidos)",
                  "surtido_S": -fuera["cajas_s"].sum(), "pallet_L": -fuera["cajas_l"].sum()})
    dentro = listas[en_alcance]
    filas.append({"concepto": "(=) Cajas que usa la app en el período y turno elegidos",
                  "surtido_S": dentro["cajas_s"].sum(), "pallet_L": dentro["cajas_l"].sum()})
    return pd.DataFrame(filas)


def preparar_lpns(df: pd.DataFrame):
    """Detalle por LPN (cajas, líneas, nivel L/S). None si el archivo no trae nivel_lpn o lpn."""
    if not {"lpn", "nivel_lpn"}.issubset(df.columns):
        return None
    d = df.drop_duplicates().copy()
    d["inicio"] = pd.to_datetime(d["h_inicio"].str.strip(), format="%d/%m/%Y %H:%M", errors="coerce")
    d = d[d["inicio"].notna()].copy()
    d["nivel"] = d["nivel_lpn"].fillna("").str.strip().str.upper()
    d["zona"] = d["zona_de_trabajo"].fillna("").str.strip().replace("", "Sin zona")
    for c in ("cajas", "lineas"):
        d[c] = pd.to_numeric(d[c].str.strip(), errors="coerce").fillna(0)
    d["turno"], d["fecha_op"] = asignar_turno(d["inicio"])
    return d[["fecha_op", "turno", "zona", "id_de_lista", "lpn", "nivel", "cajas", "lineas"]].reset_index(drop=True)


def surtido_casi_pallet(lpns: pd.DataFrame) -> pd.DataFrame:
    """LPN de surtido (S) de un solo artículo y muchas cajas: pallets armados desde la posición
    de picking en vez de salir completos desde almacenamiento."""
    s = lpns[lpns["nivel"] == "S"].copy()
    s["casi_pallet"] = (s["lineas"] <= 1) & (s["cajas"] >= cfg.UMBRAL_LPN_CASI_PALLET)
    cp = s[s["casi_pallet"]]
    tipico = cp.groupby("zona")["cajas"].agg(lambda x: int(x.mode().iloc[0]) if len(x) else None)
    z = s.groupby("zona").agg(lpns_surtido=("lpn", "size"), cajas_surtido=("cajas", "sum"),
                              lpns_casi_pallet=("casi_pallet", "sum"))
    z["cajas_casi_pallet"] = cp.groupby("zona")["cajas"].sum()
    z["cajas_tipicas"] = tipico
    z = z.fillna({"cajas_casi_pallet": 0}).reset_index()
    z["pct_cajas_casi_pallet"] = np.where(z["cajas_surtido"] > 0, z["cajas_casi_pallet"] / z["cajas_surtido"], 0)
    return z[z["lpns_casi_pallet"] > 0].sort_values("cajas_casi_pallet", ascending=False)


def _clasificar(minutos: pd.Series) -> np.ndarray:
    return np.select([minutos < cfg.UMBRAL_ESPERA_MIN, minutos < cfg.UMBRAL_PAUSA_MIN],
                     ["Normal", "Espera"], default="Pausa")


def _ventana_colacion(b: pd.DataFrame, turno: str):
    """Inicio y fin (datetime) de la ventana de colación del turno, para cada brecha."""
    ini, fin = cfg.COLACION_POR_TURNO[turno]["ventana"]
    inicio_turno = dict((t, _minutos(i)) for t, i, _ in cfg.TURNOS).get(turno, 0)
    desfase = pd.Timedelta(days=1) if _minutos(ini) < inicio_turno else pd.Timedelta(0)
    base = b["fecha_op"] + desfase
    return (base + pd.Timedelta(minutes=_minutos(ini)), base + pd.Timedelta(minutes=_minutos(fin)))


def calcular_brechas(listas: pd.DataFrame) -> pd.DataFrame:
    """Tiempo entre el fin de una lista y el inicio de la siguiente, por operario y turno.

    En turnos con colación configurada (config.COLACION_POR_TURNO), la parte de cada brecha que
    cae dentro de la ventana de colación se marca como Colación, hasta los minutos que le
    corresponden a cada operario; el resto de esa brecha se clasifica como Normal, Espera o
    Pausa según lo que dure. Una brecha puede quedar partida en dos filas.
    En turnos sin colación configurada, la pausa más larga entre COLACION_MIN_MIN y
    COLACION_MAX_MIN se toma como colación.
    """
    d = listas.sort_values(CLAVE_OPERARIO + ["inicio"]).copy()
    d["fin_anterior"] = d.groupby(CLAVE_OPERARIO)["termino"].shift()
    b = d[d["fin_anterior"].notna()].copy()
    b["brecha_min"] = (b["inicio"] - b["fin_anterior"]).dt.total_seconds() / 60
    b["solapada"] = b["brecha_min"] < 0
    b["brecha_min"] = b["brecha_min"].clip(lower=0)
    b["colacion_min"] = 0.0

    for turno, conf in cfg.COLACION_POR_TURNO.items():
        m = b["turno"] == turno
        if not m.any():
            continue
        v_ini, v_fin = _ventana_colacion(b[m], turno)
        desde = np.maximum(b.loc[m, "fin_anterior"], v_ini)
        hasta = np.minimum(b.loc[m, "inicio"], v_fin)
        traslape = ((hasta - desde).dt.total_seconds() / 60).clip(lower=0)
        # Huecos cortos (traslados normales) no cuentan como colación aunque caigan en la ventana
        traslape = traslape.where(b.loc[m, "brecha_min"] >= cfg.COLACION_BRECHA_MINIMA_MIN, 0)
        # Cada operario tiene derecho a conf["minutos"] de colación dentro de la ventana
        previo = traslape.groupby([b.loc[m, c] for c in CLAVE_OPERARIO]).cumsum() - traslape
        b.loc[m, "colacion_min"] = np.minimum(traslape, (conf["minutos"] - previo).clip(lower=0))

    b["resto_min"] = b["brecha_min"] - b["colacion_min"]
    b["tipo"] = _clasificar(b["resto_min"])

    col = b[b["colacion_min"] > 0].copy()
    col["brecha_min"], col["tipo"], col["solapada"] = col["colacion_min"], "Colación", False
    resto = b[(b["colacion_min"] == 0) | (b["resto_min"] > 0)].copy()
    resto["brecha_min"] = resto["resto_min"]
    b = pd.concat([resto, col])

    if getattr(cfg, "COLACION_AUTOMATICA", True):
        sin_conf = ~b["turno"].isin(list(cfg.COLACION_POR_TURNO))
        cand = b[sin_conf & (b["tipo"] == "Pausa")
                 & b["brecha_min"].between(cfg.COLACION_MIN_MIN, cfg.COLACION_MAX_MIN)]
        if len(cand):
            b.loc[cand.groupby(CLAVE_OPERARIO)["brecha_min"].idxmax(), "tipo"] = "Colación"

    b["hora"] = b["fin_anterior"].dt.hour
    b = b.sort_values(CLAVE_OPERARIO + ["fin_anterior"]).reset_index(drop=True)
    return b[CLAVE_OPERARIO + ["nombre", "zona", "id_de_lista", "fin_anterior", "inicio",
                               "brecha_min", "tipo", "solapada", "hora"]]


def _minutos_por_tipo(brechas, claves):
    if brechas.empty:
        return pd.DataFrame(columns=claves)
    p = brechas.pivot_table(index=claves, columns="tipo", values="brecha_min",
                            aggfunc="sum", fill_value=0)
    p.columns = [f"min_{c.lower().replace('ó', 'o')}" for c in p.columns]
    return p.reset_index()


def _asegurar_columnas(df, columnas):
    for c in columnas:
        if c not in df.columns:
            df[c] = 0.0
    return df


TIPOS_MIN = ["min_normal", "min_espera", "min_pausa", "min_colacion"]


def _por_tipo(listas, claves):
    """Listas, cajas y minutos separados en manual y pallet completo."""
    # Las cajas se cuentan por nivel de LPN (S/L) en todas las listas, así una lista mixta no
    # pierde cajas; listas, minutos y operarios se asignan según el tipo mayoritario de la lista.
    partes = [listas.groupby(claves).agg(
        cajas_manual=("cajas_s", "sum"), cajas_pallet=("cajas_l", "sum"),
        lineas_manual=("lineas_s", "sum"), lpns_pallet=("lpns_pallet", "sum"))]
    for tipo, suf in ((MANUAL, "manual"), (PALLET, "pallet")):
        sub = listas[listas["tipo_picking"] == tipo]
        partes.append(sub.groupby(claves).agg(**{
            f"listas_{suf}": ("id_de_lista", "size"),
            f"min_{suf}": ("dur_ef_min", "sum"),
            f"operarios_{suf}": ("usuario", "nunique"),
        }))
    return pd.concat(partes, axis=1).fillna(0)


def resumen_operarios(listas, brechas):
    """Una fila por operario, fecha operativa y turno. Considera todas sus listas."""
    op = listas.groupby(CLAVE_OPERARIO + ["nombre"]).agg(
        primera=("inicio", "min"), ultima=("termino", "max"),
    ).reset_index()
    op = op.join(_por_tipo(listas, CLAVE_OPERARIO), on=CLAVE_OPERARIO)
    op["min_efectivos"] = op["min_manual"] + op["min_pallet"]
    op = op.merge(_minutos_por_tipo(brechas, CLAVE_OPERARIO), on=CLAVE_OPERARIO, how="left")
    op = _asegurar_columnas(op, TIPOS_MIN).fillna({c: 0 for c in TIPOS_MIN})

    inicio_turno = listas.groupby(["fecha_op", "turno"])["inicio"].min().rename("inicio_turno")
    op = op.join(inicio_turno, on=["fecha_op", "turno"])
    # Horas-hombre de cada operario = ventana de picking manual de su turno, igual que el Power BI
    man = listas[listas["tipo_picking"] == MANUAL]
    ventana = man.groupby(["fecha_op", "turno"]).agg(i=("inicio", "min"), f=("termino", "max"))
    ventana = ((ventana["f"] - ventana["i"]).dt.total_seconds() / 3600).rename("horas_turno")
    op = op.join(ventana, on=["fecha_op", "turno"])
    op["horas_turno"] = op["horas_turno"].fillna(0)
    op["min_inicio_tardio"] = (op["primera"] - op["inicio_turno"]).dt.total_seconds() / 60
    op["min_en_piso"] = (op["ultima"] - op["primera"]).dt.total_seconds() / 60
    op["min_disponibles"] = (op["min_en_piso"] - op["min_colacion"]).clip(lower=0)
    return op.drop(columns="inicio_turno")


def consolidar_operarios(op):
    """Suma el detalle diario por operario para el período filtrado."""
    g = op.groupby(["usuario", "nombre"]).agg(
        turnos=("fecha_op", "size"),
        listas_manual=("listas_manual", "sum"), cajas_manual=("cajas_manual", "sum"),
        listas_pallet=("listas_pallet", "sum"), cajas_pallet=("cajas_pallet", "sum"),
        min_manual=("min_manual", "sum"), min_efectivos=("min_efectivos", "sum"),
        min_disponibles=("min_disponibles", "sum"), min_espera=("min_espera", "sum"),
        min_pausa=("min_pausa", "sum"), min_inicio_tardio=("min_inicio_tardio", "mean"),
        horas_turno=("horas_turno", "sum"),
    ).reset_index()
    g["cj_hh_total"] = np.where(g["horas_turno"] > 0, g["cajas_manual"] / g["horas_turno"], np.nan)
    g["horas_efectivas"] = g["min_efectivos"] / 60
    g["horas_disponibles"] = g["min_disponibles"] / 60
    g["utilizacion"] = np.where(g["min_disponibles"] > 0,
                                g["min_efectivos"] / g["min_disponibles"], np.nan)
    g["cj_h_manual"] = np.where(g["min_manual"] > 0,
                                g["cajas_manual"] / (g["min_manual"] / 60), np.nan)
    return g.sort_values("utilizacion")


def resumen_turnos(listas, brechas):
    """Una fila por fecha operativa y turno.

    La productividad (cj/HH) se calcula solo sobre picking manual, igual que el
    Power BI; el pallet completo se informa aparte.
    """
    claves = ["fecha_op", "turno"]
    man = listas[listas["tipo_picking"] == MANUAL]
    t = listas.groupby(claves).agg(inicio_total=("inicio", "min"), fin_total=("termino", "max"))
    t = t.join(man.groupby(claves).agg(inicio=("inicio", "min"), fin=("termino", "max")))
    t = t.join(_por_tipo(listas, claves)).reset_index()
    t = t.merge(_minutos_por_tipo(brechas, claves), on=claves, how="left")
    t = _asegurar_columnas(t, TIPOS_MIN).fillna({c: 0 for c in TIPOS_MIN})

    t["horas_ventana"] = ((t["fin"] - t["inicio"]).dt.total_seconds() / 3600).fillna(0)
    # Igual que el BI: horas-hombre = ventana de picking manual x operarios de picking manual
    t["horas_hombre"] = t["horas_ventana"] * t["operarios_manual"]
    t["cj_hh_total"] = np.where(t["horas_hombre"] > 0, t["cajas_manual"] / t["horas_hombre"], np.nan)
    t["cj_hh_efectiva"] = np.where(t["min_manual"] > 0,
                                   t["cajas_manual"] / (t["min_manual"] / 60), np.nan)
    total = t["cajas_manual"] + t["cajas_pallet"]
    t["pct_pallet"] = np.where(total > 0, t["cajas_pallet"] / total, np.nan)
    return t


def resumen_zonas(listas, brechas, col_cajas="cajas"):
    z = listas.groupby("zona").agg(
        listas=("id_de_lista", "size"), cajas=(col_cajas, "sum"), lineas=("lineas", "sum"),
        lpns=("lpns", "sum"), min_efectivos=("dur_ef_min", "sum"), min_por_lista=("dur_min", "mean"),
    ).reset_index()
    # La espera antes de una lista se atribuye a la zona de esa lista
    esp = brechas[brechas["tipo"].isin(["Espera", "Pausa"])].groupby("zona")["brecha_min"].sum()
    z["min_espera_antes"] = z["zona"].map(esp).fillna(0)
    z["cj_h_efectiva"] = z["cajas"] / (z["min_efectivos"] / 60)
    z["cajas_por_lista"] = z["cajas"] / z["listas"]
    z["cajas_por_linea"] = np.where(z["lineas"] > 0, z["cajas"] / z["lineas"], np.nan)
    z["min_por_lpn"] = z["min_efectivos"] / z["lpns"]
    return z.sort_values("cj_h_efectiva")


# ---------------------------------------------------------------- grúa

def preparar_grua(df: pd.DataFrame) -> pd.DataFrame:
    _validar_columnas(df, COLS_GRUA, "movimientos de grúa")
    d = df.copy()
    d["fecha"] = pd.to_datetime(d["fecha_de_transaccion"].str.strip(), format="%d/%m/%Y", errors="coerce")
    d = d[d["fecha"].notna()].copy()
    d["movimientos"] = pd.to_numeric(d["movimientos"], errors="coerce").fillna(0)
    op = d["codigo_de_operacion"].fillna("").str.lower()
    act = d["codigo_de_actividad"].fillna("").str.lower()
    d["tipo"] = np.select(
        [op.str.contains("no dirigid") | act.str.contains("no dirigid"),
         op.str.contains("reabast") | act.str.contains("reabastecimiento"),
         op.str.contains("recogida")],
        ["No dirigido", "Reabastecimiento dirigido", "Recogida almacenamiento"],
        default="Otro",
    )
    d["nombre"] = d["nombre_usuario"].fillna(d["usuario"]).str.strip()
    iso = d["fecha"].dt.isocalendar()
    d["semana"] = iso["year"].astype(str) + "-S" + iso["week"].astype(str).str.zfill(2)
    return d


def resumen_grua_semanal(g):
    s = g.pivot_table(index="semana", columns="tipo", values="movimientos",
                      aggfunc="sum", fill_value=0)
    s["total"] = s.sum(axis=1)
    s["pct_no_dirigido"] = s.get("No dirigido", 0) / s["total"]
    return s.reset_index()


def resumen_grua_usuarios(g):
    u = g.pivot_table(index=["usuario", "nombre"], columns="tipo", values="movimientos",
                      aggfunc="sum", fill_value=0)
    u["total"] = u.sum(axis=1)
    u["pct_no_dirigido"] = u.get("No dirigido", 0) / u["total"]
    return u.reset_index().sort_values("total", ascending=False)


# ---------------------------------------------------------------- alertas

def generar_alertas(turnos, operarios, zonas, grua_semanal=None):
    """Lista de (nivel, mensaje). nivel: 'error' | 'warning' | 'success'."""
    alertas = []

    hh = turnos["horas_hombre"].sum()
    if hh > 0:
        prod = turnos["cajas_manual"].sum() / hh
        if prod < cfg.META_ICEO:
            alertas.append(("error", f"Productividad total de {prod:.0f} cj/HH, bajo la meta ICEO de {cfg.META_ICEO}."))
        elif prod < cfg.META_ICEO * 1.05:
            alertas.append(("warning", f"Productividad total de {prod:.0f} cj/HH, apenas sobre la meta ICEO de {cfg.META_ICEO}."))

        pct_ef = turnos["min_manual"].sum() / 60 / hh
        if pct_ef < 0.6:
            alertas.append(("warning", f"Solo el {pct_ef:.0%} de las horas-hombre de picking manual se usan dentro de listas."))

    esperas = turnos["min_espera"].sum() + turnos["min_pausa"].sum()
    if esperas > 0:
        alertas.append(("warning", f"{esperas / 60:.1f} horas-hombre en esperas y pausas entre listas (sin contar colación)."))

    bajo = zonas[zonas["cj_h_efectiva"] < cfg.META_ICEO]
    for _, z in bajo.iterrows():
        alertas.append(("error", f"{z['zona']} rinde {z['cj_h_efectiva']:.0f} cj/h en tiempo efectivo: bajo la meta incluso sin contar esperas."))

    bajos = operarios[operarios["utilizacion"] < cfg.UMBRAL_UTILIZACION]
    if len(bajos):
        nombres = ", ".join(bajos["nombre"].head(5))
        extra = f" y {len(bajos) - 5} más" if len(bajos) > 5 else ""
        alertas.append(("warning", f"{len(bajos)} operarios con menos del {cfg.UMBRAL_UTILIZACION:.0%} de su tiempo en listas: {nombres}{extra}."))

    if grua_semanal is not None and len(grua_semanal):
        ult = grua_semanal.iloc[-1]
        if ult["pct_no_dirigido"] > cfg.UMBRAL_NO_DIRIGIDO:
            alertas.append(("error", f"Semana {ult['semana']}: {ult['pct_no_dirigido']:.0%} de los movimientos de grúa son no dirigidos."))
        if len(grua_semanal) > 1:
            prev = grua_semanal.iloc[-2]
            nd_ult, nd_prev = ult.get("No dirigido", 0), prev.get("No dirigido", 0)
            if nd_prev > 0 and nd_ult / nd_prev > 1.1:
                alertas.append(("warning", f"Movimientos no dirigidos subieron {nd_ult / nd_prev - 1:.0%} respecto a la semana {prev['semana']}."))

    if not alertas:
        alertas.append(("success", "Sin alertas para el período seleccionado."))
    return alertas
