import pandas as pd
import plotly.express as px
import streamlit as st

import config as cfg
import procesamiento as proc

st.set_page_config(page_title=f"Torre de control WMS · {cfg.CD_NOMBRE}", page_icon="🏭", layout="wide")

VERDE = "#174A39"
COLORES_TIPO = {"Normal": "#9DB8AE", "Espera": "#E0A526", "Pausa": "#C4452F", "Colación": "#8A8F98"}
COLORES_GRUA = {"No dirigido": "#C4452F", "Reabastecimiento dirigido": VERDE,
                "Recogida almacenamiento": "#6F8FAF", "Otro": "#B8BDC4"}


def fmt_num(x, dec=0):
    return f"{x:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")


@st.cache_data(show_spinner="Procesando picking...")
def cargar_picking(contenidos: tuple):
    df = pd.concat([proc.leer_csv(c) for c in contenidos], ignore_index=True)
    listas, exclusiones, avisos, totales = proc.preparar_picking(df)
    brechas = proc.calcular_brechas(listas)
    lpns = proc.preparar_lpns(df)
    if lpns is not None:  # mismo turno y fecha que su lista (incluye la extensión del turno)
        mapa = listas.set_index("id_de_lista")[["turno", "fecha_op"]]
        en = lpns["id_de_lista"].isin(mapa.index)
        lpns.loc[en, "turno"] = lpns.loc[en, "id_de_lista"].map(mapa["turno"])
        lpns.loc[en, "fecha_op"] = lpns.loc[en, "id_de_lista"].map(mapa["fecha_op"])
    return listas, brechas, exclusiones, avisos, lpns, totales


@st.cache_data(show_spinner="Leyendo cuadratura de venta...")
def cargar_cuadratura(cuad: tuple, cort: tuple):
    import informe_pdf
    return informe_pdf.leer_cuadratura(cuad), informe_pdf.leer_cortados(cort)


@st.cache_data(show_spinner="Procesando movimientos de grúa...")
def cargar_grua(contenidos: tuple):
    df = pd.concat([proc.leer_csv(c) for c in contenidos], ignore_index=True)
    return proc.preparar_grua(df.drop_duplicates())


# ---------------------------------------------------------------- origen de datos
def secretos_drive():
    try:
        return st.secrets["gcp_json"], st.secrets["drive_folder_id"]
    except (KeyError, FileNotFoundError):
        return None


@st.cache_resource
def cliente_drive(credenciales_json):
    import drive
    return drive.conectar(credenciales_json)


@st.cache_data(ttl=3600, show_spinner="Buscando archivos en Drive...")
def listar_drive(credenciales_json, carpeta_id):
    import drive
    return drive.listar_archivos(cliente_drive(credenciales_json), carpeta_id)


@st.cache_data(show_spinner=False, max_entries=200)
def bajar_drive(credenciales_json, archivo_id, modificado):
    # "modificado" forma parte de la clave de caché: si el archivo cambia en Drive, se vuelve a bajar
    import drive
    return drive.descargar(cliente_drive(credenciales_json), archivo_id)


@st.cache_data(show_spinner="Analizando posiciones de picking...")
def cargar_posiciones(ventas: tuple, maestro: bytes | None, factor: tuple | None):
    import posiciones as pos
    v = pd.concat([pos.leer_tabla(b, n, columnas=pos.COLS_VENTA) for b, n in ventas], ignore_index=True)
    v = pos.preparar_venta(v.drop_duplicates(subset=["documento material", "material"]))
    import maestro_adc
    # Sin ADC ni cajas por pallet en Drive se usan los del ADC de Puerto Montt embebidos en la app
    f = pos.leer_factor_pallet(*factor) if factor else maestro_adc.cajas_por_pallet()
    m = pos.leer_maestro(maestro) if maestro is not None else maestro_adc.posiciones()
    r, dias = pos.analizar(v, m, f)
    esperado = pos.pallet_esperado(v, f) if f is not None else None
    return r, pos.resumen_zonas(r), dias.min(), dias.max(), len(dias), esperado


def nombre_norm(nombre):
    return nombre.upper().replace("_", " ")


conf_drive = secretos_drive()
contenidos_pick, contenidos_grua, contenidos_venta, contenido_maestro, factor_pallet = (), (), (), None, None
contenidos_cuad, contenidos_cort = (), ()

