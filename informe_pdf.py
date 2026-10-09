"""Informe diario de picking en PDF + lectura de la cuadratura de venta y pedidos cortados.

No depende de Streamlit: recibe DataFrames ya calculados por procesamiento.py y devuelve bytes.
"""
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)

import config as cfg
import procesamiento as proc

VERDE = "#174A39"
VERDE_OK = "#2E7D5B"
AMBAR = "#E0A526"
ROJO = "#C4452F"
AZUL = "#4A5BC4"
GRIS = "#6B7280"


# ================================================================ cuadratura y cortados

def _fecha(serie: pd.Series) -> pd.Series:
    """'09/10/2026 05:00' -> 2026-10-09 (solo la fecha)."""
    return pd.to_datetime(serie.str.strip().str[:10], format="%d/%m/%Y", errors="coerce")


def _col(df, *candidatas):
    for c in candidatas:
        if c in df.columns:
            return c
    raise ValueError(f"Falta la columna '{candidatas[0]}'. Columnas del archivo: {', '.join(df.columns)}")


def _ultimo_por_fecha(archivos, preparar):
    """Cada descarga trae el día del picking y el siguiente (parcial). Para cada fecha se usa el
    archivo más reciente que la contiene. `archivos` va ordenado del más antiguo al más nuevo."""
    partes = []
    for orden, contenido in enumerate(archivos):
        d = preparar(proc.leer_csv(contenido))
        d["_orden"] = orden
        partes.append(d)
    if not partes:
        return None
    d = pd.concat(partes, ignore_index=True).dropna(subset=["fecha"])
    ultimo = d.groupby("fecha")["_orden"].transform("max")
    return d[d["_orden"] == ultimo].drop(columns="_orden")


def leer_cuadratura(archivos: tuple) -> pd.DataFrame | None:
    def prep(df):
        out = pd.DataFrame({
            "fecha": _fecha(df[_col(df, "fecha de op de cliente")]),
            "camion": df[_col(df, "fecha de po de cliente")].str.strip(),
            "tipo": df[_col(df, "tipo de pedido")].str.strip(),
            "cajas": pd.to_numeric(df[_col(df, "totalqty")], errors="coerce").fillna(0),
        })
        alm = next((c for c in df.columns if c.startswith("id de almac")), None)
        if alm:
            out = out[df[alm].str.strip().str.upper().eq(cfg.ALMACEN)]
        return out
    return _ultimo_por_fecha(archivos, prep)


def leer_cortados(archivos: tuple) -> pd.DataFrame | None:
    def prep(df):
        out = pd.DataFrame({
            "fecha": _fecha(df[_col(df, "fecha de oc de comprador")]),
            "camion": df[_col(df, "camión", "camion")].str.strip(),
            "articulo": df[_col(df, "articulo", "artículo")].str.strip(),
            "descripcion": df[_col(df, "descripcion", "descripción")].str.strip(),
            "cantidad": pd.to_numeric(df[_col(df, "cantidad cortada")], errors="coerce").fillna(0),
            "pedido": df[_col(df, "numero de pedido", "número de pedido")].str.strip(),
        })
        if "almacen" in df.columns:
            out = out[df["almacen"].str.strip().str.upper().eq(cfg.ALMACEN)]
        return out
    return _ultimo_por_fecha(archivos, prep)


def resumen_venta(cuad, cort, fecha, cajas_pickeadas):
    """Venta y cortes del día de picking. None si la cuadratura no trae esa fecha."""
    fecha = pd.Timestamp(fecha).normalize()
    if cuad is None:
        return None
    v = cuad[cuad["fecha"] == fecha]
    if v.empty:
        return None
    c = cort[cort["fecha"] == fecha] if cort is not None else None
    venta = float(v["cajas"].sum())
    cortado = float(c["cantidad"].sum()) if c is not None else None
    detalle = None
    if c is not None and not c.empty:
        detalle = (c.groupby(["articulo", "descripcion"])
                   .agg(cantidad=("cantidad", "sum"), pedidos=("pedido", "nunique"), camiones=("camion", "nunique"))
                   .reset_index().sort_values("cantidad", ascending=False))
    return {
        "venta": venta,
        "por_tipo": v.groupby("tipo")["cajas"].sum().sort_values(ascending=False).to_dict(),
        "camiones": v["camion"].str.split("-").str[0].nunique(),
        "vueltas": v["camion"].nunique(),
        "cortado": cortado,
        "pedidos_cortados": c["pedido"].nunique() if c is not None else None,
        "lineas_cortadas": len(c) if c is not None else None,
        "hay_cortados": c is not None,
        "pickeado": cajas_pickeadas,
        "diferencia": venta - cajas_pickeadas - (cortado or 0),
        "detalle_cortes": detalle,
    }


