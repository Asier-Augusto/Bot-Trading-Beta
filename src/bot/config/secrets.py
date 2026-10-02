"""Carga de credenciales, separada de la configuración.

Van en un fichero aparte y **fuera del repositorio** (`config/secrets.yaml`, excluido por
`.gitignore`) por una razón que no es de estilo: un token de bot filtrado deja que cualquiera
escriba en el canal por el que este sistema avisa sobre dinero, y un histórico de git no se
puede limpiar de verdad — el commit sigue ahí en cada copia que alguien clonó.

**Las variables de entorno mandan sobre el fichero.** Es lo que permite que un despliegue no
tenga que llevar el fichero: `BOT_TELEGRAM_TOKEN`, `BOT_TELEGRAM_CHAT_IDS`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

#: Dónde se buscan las credenciales si nadie dice otra cosa.
DEFAULT_PATH = Path("config/secrets.yaml")


def load_secrets(path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    """Lee el fichero de credenciales.

    Que no exista NO es un error: un backtest no necesita credenciales, y obligar a tenerlas
    para correr uno haría que alguien acabara metiendo un fichero de ejemplo con valores
    falsos en el repositorio, que es justo lo que hay que evitar.

    path   : Path | str — ruta del fichero.
    return : dict — contenido; vacío si no existe.
    """
    ruta = Path(path)
    if not ruta.exists():
        return {}
    return yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}


def telegram_credentials(secrets: dict[str, Any] | None = None) -> tuple[str, list[str]]:
    """Token y destinatarios de Telegram, con las variables de entorno por delante.

    secrets : dict | None — contenido del fichero de credenciales; None lo carga.
    return  : tuple[str, list[str]] — (token, chat_ids). Token vacío significa "no
              configurado", y el notificador se desactiva solo en vez de fallar al arrancar:
              quedarse sin avisos es malo, no arrancar es peor.
    """
    datos = (secrets if secrets is not None else load_secrets()).get("telegram") or {}

    token = os.environ.get("BOT_TELEGRAM_TOKEN") or str(datos.get("bot_token") or "")

    desde_entorno = os.environ.get("BOT_TELEGRAM_CHAT_IDS")
    if desde_entorno:
        chat_ids = [c.strip() for c in desde_entorno.split(",") if c.strip()]
    else:
        chat_ids = [str(c) for c in (datos.get("chat_ids") or [])]

    return token, chat_ids


def redact(token: str) -> str:
    """Enmascara un token para poder registrarlo sin filtrarlo.

    Se conservan los primeros caracteres —los de un token de Telegram son el identificador
    numérico del bot, que no es secreto— para poder distinguir dos bots en un log sin revelar
    la parte que sí lo es.

    token  : str — el token completo.
    return : str — versión enmascarada, o "(sin configurar)" si está vacío.
    """
    if not token:
        return "(sin configurar)"
    cabeza = token.split(":")[0]
    return f"{cabeza}:***"