with st.sidebar:
    st.header("Datos")
    opciones = (["Google Drive", "Subir archivos"] if conf_drive else ["Subir archivos"])
    origen = st.radio("Origen", opciones, horizontal=True)

    if origen == "Google Drive":
        cred, carpeta = conf_drive
        if st.button("Actualizar desde Drive", use_container_width=True):
            listar_drive.clear()
        try:
            archivos = listar_drive(cred, carpeta)
        except Exception as e:  # credenciales, permisos o red
            st.error(f"No pude leer la carpeta de Drive: {e}")
            st.stop()
        carpetas_ok = [c.strip().upper() for c in getattr(cfg, "CARPETAS_DRIVE", [])]
        if carpetas_ok:
            archivos = [f for f in archivos if f["carpeta"].split("/")[0].strip().upper() in carpetas_ok]
        es_csv = lambda f: f["name"].lower().endswith(".csv")
        a_pick = [f for f in archivos if nombre_norm(cfg.PATRON_PICKING) in nombre_norm(f["name"]) and es_csv(f)]
        a_grua = [f for f in archivos if nombre_norm(cfg.PATRON_GRUA) in nombre_norm(f["name"]) and es_csv(f)]
        a_venta = [f for f in archivos if nombre_norm(cfg.PATRON_VENTA) in nombre_norm(f["name"])]
        a_maestro = sorted([f for f in archivos if nombre_norm(cfg.PATRON_MAESTRO) in nombre_norm(f["name"])
                            and f["name"].lower().endswith(".xlsx")], key=lambda f: f["modifiedTime"])[-1:]
        a_factor = sorted([f for f in archivos if nombre_norm(cfg.PATRON_FACTOR_PALLET) in nombre_norm(f["name"])],
                          key=lambda f: f["modifiedTime"])[-1:]
        por_fecha = lambda lst: sorted(lst, key=lambda f: f["modifiedTime"])
        a_cuad = por_fecha([f for f in archivos if nombre_norm(cfg.PATRON_CUADRATURA) in nombre_norm(f["name"])
                            and es_csv(f)])
        a_cort = por_fecha([f for f in archivos if nombre_norm(cfg.PATRON_CORTADOS) in nombre_norm(f["name"])
                            and es_csv(f)])
        # La cuadratura también tiene "VENTA" en el nombre: no es la venta de SAP para posiciones
        a_venta = [f for f in a_venta if f not in a_factor and f not in a_cuad]
        todos = a_pick + a_grua + a_venta + a_maestro + a_factor + a_cuad + a_cort
        with st.spinner(f"Descargando {len(todos)} archivos..."):
            contenidos_pick = tuple(bajar_drive(cred, f["id"], f["modifiedTime"]) for f in a_pick)
            contenidos_grua = tuple(bajar_drive(cred, f["id"], f["modifiedTime"]) for f in a_grua)
            contenidos_venta = tuple((bajar_drive(cred, f["id"], f["modifiedTime"]), f["name"]) for f in a_venta)
            contenidos_cuad = tuple(bajar_drive(cred, f["id"], f["modifiedTime"]) for f in a_cuad)
            contenidos_cort = tuple(bajar_drive(cred, f["id"], f["modifiedTime"]) for f in a_cort)
            if a_maestro:
                contenido_maestro = bajar_drive(cred, a_maestro[0]["id"], a_maestro[0]["modifiedTime"])
            if a_factor:
                factor_pallet = (bajar_drive(cred, a_factor[0]["id"], a_factor[0]["modifiedTime"]),
                                 a_factor[0]["name"])
        if carpetas_ok:
            st.caption(f"{cfg.CD_NOMBRE if hasattr(cfg, 'CD_NOMBRE') else ''} · carpetas: {', '.join(cfg.CARPETAS_DRIVE)}")
        st.caption(f"En Drive: {len(a_pick)} de picking, {len(a_grua)} de grúa, {len(a_venta)} de venta, "
                   f"{len(a_cuad)} de cuadratura y {len(a_cort)} de pedidos cortados, "
                   f"ADC y cajas por pallet: {'Drive' if a_maestro else 'incluidos en la app'}. "
                   "La lista se refresca sola cada hora o con el botón.")
        with st.expander("Ver archivos"):
            for f in todos:
                st.caption(f"{f['carpeta']}{f['name']}")
    else:
        arch_pick = st.file_uploader("Picking (CAJA_PICKEADA)", type="csv", accept_multiple_files=True)
        arch_grua = st.file_uploader("Movimientos de grúa", type="csv", accept_multiple_files=True)
        st.caption("Puedes subir varios archivos de cada tipo; los registros repetidos se eliminan.")
        contenidos_pick = tuple(f.getvalue() for f in arch_pick or [])
        contenidos_grua = tuple(f.getvalue() for f in arch_grua or [])
        arch_venta = st.file_uploader("Venta (para posiciones)", type=["csv", "xlsx"], accept_multiple_files=True)
        arch_maestro = st.file_uploader("ADC Puerto Montt (maestro de ubicaciones)", type="xlsx")
        contenidos_venta = tuple((f.getvalue(), f.name) for f in arch_venta or [])
        contenido_maestro = arch_maestro.getvalue() if arch_maestro else None
        arch_factor = st.file_uploader("Cajas por pallet (CAJAS_X_PALLET)", type=["csv", "xlsx"])
        factor_pallet = (arch_factor.getvalue(), arch_factor.name) if arch_factor else None
        arch_cuad = st.file_uploader("Cuadratura de venta (informe diario)", type="csv", accept_multiple_files=True)
        arch_cort = st.file_uploader("Pedidos cortados (informe diario)", type="csv", accept_multiple_files=True)
        contenidos_cuad = tuple(f.getvalue() for f in arch_cuad or [])
        contenidos_cort = tuple(f.getvalue() for f in arch_cort or [])

st.title("Torre de control WMS")
st.caption(f"{cfg.CD_NOMBRE} · Turno {', '.join(cfg.TURNOS_ANALIZADOS)}")

if not contenidos_pick:
    st.info("No hay archivos de picking. Sube uno o más CAJA_PICKEADA en la barra lateral, "
            "o déjalos en la carpeta de Drive. El archivo de grúa es opcional.")
    st.stop()

try:
    listas, brechas, exclusiones, avisos, lpns, totales = cargar_picking(contenidos_pick)
except ValueError as e:
    st.error(str(e))
    st.stop()

grua = None
if contenidos_grua:
    try:
        grua = cargar_grua(contenidos_grua)
    except ValueError as e:
        st.error(str(e))

# ---------------------------------------------------------------- filtros
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]

foco = listas["turno"].isin(cfg.TURNOS_ANALIZADOS)
if not foco.any():
    st.warning(f"El archivo no tiene listas del turno {', '.join(cfg.TURNOS_ANALIZADOS)}.")
    st.stop()
fechas = sorted(listas.loc[foco, "fecha_op"].dt.date.unique())