# ================================================================ PDF

def _n(x, dec=0):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return f"{x:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")


plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.spines.top": False,
                     "axes.spines.right": False, "axes.edgecolor": "#D1D5DB", "axes.labelcolor": GRIS,
                     "xtick.color": GRIS, "ytick.color": GRIS})


def _img(fig, ancho_cm=17.5):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=200, bbox_inches="tight")
    w, h = fig.get_size_inches()
    plt.close(fig)
    buf.seek(0)
    return Image(buf, width=ancho_cm * cm, height=ancho_cm * cm * h / w)


def _graf_dias(hist):
    meta = cfg.META_ICEO
    x = list(hist["x"])
    fig, ax = plt.subplots(figsize=(9, 3.6))
    b = ax.bar(x, hist["total"], width=0.6, zorder=2, label="cj/HH total",
               color=[VERDE_OK if v >= meta else AMBAR for v in hist["total"]])
    ax.bar_label(b, labels=[_n(v) for v in hist["total"]], fontsize=9, padding=2)
    ax.plot(x, hist["efectiva"], color=AZUL, marker="o", lw=2, zorder=3, label="cj/HH efectiva")
    for xi, y in zip(x, hist["efectiva"]):
        ax.annotate(_n(y), (xi, y), textcoords="offset points", xytext=(0, 7), ha="center", fontsize=9, color=AZUL)
    ax.axhline(meta, color=ROJO, ls="--", lw=1.4, zorder=1)
    ax.text(len(x) - 0.5, meta - 16, f"Meta ICEO {meta}", color=ROJO, fontsize=8, ha="right")
    ax.set_ylim(0, max(hist["efectiva"].max(), hist["total"].max(), meta) * 1.2)
    ax.set_ylabel("cajas por hora")
    ax.grid(axis="y", color="#EEE", zorder=0)
    ax.legend(loc="upper left", frameon=False, ncol=2, bbox_to_anchor=(0, 1.12))
    return _img(fig)


def _graf_zonas(zonas):
    meta = cfg.META_ICEO
    z = zonas.sort_values("cj_h_efectiva")
    nombres = [s.replace("ZT ", "") for s in z["zona"]]
    vals = z["cj_h_efectiva"].fillna(0).values
    fig, ax = plt.subplots(figsize=(9, 3.8))
    b = ax.bar(nombres, vals, width=0.65, zorder=2, color=[VERDE if v >= meta else AMBAR for v in vals])
    ax.bar_label(b, labels=[_n(v) for v in vals], fontsize=8.5, padding=2)
    ax.axhline(meta, color=ROJO, ls="--", lw=1.4, zorder=3)
    ax.text(-0.45, meta + 12, f"Meta ICEO {meta}", color=ROJO, fontsize=8)
    ax.set_ylabel("cj/h en tiempo efectivo")
    ax.set_ylim(0, max(vals.max(), meta) * 1.12)
    ax.grid(axis="y", color="#EEE", zorder=0)
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=8)
    return _img(fig)


H1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=17, leading=21, textColor=colors.HexColor(VERDE))
SUB = ParagraphStyle("sub", fontName="Helvetica", fontSize=9, textColor=colors.HexColor(GRIS), spaceAfter=8)
H2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=12, textColor=colors.HexColor(VERDE),
                    spaceBefore=10, spaceAfter=5)
NOTA = ParagraphStyle("nota", fontName="Helvetica", fontSize=7.5, leading=10, textColor=colors.HexColor(GRIS),
                      spaceBefore=3)
