"""Parámetros de la torre de control. Ajusta estos valores a la operación real del CD."""

# Nombre del CD que se muestra en la app
CD_NOMBRE = "CD Puerto Montt"

# Meta de productividad sobre tiempo total (cajas por hora-hombre)
META_ICEO = 240

# Turnos: (nombre, hora inicio, hora fin). En Puerto Montt el TC parte a las 22:00 del día
# anterior, porque se empieza a pickear cerca de las 23:00. El TB termina a las 22:00.
# El inicio real del turno no es esta hora: es la primera lista de picking manual (mixto)
# del TC, y desde ahí se miden las horas-hombre y el inicio tardío de cada operario.
TURNOS = [
    ("TA", "08:00", "16:00"),
    ("TB", "16:00", "22:00"),
    ("TC", "22:00", "08:00"),
]

# Un turno que cruza medianoche queda con la fecha del día en que TERMINA (True) o en que
# empezó (False). Con True, lo pickeado a las 23:00 del 07/10 cuenta en el TC del 08/10.
FECHA_OPERATIVA_AL_TERMINO = True

# Si el picking del turno se alarga (ej. el TC termina a las 09:30), las listas que un operario
# empieza después del fin de turno siguen contando en su turno mientras no corte más de estos
# minutos entre una lista y la siguiente, y hasta EXTENSION_MAX_MIN pasado el fin de turno.
CONTINUIDAD_TURNO_MIN = 30
EXTENSION_MAX_MIN = 240

# Nombres de archivo que la app busca en Drive (en mayúsculas, basta con que el nombre lo contenga)
PATRON_PICKING = "CAJA_PICKEADA"
PATRON_GRUA = "MOVIMIENTO_DE_GRUA"

# LPN de surtido (nivel S) de un solo artículo con al menos estas cajas se consideran
# "pallets armados en surtido" (debieron salir como pallet completo desde almacenamiento)
UMBRAL_LPN_CASI_PALLET = 50

# Días de la semana sin picking (0 = lunes ... 6 = domingo). La venta del sábado se pickea el lunes.
DIAS_SIN_PICKING = [6]

# Carpetas de Drive (dentro de la carpeta raíz) de las que lee esta app. La raíz
# "TORRE DE CONTROL WMS" la comparten Coquimbo y Puerto Montt: sin este filtro cada app
# mezclaría los archivos del otro CD. Los archivos sueltos en la raíz no se leen.
CARPETAS_DRIVE = [
    "02_ENTRADA",
    "DATOS WMS PMONTT",
    "VENTAS MENSUALES PMONTT",
]

# Posiciones de picking
PATRON_VENTA = "VENTA"                    # archivos de venta (CSV recomendado; Excel es lento)
PATRON_MAESTRO = "ADC"                    # ADC de Puerto Montt (pestañas 6, 16 y 25)
PATRON_FACTOR_PALLET = "CAJAS X PALLET"   # columnas ID_SKU_INV y CAJAS_POR_PALLET       # maestro de ubicaciones (Excel con hojas UBICACIONES, PREFERENCIA, ZM)
VENTANA_VENTA_SEMANAS = 8                # el diagnóstico usa solo las últimas N semanas de venta (None = toda)
MIN_LINEAS_DIA_OPERATIVO = 1000           # días con menos líneas de venta no se consideran
PERCENTIL_DIA_ALTO = 0.9                  # "día alto" = percentil 90 de la venta diaria del SKU

# Turnos que analiza la app (la app muestra solo estos)
TURNOS_ANALIZADOS = ["TC"]

# El pallet completo se identifica por la columna nivel_lpn del exporte (L = pallet, S = surtido).
# Solo si el archivo no trae esa columna se usa el nombre de la zona:
# cualquier zona que empiece con estos textos cuenta como pallet completo.
PREFIJOS_PALLET_COMPLETO = ["ZT ALMACENAMIENTO"]

# Zonas que no son picking y se excluyen de los indicadores.
# Escribe el nombre exacto como aparece en el archivo, ej. "ZT ALMACENAMIENTO".
ZONAS_EXCLUIDAS = []

# Clasificación del tiempo entre listas (minutos)
UMBRAL_ESPERA_MIN = 5    # bajo esto: normal (traslado, tomar la siguiente lista)
UMBRAL_PAUSA_MIN = 15    # entre espera y pausa: espera; sobre esto: pausa

# Colación con horario fijo por turno: ventana en que los operarios salen a colación
# (en TC hay dos grupos: 01:30-02:00 y 02:00-02:30) y minutos que le corresponden a cada uno.
# El tiempo sin listas dentro de la ventana, hasta esos minutos, se cuenta como colación.
# En Puerto Montt no hay colación dentro del turno: el personal cena al llegar al CD, antes
# de su primera lista. Todo hueco entre listas cuenta como normal, espera o pausa.
COLACION_POR_TURNO = {}

# Solo huecos de al menos estos minutos se consideran salida a colación
COLACION_BRECHA_MINIMA_MIN = 10

# Si es True, en turnos sin colación configurada la pausa más larga de cada operario dentro
# de este rango se toma como colación. En Puerto Montt va en False (cenan antes de empezar).
COLACION_AUTOMATICA = False
COLACION_MIN_MIN = 25
COLACION_MAX_MIN = 60

# Listas con duración mayor a esto se excluyen (probable lista abandonada o error)
DURACION_MAX_LISTA_MIN = 120

# El WMS registra hora:minuto, así que una lista de 0 minutos duró algo menos
# de un minuto. Se le asigna esta duración para no inflar la productividad.
DURACION_MIN_LISTA_MIN = 0.5

# Alertas
UMBRAL_UTILIZACION = 0.50      # operario con menos % de su tiempo en listas
UMBRAL_NO_DIRIGIDO = 0.50          # % de movimientos de grúa no dirigidos
UMBRAL_CUMPLIMIENTO_PALLET = 0.85  # alerta si sale como pallet completo menos de este % de lo esperado