with st.sidebar:
    st.header("Filtros")
    modo = st.radio("Período", ["Día", "Semana", "Mes", "Rango"], horizontal=True)
    if modo == "Día":
        dia = st.selectbox("Fecha operativa", fechas[::-1], format_func=lambda f: f.strftime("%d/%m/%Y"))
        rango = (dia, dia)
    elif modo == "Semana":
        lunes = sorted({f - pd.Timedelta(days=f.weekday()) for f in fechas}, reverse=True)
        sem = st.selectbox(
            "Semana", lunes,
            format_func=lambda l: f"Semana {l.isocalendar()[1]} · {l:%d/%m} al "
                                  f"{l + pd.Timedelta(days=6):%d/%m/%Y}")
        rango = (sem, sem + pd.Timedelta(days=6))
    elif modo == "Mes":
        meses = sorted({(f.year, f.month) for f in fechas}, reverse=True)
        a, m = st.selectbox("Mes", meses, format_func=lambda x: f"{MESES[x[1] - 1].capitalize()} {x[0]}")
        fin_mes = pd.Timestamp(year=a, month=m, day=1) + pd.offsets.MonthEnd(0)
        rango = (pd.Timestamp(year=a, month=m, day=1).date(), fin_mes.date())
    else:
        rango = st.date_input("Desde / hasta", value=(fechas[0], fechas[-1]),
                              min_value=fechas[0], max_value=fechas[-1], format="DD/MM/YYYY")
        st.caption("Puedes elegir fechas de meses distintos.")

if not isinstance(rango, tuple) or len(rango) != 2:
    st.info("Elige la fecha de término del rango.")
    st.stop()
desde, hasta = pd.Timestamp(rango[0]), pd.Timestamp(rango[1])


def en_alcance(df):
    return (df["fecha_op"].between(desde, hasta) & df["turno"].isin(cfg.TURNOS_ANALIZADOS)
            & ~df["zona"].isin(cfg.ZONAS_EXCLUIDAS))


def filtrar(df):
    return df[en_alcance(df)]


def tabla(df, **kwargs):
    """Tabla completa, sin barra de desplazamiento vertical."""
    st.dataframe(df, hide_index=True, use_container_width=True,
                 height=int(35 * (len(df) + 1) + 3), **kwargs)


L, B = filtrar(listas), filtrar(brechas)
if L.empty:
    st.warning("No hay listas para el período elegido. Elige otro período.")
    st.stop()

op_diario = proc.resumen_operarios(L, B)
operarios = proc.consolidar_operarios(op_diario)
operarios["utilizacion_pct"] = operarios["utilizacion"] * 100
operarios["vs_meta"] = operarios["cj_hh_total"] - cfg.META_ICEO
operarios = operarios.sort_values("cj_hh_total", ascending=False)
turnos = proc.resumen_turnos(L, B)
turnos["pct_pallet_pct"] = turnos["pct_pallet"] * 100
L_man = L[L["tipo_picking"] == proc.MANUAL]
L_pal = L[L["tipo_picking"] == proc.PALLET]
zonas = proc.resumen_zonas(L_man, B, "cajas_s")
zonas_pallet = proc.resumen_zonas(L_pal, B, "cajas_l")

# Historial completo (sin filtro de fecha) para el gráfico de rendimiento
alcance_hist = listas["turno"].isin(cfg.TURNOS_ANALIZADOS) & ~listas["zona"].isin(cfg.ZONAS_EXCLUIDAS)
alcance_hist_b = brechas["turno"].isin(cfg.TURNOS_ANALIZADOS) & ~brechas["zona"].isin(cfg.ZONAS_EXCLUIDAS)
turnos_hist = proc.resumen_turnos(listas[alcance_hist], brechas[alcance_hist_b])

G, grua_sem = None, None
if grua is not None:
    G = grua[grua["fecha"].between(desde, hasta)]
    grua_sem = proc.resumen_grua_semanal(grua[grua["fecha"] <= hasta])

Lp = None
casi = pd.DataFrame()
if lpns is not None:
    Lp = lpns[lpns["fecha_op"].between(desde, hasta) & lpns["turno"].isin(cfg.TURNOS_ANALIZADOS)
              & ~lpns["zona"].isin(cfg.ZONAS_EXCLUIDAS)]
    casi = proc.surtido_casi_pallet(Lp)

# Posiciones (se cargan antes para poder juntar sus alertas)
import posiciones as posmod
det = None
error_pos = None
if contenidos_venta:
    try:
        det, zonas_pos, v_desde, v_hasta, n_dias, esperado = cargar_posiciones(
            contenidos_venta, contenido_maestro, factor_pallet)
    except ValueError as e:
        error_pos = str(e)
comp = pd.DataFrame()
if det is not None and esperado is not None and lpns is not None:
    comp = posmod.comparar_pallet(esperado, lpns, cfg.TURNOS_ANALIZADOS)

periodo = (desde.strftime("%d/%m/%Y") if desde == hasta
           else f"{desde:%d/%m/%Y} al {hasta:%d/%m/%Y}")

# ---------------------------------------------------------------- alertas (todas)
alertas = []  # (nivel, sección, texto)
for nivel, texto in proc.generar_alertas(turnos, operarios, zonas, grua_sem):
    if nivel != "success":
        alertas.append((nivel, "Grúa" if "grúa" in texto.lower() else "Rendimiento", texto))
if not casi.empty:
    alertas.append(("warning", "Pallet completo",
                    f"{int(casi['lpns_casi_pallet'].sum())} LPN de surtido casi pallet "
                    f"({fmt_num(casi['cajas_casi_pallet'].sum())} cajas) en el período: "
                    f"{', '.join(casi['zona'].head(3))}. Revisar volumetría o stock en almacenamiento."))
if not comp.empty:
    cp_per = comp[comp["fecha_op"].between(desde, hasta)]
    bajos = cp_per[cp_per["cumplimiento"] < cfg.UMBRAL_CUMPLIMIENTO_PALLET]
    for _, r in bajos.iterrows():
        alertas.append(("warning", "Pallet completo",
                        f"{r['fecha_op']:%d/%m}: salió como pallet completo el {r['cumplimiento']:.0%} de lo "
                        f"esperado; {fmt_num(r['cajas_faltantes'])} cajas se armaron desde surtido."))