KL = ParagraphStyle("kl", fontName="Helvetica", fontSize=7.5, leading=9, textColor=colors.HexColor(GRIS))
KV = ParagraphStyle("kv", fontName="Helvetica-Bold", fontSize=15, leading=18)
KD = ParagraphStyle("kd", fontName="Helvetica", fontSize=7, leading=9)


def _tarjetas(items, ancho=17.5):
    fila = []
    for lab, val, det in items:
        c = [Paragraph(lab, KL), Paragraph(val, KV)]
        if det:
            c.append(Paragraph(det, KD))
        fila.append(c)
    t = Table([fila], colWidths=[ancho / len(items) * cm] * len(items))
    t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F6F9F7")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _tabla(encab, filas, anchos, izq=(0,), fuente=7.5, extra=(), total=False):
    he = ParagraphStyle("he", fontName="Helvetica-Bold", fontSize=fuente - 0.5, leading=fuente + 1.5,
                        textColor=colors.white, alignment=2)
    hel = ParagraphStyle("hel", parent=he, alignment=0)
    data = [[Paragraph(h, hel if i in izq else he) for i, h in enumerate(encab)]]
    data += [[str(c) for c in f] for f in filas]
    t = Table(data, colWidths=[a * cm for a in anchos], repeatRows=1)
    est = [
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"), ("FONTSIZE", (0, 0), (-1, -1), fuente),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(VERDE)),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6F4")]),
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#E5E7EB")),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for c in izq:
        est.append(("ALIGN", (c, 1), (c, -1), "LEFT"))
    if total:
        est += [("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#E3ECE7"))]
    t.setStyle(TableStyle(est + list(extra)))
    return t


def _color_meta(col, fila, valor, meta):
    if valor is None or (isinstance(valor, float) and np.isnan(valor)):
        return []
    return [("TEXTCOLOR", (col, fila), (col, fila), colors.HexColor(VERDE_OK if valor >= meta else ROJO))]


