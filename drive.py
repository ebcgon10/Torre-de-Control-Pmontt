"""Lectura de exportes del WMS desde una carpeta de Google Drive (cuenta de servicio).

No depende de Streamlit: el reporte automático (fase 3) usará este mismo módulo.
"""
import io
import json

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive"]
CARPETA = "application/vnd.google-apps.folder"


def conectar(credenciales_json: str):
    """Crea el cliente de Drive a partir del contenido del archivo JSON de la cuenta de servicio."""
    info = json.loads(credenciales_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def listar_archivos(servicio, carpeta_id: str, extensiones=(".csv", ".xlsx")) -> list[dict]:
    """Archivos CSV y Excel de la carpeta y sus subcarpetas: id, name, modifiedTime, carpeta."""
    archivos, pendientes = [], [(carpeta_id, "")]
    while pendientes:
        actual, ruta = pendientes.pop()
        token = None
        while True:
            r = servicio.files().list(
                q=f"'{actual}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
                pageSize=1000, pageToken=token,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            for f in r.get("files", []):
                if f["mimeType"] == CARPETA:
                    pendientes.append((f["id"], f"{ruta}{f['name']}/"))
                elif f["name"].lower().endswith(tuple(extensiones)):
                    f["carpeta"] = ruta
                    archivos.append(f)
            token = r.get("nextPageToken")
            if not token:
                break
    return sorted(archivos, key=lambda f: f["name"])


def descargar(servicio, archivo_id: str) -> bytes:
    buffer = io.BytesIO()
    descarga = MediaIoBaseDownload(buffer, servicio.files().get_media(fileId=archivo_id))
    terminado = False
    while not terminado:
        _, terminado = descarga.next_chunk()
    return buffer.getvalue()