if det is not None:
    res_d = posmod.resumen_diagnostico(det).set_index("diagnostico")
    for d, txt in [(posmod.ORDEN[0], "SKUs críticos que necesitan doble posición"),
                   (posmod.ORDEN[2], "SKUs con venta y sin posición de picking")]:
        if res_d.loc[d, "skus"] > 0:
            alertas.append(("error", "Posiciones",
                            f"{int(res_d.loc[d, 'skus'])} {txt} ({int(res_d.loc[d, 'skus_a'])} clase A)."))
    rep = res_d.loc[posmod.ORDEN[1]]
    if rep["skus"] > 0:
        alertas.append(("warning", "Posiciones",
                        f"{int(rep['skus'])} SKUs se vacían dentro del turno en días altos "
                        f"({fmt_num(rep['lineas_dia'])} líneas por día)."))
excl_total = sum(e["registros"] for e in exclusiones)
if excl_total:
    alertas.append(("warning", "Calidad de datos",
                    f"{excl_total} registros excluidos del archivo de picking (ver detalle en Calidad de datos)."))

# ---------------------------------------------------------------- venta del día e informe PDF
venta_dia, error_cuad = None, None
if desde == hasta and contenidos_cuad:
    try:
        import informe_pdf
        cuad, cort = cargar_cuadratura(contenidos_cuad, contenidos_cort)
        venta_dia = informe_pdf.resumen_venta(
            cuad, cort if contenidos_cort else None, desde,
            turnos["cajas_manual"].sum() + turnos["cajas_pallet"].sum())
    except ValueError as e:
        error_cuad = str(e)


def armar_informe():
    import informe_pdf
    hh = turnos["horas_hombre"].sum()
    h_ef = turnos["min_manual"].sum() / 60
    cj = turnos["cajas_manual"].sum()
    cj_pal = turnos["cajas_pallet"].sum()
    kpi = {"cajas": cj, "operarios": int(L_man["usuario"].nunique()),
           "cj_hh_total": cj / hh if hh else 0, "cj_hh_efectiva": cj / h_ef if h_ef else 0,
           "tiempo_listas": h_ef / hh if hh else 0, "cajas_pallet": cj_pal,
           "pallets": turnos["lpns_pallet"].sum(), "pct_pallet": cj_pal / (cj + cj_pal) if cj + cj_pal else 0,
           "operarios_pallet": int(L_pal["usuario"].nunique())}
    h = turnos_hist[turnos_hist["fecha_op"] <= desde].groupby("fecha_op").agg(
        cajas=("cajas_manual", "sum"), hh=("horas_hombre", "sum"), mef=("min_manual", "sum")).tail(
        cfg.DIAS_GRAFICO_INFORME).reset_index()
    h["total"] = h["cajas"] / h["hh"]
    h["efectiva"] = h["cajas"] / (h["mef"] / 60)
    h["x"] = h["fecha_op"].dt.strftime("%d/%m")
    return pdf_cacheado(desde, turnos.iloc[0], h, operarios, zonas, kpi, venta_dia)


@st.cache_data(show_spinner="Armando informe PDF...", max_entries=20)
def pdf_cacheado(fecha, turno, hist, ops, zon, kpi, venta):
    # en caché para no redibujar el PDF cada vez que se toca un filtro
    import informe_pdf
    return informe_pdf.generar(fecha, turno, hist, ops, zon, kpi, venta)


with st.sidebar:
    st.header("Informe diario")
    if desde != hasta:
        st.caption("Elige Período = Día para descargar el informe PDF de ese turno.")
    elif len(turnos) != 1:
        st.caption("No hay un turno único para la fecha elegida.")
    else:
        if error_cuad:
            st.error(f"Cuadratura de venta: {error_cuad}")
        elif not contenidos_cuad:
            st.caption("Sin archivo CUADRATURA_DE_VENTA: el informe saldrá sin venta ni cortes.")
        elif venta_dia is None:
            st.warning(f"La cuadratura cargada no trae venta del {desde:%d/%m/%Y}.")
        try:
            st.download_button("Descargar informe PDF", armar_informe(), type="primary",
                               file_name=f"Informe_picking_PMONTT_{desde:%Y-%m-%d}.pdf",
                               mime="application/pdf", use_container_width=True)
        except Exception as e:  # el informe nunca debe botar la app
            st.error(f"No pude generar el PDF: {e}")

tab_res, tab_rep = st.tabs(["Resumen", f"Reportes ({len(alertas)} alertas)"])