def generar(fecha, turno, hist, operarios, zonas, kpi, venta=None) -> bytes:
    """
    fecha: día de picking (Timestamp). turno: fila de resumen_turnos para ese día.
    hist: DataFrame con x, total, efectiva (cj/HH por día). operarios: consolidar_operarios del día.
    zonas: resumen_zonas manual del día. kpi: dict con los indicadores del resumen.
    venta: resultado de resumen_venta o None.
    """
    meta = cfg.META_ICEO
    fecha_txt = f"{pd.Timestamp(fecha):%d/%m/%Y}"
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.6 * cm, rightMargin=1.6 * cm,
                            topMargin=1.4 * cm, bottomMargin=1.4 * cm,
                            title=f"Informe picking {cfg.CD_NOMBRE} {fecha_txt}", author="Torre de control WMS")
    s = [Paragraph(f"Informe de picking — {cfg.CD_NOMBRE}", H1),
         Paragraph(f"Turno {turno['turno']} · {fecha_txt} · picking de {turno['inicio']:%H:%M} a "
                   f"{turno['fin']:%H:%M}", SUB)]

    # ---------- 1. resumen
    s.append(Paragraph("1. Resumen del turno", H2))
    if venta:
        tipos = " · ".join(f"{k.capitalize()} {_n(v)}" for k, v in venta["por_tipo"].items())
        servicio = (1 - venta["cortado"] / venta["venta"]) if venta["hay_cortados"] and venta["venta"] else None
        s.append(_tarjetas([
            ("Venta total (cajas)", _n(venta["venta"]), f"{tipos} · {venta['camiones']} camiones, {venta['vueltas']} vueltas"),
            ("Pickeado (manual + pallet)", _n(venta["pickeado"]),
             f"diferencia vs venta: {_n(venta['diferencia'])}" if venta["hay_cortados"] else None),
            ("Pedidos cortados", _n(venta["cortado"]) if venta["hay_cortados"] else "—",
             (f"{venta['pedidos_cortados']} pedidos · {venta['lineas_cortadas']} líneas"
              if venta["hay_cortados"] else "falta archivo PEDIDOS_CORTADOS")),
            ("Nivel de servicio", f"{servicio:.2%}".replace(".", ",") if servicio is not None else "—",
             "1 - cortado ÷ venta"),
        ]))
        s.append(Spacer(1, 4))
    brecha = kpi["cj_hh_total"] - meta
    s.append(_tarjetas([
        ("Cajas picking manual", _n(kpi["cajas"]), f"{kpi['operarios']} operarios"),
        ("Cajas pallet completo", _n(kpi["cajas_pallet"]),
         f"{_n(kpi['pallets'])} pallets · {kpi['pct_pallet']:.1%} · {kpi['operarios_pallet']} operarios"),
        ("cj/HH total", _n(kpi["cj_hh_total"]),
         f"<font color='{ROJO if brecha < 0 else VERDE_OK}'>{brecha:+.0f} vs meta {meta}</font>"),
        ("cj/HH efectiva", _n(kpi["cj_hh_efectiva"]), f"tiempo en listas {kpi['tiempo_listas']:.0%}"),
    ]))
    notas = ("cj/HH total = cajas de surtido ÷ horas-hombre del turno. cj/HH efectiva = productividad solo dentro "
             "de las listas; la diferencia entre ambas son esperas y pausas fuera de lista.")
    if venta and venta["hay_cortados"]:
        notas += (" Diferencia vs venta = venta - pickeado - cortado (cajas de la venta que no aparecen pickeadas "
                  "ni cortadas).")
    if not venta:
        notas += " Sin cuadratura de venta para esta fecha."
    s.append(Paragraph(notas, NOTA))

    # ---------- 2. rendimiento por día y turno
    s.append(Paragraph("2. Rendimiento de picking por día", H2))
    s.append(_graf_dias(hist))
    s.append(Paragraph("Barras verdes: sobre la meta; amarillas: bajo la meta. Línea: productividad dentro de las "
                       "listas.", NOTA))
    s.append(Paragraph("Detalle por turno", H2))
    t = turno
    s.append(_tabla(
        ["Fecha", "Turno", "Inicio", "Fin", "Oper.", "Listas", "Cajas manual", "cj/HH total", "cj/HH efect.",
         "Cajas pallet c.", "% pallet c.", "Min. espera", "Min. pausa"],
        [[fecha_txt, t["turno"], f"{t['inicio']:%H:%M}", f"{t['fin']:%H:%M}", _n(t["operarios_manual"]),
          _n(t["listas_manual"]), _n(t["cajas_manual"]), _n(t["cj_hh_total"]), _n(t["cj_hh_efectiva"]),
          _n(t["cajas_pallet"]), f"{t['pct_pallet']:.1%}", _n(t["min_espera"]), _n(t["min_pausa"])]],
        [1.85, 1.05, 1.15, 1.15, 1.1, 1.15, 1.4, 1.25, 1.25, 1.45, 1.25, 1.25, 1.2], izq=(0, 1), fuente=6.8,
        extra=_color_meta(7, 1, t["cj_hh_total"], meta)))

    # ---------- 3. operarios
    s.append(PageBreak())
    s.append(Paragraph("3. Productividad por operario (picking manual)", H2))
    man = operarios[operarios["listas_manual"] > 0].sort_values("cj_hh_total", ascending=False)
    filas, extra = [], []
    for i, (_, r) in enumerate(man.iterrows(), start=1):
        faltan = max(0.0, meta * r["horas_turno"] - r["cajas_manual"])
        filas.append([r["nombre"], _n(r["listas_manual"]), _n(r["cajas_manual"]), _n(r["horas_turno"], 1),
                      _n(r["horas_efectivas"], 1), f"{r['utilizacion']:.0%}" if pd.notna(r["utilizacion"]) else "—",
                      _n(r["cj_hh_total"]), f"{r['cj_hh_total'] - meta:+.0f}", _n(r["cj_h_manual"]), _n(faltan)])
        extra += _color_meta(7, i, r["cj_hh_total"], meta)
    hh, cj = man["horas_turno"].sum(), man["cajas_manual"].sum()
    he, md = man["horas_efectivas"].sum(), man["min_manual"].sum()
    if hh:
        filas.append(["TOTAL", _n(man["listas_manual"].sum()), _n(cj), _n(hh, 1), _n(he, 1),
                      f"{man['min_efectivos'].sum() / man['min_disponibles'].sum():.0%}"
                      if man["min_disponibles"].sum() else "—",
                      _n(cj / hh), f"{cj / hh - meta:+.0f}", _n(cj / (md / 60)) if md else "—",
                      _n(max(0.0, meta * hh - cj))])
    s.append(_tabla(["Operario", "Listas", "Cajas", "Horas turno", "Horas en listas", "Utiliz.", "cj/HH total",
                     f"vs meta {meta}", "cj/h en listas", "Cajas faltantes para meta"],
                    filas, [4.6, 1.1, 1.3, 1.3, 1.4, 1.2, 1.3, 1.3, 1.4, 2.0], extra=extra, total=bool(hh)))
    otros = operarios[operarios["listas_manual"] == 0]
    nota = ("Horas turno = ventana de picking manual del turno (horas-hombre de cada operario). Horas en listas = "
            "tiempo dentro de listas. Utilización = horas en listas ÷ tiempo entre su primera y última lista. "
            f"Cajas faltantes = cajas que habría que sumar para llegar a {meta} cj/HH en las mismas horas.")
    if not otros.empty:
        nota += " Solo pallet completo (sin listas manuales): " + "; ".join(
            f"{r['nombre'].title()} ({_n(r['listas_pallet'])} listas, {_n(r['cajas_pallet'])} cajas)"
            for _, r in otros.iterrows()) + "."
    s.append(Paragraph(nota, NOTA))

    # ---------- 4. zonas
    s.append(PageBreak())
    s.append(Paragraph("4. Picking manual por zona de trabajo", H2))
    s.append(Paragraph("Productividad en tiempo efectivo: si una zona queda bajo la meta, el problema está dentro de "
                       "la lista (recorrido, ubicación, tipo de producto), no en las esperas.", NOTA))
    s.append(_graf_zonas(zonas))
    zt = zonas.sort_values("listas", ascending=False)
    filas, extra = [], []
    for i, (_, r) in enumerate(zt.iterrows(), start=1):
        filas.append([r["zona"], _n(r["listas"]), _n(r["cajas"]), _n(r["lineas"]), _n(r["cj_h_efectiva"]),
                      _n(r["min_por_lista"], 1), _n(r["cajas_por_lista"], 1), _n(r["cajas_por_linea"], 1),
                      _n(r["min_espera_antes"])])
        extra += _color_meta(4, i, r["cj_h_efectiva"], meta)
    s.append(KeepTogether(_tabla(
        ["Zona", "Listas", "Cajas", "Líneas", "cj/h efectiva", "Min. por lista", "Cajas por lista",
         "Cajas por línea", "Min. espera previa"],
        filas, [4.6, 1.3, 1.5, 1.5, 1.6, 1.6, 1.7, 1.7, 2.0], extra=extra)))

    # ---------- 5. cortes
    if venta and venta["detalle_cortes"] is not None:
        d = venta["detalle_cortes"].head(15)
        filas = [[r["articulo"], r["descripcion"][:42], _n(r["cantidad"]), _n(r["pedidos"]), _n(r["camiones"])]
                 for _, r in d.iterrows()]
        bloque = [Paragraph("5. Pedidos cortados por artículo", H2),
                  _tabla(["Artículo", "Descripción", "Cantidad cortada", "Pedidos", "Camiones"], filas,
                         [2.4, 9.0, 2.2, 1.9, 2.0], izq=(0, 1))]
        if len(venta["detalle_cortes"]) > 15:
            bloque.append(Paragraph(f"Se muestran los 15 artículos con más cortes de "
                                    f"{len(venta['detalle_cortes'])}.", NOTA))
        s.append(KeepTogether(bloque))

    def pie(c, d):
        c.saveState()
        c.setFont("Helvetica", 7)
        c.setFillColor(colors.HexColor(GRIS))
        c.drawString(1.6 * cm, 0.8 * cm, f"Torre de control WMS · {cfg.CD_NOMBRE} · {fecha_txt}")
        c.drawRightString(A4[0] - 1.6 * cm, 0.8 * cm, f"Página {d.page}")
        c.restoreState()

    doc.build(s, onFirstPage=pie, onLaterPages=pie)
    return buf.getvalue()