# ================================================================ RESUMEN
with tab_res:
    st.subheader(f"Resumen {periodo}")
    if venta_dia:
        st.markdown("##### Venta y cortes")
        c = st.columns(5)
        c[0].metric("Venta total", fmt_num(venta_dia["venta"]),
                    help=" · ".join(f"{k}: {fmt_num(v)}" for k, v in venta_dia["por_tipo"].items()))
        c[1].metric("Pickeado (manual + pallet)", fmt_num(venta_dia["pickeado"]))
        if venta_dia["hay_cortados"]:
            c[2].metric("Pedidos cortados", fmt_num(venta_dia["cortado"]),
                        f"{venta_dia['pedidos_cortados']} pedidos", delta_color="off")
            c[3].metric("Nivel de servicio",
                        f"{1 - venta_dia['cortado'] / venta_dia['venta']:.2%}" if venta_dia["venta"] else "-")
            c[4].metric("Diferencia", fmt_num(venta_dia["diferencia"]),
                        help="Venta - pickeado - cortado.")
        else:
            c[2].metric("Pedidos cortados", "-", help="Falta el archivo PEDIDOS_CORTADOS.")
    hh = turnos["horas_hombre"].sum()
    h_ef = turnos["min_manual"].sum() / 60
    cajas = turnos["cajas_manual"].sum()
    prod_total = cajas / hh if hh else 0
    st.markdown("##### Picking manual")
    c = st.columns(5)
    c[0].metric("Cajas pickeadas", fmt_num(cajas), help="Suma de cajas en LPN de surtido (nivel S).")
    c[1].metric("Operarios", int(L_man["usuario"].nunique()))
    c[2].metric("cj/HH total", fmt_num(prod_total),
                delta=f"{fmt_num(prod_total - cfg.META_ICEO)} vs meta {cfg.META_ICEO}")
    c[3].metric("cj/HH efectiva", fmt_num(cajas / h_ef if h_ef else 0))
    c[4].metric("Tiempo en listas", f"{h_ef / hh:.0%}" if hh else "-",
                help="Horas dentro de listas manuales sobre horas-hombre (ventana de picking manual × operarios).")
    st.markdown("##### Pallet completo")
    cajas_pal = turnos["cajas_pallet"].sum()
    pallets = turnos["lpns_pallet"].sum()
    c = st.columns(5)
    c[0].metric("Cajas en pallet completo", fmt_num(cajas_pal))
    c[1].metric("Pallets", fmt_num(pallets), help="LPN de nivel L.")
    c[2].metric("% de cajas en pallet completo",
                f"{cajas_pal / (cajas + cajas_pal):.1%}" if cajas + cajas_pal else "-")
    c[3].metric("Operarios", int(L_pal["usuario"].nunique()))
    if alertas:
        st.info(f"Hay {len(alertas)} alertas para este período. Revísalas en la pestaña Reportes.")

    # ---------------- rendimiento por semana / día
    st.divider()
    st.subheader("Rendimiento de picking")
    agrupar = st.radio("Agrupar por", ["Semana", "Día"], horizontal=True, key="agrupar")
    h = turnos_hist.copy()
    if agrupar == "Semana":
        h["clave"] = h["fecha_op"] - pd.to_timedelta(h["fecha_op"].dt.weekday, unit="D")
        etiqueta = lambda k: f"S{k.isocalendar()[1]}<br>{k:%d/%m}"
    else:
        h["clave"] = h["fecha_op"]
        etiqueta = lambda k: f"{k:%d/%m}"
    g = h.groupby("clave").agg(cajas=("cajas_manual", "sum"), hh=("horas_hombre", "sum"),
                               mef=("min_manual", "sum")).reset_index()
    g["total"] = g["cajas"] / g["hh"]
    g["efectiva"] = g["cajas"] / (g["mef"] / 60)
    g["x"] = g["clave"].map(etiqueta)
    import plotly.graph_objects as go
    fig = go.Figure()
    fig.add_bar(x=g["x"], y=g["total"], name="cj/HH total", text=g["total"].round(0), textposition="outside",
                marker_color=[VERDE if v >= cfg.META_ICEO else "#E0A526" for v in g["total"]])
    fig.add_scatter(x=g["x"], y=g["efectiva"], name="cj/HH efectiva", mode="lines+markers+text",
                    text=g["efectiva"].round(0), textposition="top center", line_color="#4A5BC4")
    fig.add_hline(y=cfg.META_ICEO, line_dash="dash", line_color="#C4452F",
                  annotation_text=f"Meta ICEO {cfg.META_ICEO}", annotation_position="top left")
    fig.update_layout(height=420, legend=dict(orientation="h", y=1.12), margin=dict(t=40, b=10),
                      yaxis_title="cajas por hora", yaxis_range=[0, max(g["efectiva"].max(), g["total"].max()) * 1.2])
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Todo el historial cargado, sin el filtro de fecha. Barras verdes: sobre la meta; "
               "amarillas: bajo la meta. Línea: productividad dentro de las listas.")

    st.markdown("#### Detalle por turno")
    tabla(turnos[["fecha_op", "turno", "inicio", "fin", "operarios_manual", "listas_manual", "cajas_manual",
                  "cj_hh_total", "cj_hh_efectiva", "cajas_pallet", "pct_pallet_pct", "min_espera", "min_pausa"]],
          column_config={
              "fecha_op": st.column_config.DateColumn("Fecha", format="DD/MM/YYYY"),
              "turno": "Turno",
              "inicio": st.column_config.DatetimeColumn("Inicio picking", format="HH:mm"),
              "fin": st.column_config.DatetimeColumn("Fin picking", format="HH:mm"),
              "operarios_manual": "Operarios", "listas_manual": "Listas",
              "cajas_manual": st.column_config.NumberColumn("Cajas manual", format="%.0f"),
              "cajas_pallet": st.column_config.NumberColumn("Cajas pallet completo", format="%.0f"),
              "pct_pallet_pct": st.column_config.NumberColumn("% pallet completo", format="%.1f%%"),
              "cj_hh_total": st.column_config.NumberColumn("cj/HH total", format="%.0f"),
              "cj_hh_efectiva": st.column_config.NumberColumn("cj/HH efectiva", format="%.0f"),
              "min_espera": st.column_config.NumberColumn("Min. espera", format="%.0f"),
              "min_pausa": st.column_config.NumberColumn("Min. pausa", format="%.0f"),
          })

    # ---------------- operarios
    st.divider()
    st.subheader("Operarios")
    st.caption("cj/HH total = cajas de surtido ÷ horas del turno. Utilización = minutos dentro de listas / "
               "minutos entre su primera y última lista" + (", descontando colación" if cfg.COLACION_POR_TURNO else "")
               + ". Ordenado por cj/HH total.")
    tabla(operarios[["nombre", "turnos", "listas_manual", "cajas_manual", "cj_hh_total", "vs_meta", "horas_turno",
                     "listas_pallet", "cajas_pallet", "horas_efectivas", "horas_disponibles", "utilizacion_pct",
                     "cj_h_manual", "min_espera", "min_pausa", "min_inicio_tardio"]],
          column_config={
              "nombre": "Operario", "turnos": "Turnos",
              "listas_manual": "Listas manual", "cajas_manual": st.column_config.NumberColumn("Cajas manual", format="%.0f"),
              "cj_hh_total": st.column_config.NumberColumn(
                  "cj/HH total", format="%.0f",
                  help="Cajas de surtido ÷ horas del turno (de la primera a la última lista manual del turno)."),
              "vs_meta": st.column_config.NumberColumn(f"vs meta {cfg.META_ICEO}", format="%+.0f"),
              "horas_turno": st.column_config.NumberColumn("Horas turno", format="%.2f"),
              "listas_pallet": "Listas pallet", "cajas_pallet": st.column_config.NumberColumn("Cajas pallet", format="%.0f"),
              "horas_efectivas": st.column_config.NumberColumn("Horas en listas", format="%.2f"),
              "horas_disponibles": st.column_config.NumberColumn("Horas disponibles", format="%.2f"),
              "utilizacion_pct": st.column_config.ProgressColumn("Utilización", format="%.0f%%",
                                                                 min_value=0, max_value=100),
              "cj_h_manual": st.column_config.NumberColumn("cj/h en listas", format="%.0f"),
              "min_espera": st.column_config.NumberColumn("Min. espera", format="%.0f"),
              "min_pausa": st.column_config.NumberColumn("Min. pausa", format="%.0f"),
              "min_inicio_tardio": st.column_config.NumberColumn(
                  "Inicio tardío (min)", format="%.0f",
                  help="Minutos entre la primera lista del turno y la primera lista del operario."),
          })

    # ---------------- zonas
    st.divider()
    st.subheader("Picking manual por zona de trabajo")
    st.caption("Productividad en tiempo efectivo: si una zona queda bajo la meta aquí, el problema está dentro de "
               "la lista (recorrido, ubicación, tipo de producto), no en las esperas.")
    fig = px.bar(zonas, x="zona", y="cj_h_efectiva", color_discrete_sequence=[VERDE], text_auto=".0f",
                 labels={"zona": "", "cj_h_efectiva": "cj/h en tiempo efectivo"})
    fig.add_hline(y=cfg.META_ICEO, line_dash="dash", line_color="#C4452F", annotation_text=f"Meta ICEO {cfg.META_ICEO}")
    st.plotly_chart(fig, use_container_width=True)
    tabla(zonas[["zona", "listas", "cajas", "lineas", "cj_h_efectiva", "min_por_lista", "cajas_por_lista",
                 "cajas_por_linea", "min_espera_antes"]],
          column_config={
              "zona": "Zona", "listas": "Listas", "cajas": st.column_config.NumberColumn("Cajas", format="%.0f"),
              "lineas": st.column_config.NumberColumn("Líneas", format="%.0f"),
              "cj_h_efectiva": st.column_config.NumberColumn("cj/h efectiva", format="%.0f"),
              "min_por_lista": st.column_config.NumberColumn("Min. por lista", format="%.1f"),
              "cajas_por_lista": st.column_config.NumberColumn("Cajas por lista", format="%.1f"),
              "cajas_por_linea": st.column_config.NumberColumn("Cajas por línea", format="%.1f"),
              "min_espera_antes": st.column_config.NumberColumn("Min. espera previa", format="%.0f"),
          })

    st.markdown("#### Pallets armados en surtido")
    if lpns is None:
        st.info("El archivo de picking no trae nivel_lpn: no se pueden identificar los LPN de surtido.")
    elif casi.empty:
        st.success("No hay LPN de surtido casi pallet en el período elegido.")
    else:
        st.caption(f"LPN de surtido (S) con una sola línea y {cfg.UMBRAL_LPN_CASI_PALLET} cajas o más: casi un pallet "
                   "de un mismo artículo pickeado desde la posición y consolidado después en parrilla. Si las "
                   "'cajas típicas' son menos que las cajas por pallet del artículo, revisa la volumetría.")
        c = st.columns(3)
        c[0].metric("LPN casi pallet", fmt_num(casi["lpns_casi_pallet"].sum()))
        c[1].metric("Cajas en esos LPN", fmt_num(casi["cajas_casi_pallet"].sum()))
        c[2].metric("% de las cajas de surtido",
                    f"{casi['cajas_casi_pallet'].sum() / Lp.loc[Lp['nivel'] == 'S', 'cajas'].sum():.1%}")
        casi["pct_pct"] = casi["pct_cajas_casi_pallet"] * 100
        tabla(casi[["zona", "lpns_casi_pallet", "cajas_casi_pallet", "cajas_tipicas", "lpns_surtido", "pct_pct"]],
              column_config={
                  "zona": "Zona", "lpns_casi_pallet": "LPN casi pallet",
                  "cajas_casi_pallet": st.column_config.NumberColumn("Cajas", format="%.0f"),
                  "cajas_tipicas": "Cajas típicas por LPN", "lpns_surtido": "LPN de surtido totales",
                  "pct_pct": st.column_config.NumberColumn("% cajas de surtido de la zona", format="%.1f%%"),
              })

    st.markdown("#### Pallet completo por zona")
    if zonas_pallet.empty:
        st.info("No hay listas de pallet completo en el período elegido.")
    else:
        tabla(zonas_pallet[["zona", "listas", "lpns", "cajas"]],
              column_config={"zona": "Zona", "listas": "Listas", "lpns": "Pallets",
                             "cajas": st.column_config.NumberColumn("Cajas", format="%.0f")})

    # ---------------- tiempo no efectivo
    st.divider()
    st.subheader("Tiempo no efectivo")
    st.caption(f"Normal: menos de {cfg.UMBRAL_ESPERA_MIN} min entre listas. Espera: {cfg.UMBRAL_ESPERA_MIN} a "
               f"{cfg.UMBRAL_PAUSA_MIN} min. Pausa: más de {cfg.UMBRAL_PAUSA_MIN} min. "
               + " ".join(f"Colación {t}: hasta {c['minutos']} min sin listas entre {c['ventana'][0]} y "
                          f"{c['ventana'][1]}; si el operario se demora más, el exceso cuenta como espera o pausa."
                          for t, c in cfg.COLACION_POR_TURNO.items())
               + ("" if cfg.COLACION_POR_TURNO else " Sin colación dentro del turno: el personal cena al llegar, "
                  "antes de su primera lista."))
    if B.empty:
        st.info("No hay tiempos entre listas para el período elegido.")
    else:
        resumen_tipo = B.groupby("tipo")["brecha_min"].agg(["sum", "count"]).reset_index()
        c = st.columns(len(resumen_tipo))
        for col, (_, r) in zip(c, resumen_tipo.iterrows()):
            col.metric(r["tipo"], f"{fmt_num(r['sum'] / 60, 1)} h", f"{int(r['count'])} veces", delta_color="off")
        por_hora = B.groupby(["hora", "tipo"])["brecha_min"].sum().reset_index()
        fig = px.bar(por_hora, x="hora", y="brecha_min", color="tipo", color_discrete_map=COLORES_TIPO,
                     category_orders={"tipo": ["Normal", "Espera", "Pausa", "Colación"]},
                     labels={"hora": "Hora del día", "brecha_min": "Minutos-hombre", "tipo": ""})
        fig.update_xaxes(dtick=1)
        st.plotly_chart(fig, use_container_width=True)
        st.markdown("#### Esperas y pausas más largas")
        top = B[B["tipo"].isin(["Espera", "Pausa"])].nlargest(15, "brecha_min")
        tabla(top[["fecha_op", "nombre", "fin_anterior", "inicio", "brecha_min", "tipo", "zona"]],
              column_config={
                  "fecha_op": st.column_config.DateColumn("Fecha", format="DD/MM/YYYY"),
                  "nombre": "Operario",
                  "fin_anterior": st.column_config.DatetimeColumn("Terminó lista", format="HH:mm"),
                  "inicio": st.column_config.DatetimeColumn("Inició siguiente", format="HH:mm"),
                  "brecha_min": st.column_config.NumberColumn("Minutos", format="%.0f"),
                  "tipo": "Tipo", "zona": "Zona siguiente lista",
              })

    # ---------------- grúa
    st.divider()
    st.subheader("Movimientos de grúa")
    st.caption("El exporte de grúa no trae la hora: esta sección muestra el día completo (todos los turnos).")
    if G is None:
        st.info("Sube el archivo de movimientos de grúa para ver esta sección.")
    elif G.empty:
        st.info("No hay movimientos de grúa en el período elegido.")
    else:
        total = G["movimientos"].sum()
        nd = G.loc[G["tipo"] == "No dirigido", "movimientos"].sum()
        rd = G.loc[G["tipo"] == "Reabastecimiento dirigido", "movimientos"].sum()
        c = st.columns(3)
        c[0].metric("Movimientos", fmt_num(total))
        c[1].metric("No dirigidos", f"{nd / total:.0%}")
        c[2].metric("Reabastecimiento dirigido", f"{rd / total:.0%}")
        diario = G.groupby(["fecha", "tipo"])["movimientos"].sum().reset_index()
        fig = px.bar(diario, x="fecha", y="movimientos", color="tipo", color_discrete_map=COLORES_GRUA,
                     labels={"fecha": "", "movimientos": "Movimientos", "tipo": ""})
        st.plotly_chart(fig, use_container_width=True)
        fig = px.line(grua_sem, x="semana", y="pct_no_dirigido", markers=True, color_discrete_sequence=["#C4452F"],
                      labels={"semana": "", "pct_no_dirigido": "% no dirigido"})
        fig.update_layout(yaxis_tickformat=".0%", height=300)
        st.plotly_chart(fig, use_container_width=True)
        gu = proc.resumen_grua_usuarios(G)
        gu["pct_no_dirigido"] = gu["pct_no_dirigido"] * 100
        tabla(gu, column_config={"pct_no_dirigido": st.column_config.ProgressColumn(
            "% no dirigido", format="%.0f%%", min_value=0, max_value=100)})

# ================================================================ REPORTES
with tab_rep:
    st.subheader(f"Alertas {periodo}")
    if not alertas:
        st.success("Sin alertas para el período elegido.")
    for seccion in ["Rendimiento", "Pallet completo", "Posiciones", "Grúa", "Calidad de datos"]:
        grupo = [(n, t) for n, s, t in alertas if s == seccion]
        if grupo:
            st.markdown(f"**{seccion}**")
            for nivel, texto in grupo:
                getattr(st, nivel)(texto)

    sub_pos, sub_cal = st.tabs(["Posiciones de picking", "Calidad de datos"])

    with sub_pos:
        if error_pos:
            st.error(error_pos)
        elif det is None:
            st.info("Para esta sección deja en VENTAS MENSUALES PMONTT el archivo de venta de SAP, con 'VENTA' en el "
                    "nombre. El ADC y las cajas por pallet ya vienen incluidos en la app.")
        else:
            st.caption(f"Venta del {v_desde:%d/%m/%Y} al {v_hasta:%d/%m/%Y} ({n_dias} días operativos). No depende "
                       f"del filtro de fecha. Día alto = percentil {cfg.PERCENTIL_DIA_ALTO:.0%} de la venta diaria de "
                       "cada SKU; capacidad en cajas. Se descuentan las cajas que salen como pallet completo "
                       "(cajas por pallet del ADC, pestaña 23-Huellas).")
            res_d = posmod.resumen_diagnostico(det)
            tabla(res_d, column_config={
                "diagnostico": "Diagnóstico", "skus": "SKUs", "skus_a": "Clase A",
                "lineas_dia": st.column_config.NumberColumn("Líneas/día", format="%.0f"),
                "accion": st.column_config.TextColumn("Acción sugerida", width="large")})
            ver = st.selectbox("Ver detalle de", posmod.ORDEN)
            sub = det[det["diagnostico"] == ver].copy()
            sub["pct_dias"] = sub["pct_dias_con_venta"] * 100
            sub["pct_pallet_pct"] = sub["pct_pallet"] * 100
            tabla(sub[["sku", "descripcion", "abc", "zona_trabajo", "zona_movimiento", "ubicaciones", "capacidad",
                       "posiciones", "posiciones_necesarias_p90", "lineas_dia_prom", "cajas_dia_prom",
                       "cajas_dia_p90", "reposiciones_dia_p90", "pct_pallet_pct", "cajas_por_linea", "pct_dias"]],
                  column_config={
                      "sku": "SKU", "descripcion": "Descripción", "abc": "ABC", "zona_trabajo": "Zona",
                      "zona_movimiento": "Zona mov.", "ubicaciones": "Ubicación",
                      "capacidad": st.column_config.NumberColumn("Capacidad", format="%.0f"),
                      "posiciones": st.column_config.NumberColumn("Posiciones", format="%.0f"),
                      "posiciones_necesarias_p90": st.column_config.NumberColumn(
                          "Posiciones para día alto", format="%.0f",
                          help="Posiciones del tamaño actual que necesitaría para no reponer en un día alto."),
                      "lineas_dia_prom": st.column_config.NumberColumn("Líneas/día", format="%.1f"),
                      "cajas_dia_prom": st.column_config.NumberColumn("Cajas/día", format="%.0f"),
                      "cajas_dia_p90": st.column_config.NumberColumn("Cajas día alto", format="%.0f"),
                      "reposiciones_dia_p90": st.column_config.NumberColumn(
                          "Reposiciones día alto", format="%.1f",
                          help="Cajas en un día alto ÷ capacidad. Sobre 1: se vacía dentro del turno."),
                      "pct_pallet_pct": st.column_config.NumberColumn("% en pallet completo", format="%.0f%%"),
                      "cajas_por_linea": st.column_config.NumberColumn("Cajas/línea", format="%.1f"),
                      "pct_dias": st.column_config.NumberColumn("% días con venta", format="%.0f%%"),
                  })
            st.markdown("#### Carga de reposición por zona")
            fig = px.bar(zonas_pos, x="zona_trabajo", y="reposiciones_dia", text_auto=".0f",
                         color_discrete_sequence=[VERDE],
                         labels={"zona_trabajo": "", "reposiciones_dia": "Reposiciones por día (promedio)"})
            st.plotly_chart(fig, use_container_width=True)

            st.markdown("#### Pallet completo: esperado vs real")
            if esperado is None:
                st.info("Agrega el archivo de cajas por pallet para comparar el pallet completo esperado con el real.")
            elif comp.empty:
                st.info("No hay días de picking que calcen con la venta cargada (la venta de un día se pickea el día "
                        "siguiente).")
            else:
                st.caption("Esperado: cajas que según la venta deberían salir como pallet completo. Real: cajas en LPN "
                           "nivel L. Si el real es menor, esos pallets se armaron desde las posiciones de picking.")
                larga = comp.melt(id_vars="fecha_op", value_vars=["cajas_esperadas", "cajas_reales"],
                                  var_name="serie", value_name="cajas")
                larga["serie"] = larga["serie"].map({"cajas_esperadas": "Esperadas", "cajas_reales": "Reales (L)"})
                fig = px.bar(larga, x="fecha_op", y="cajas", color="serie", barmode="group",
                             color_discrete_sequence=["#B8BDC4", VERDE],
                             labels={"fecha_op": "Fecha picking", "cajas": "Cajas en pallet completo", "serie": ""})
                st.plotly_chart(fig, use_container_width=True)
                comp["cumplimiento_pct"] = comp["cumplimiento"] * 100
                tabla(comp[["fecha_op", "pallets_esperados", "pallets_reales", "cajas_esperadas", "cajas_reales",
                            "cajas_faltantes", "cumplimiento_pct"]],
                      column_config={
                          "fecha_op": st.column_config.DateColumn("Fecha picking", format="DD/MM/YYYY"),
                          "pallets_esperados": st.column_config.NumberColumn("Pallets esperados", format="%.0f"),
                          "pallets_reales": "Pallets reales",
                          "cajas_esperadas": st.column_config.NumberColumn("Cajas esperadas", format="%.0f"),
                          "cajas_reales": st.column_config.NumberColumn("Cajas reales", format="%.0f"),
                          "cajas_faltantes": st.column_config.NumberColumn("Cajas armadas en surtido", format="%.0f"),
                          "cumplimiento_pct": st.column_config.NumberColumn("Cumplimiento", format="%.0f%%"),
                      })
            st.download_button("Descargar diagnóstico de posiciones en Excel", posmod.a_excel(det, zonas_pos),
                               file_name=f"posiciones_picking_{v_hasta:%Y%m%d}.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    with sub_cal:
        st.markdown("#### Cuadratura de cajas")
        st.caption("De las cajas del archivo a las que muestra la app en el período y turno elegidos.")
        tabla(proc.conciliar(totales, exclusiones, listas, en_alcance(listas)), column_config={
            "concepto": st.column_config.TextColumn("Concepto", width="large"),
            "surtido_S": st.column_config.NumberColumn("Surtido (S)", format="%.0f"),
            "pallet_L": st.column_config.NumberColumn("Pallet completo (L)", format="%.0f"),
        })
        st.markdown("#### Calidad de los datos cargados")
        st.metric("Listas válidas", fmt_num(len(listas)))
        if exclusiones:
            tabla(pd.DataFrame(exclusiones), column_config={
                "motivo": "Motivo", "registros": "Registros",
                "cajas_s": st.column_config.NumberColumn("Cajas S", format="%.0f"),
                "cajas_l": st.column_config.NumberColumn("Cajas L", format="%.0f")})
        else:
            st.success("No se excluyó ningún registro.")
        for aviso in avisos:
            st.warning(aviso)
        n_sol = int(brechas["solapada"].sum())
        if n_sol:
            st.warning(f"{n_sol} veces un operario inició una lista antes de terminar la anterior. "
                       "Se contaron como 0 minutos entre listas.")
