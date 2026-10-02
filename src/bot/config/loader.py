"""Carga de perfiles y de parámetros de estrategia.

Un perfil selecciona las implementaciones y fija los parámetros globales. Los
parámetros de cada estrategia van aparte, en `config/strategies/<nombre>.yaml`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bot.core.scoring import LevelThresholds
from bot.core.session import SessionSchedule


def _stringify_keys(value: Any) -> Any:
    """Convierte a texto todas las claves de diccionario, recursivamente.

    `json.dumps(..., sort_keys=True)` ordena comparando las claves entre sí, y en Python
    un `int` y un `str` no se pueden comparar: un YAML tan razonable como

        niveles:
          30: alto
          bajo: 10

    —una clave numérica sin comillas junto a una de texto— reventaba el arranque con un
    `TypeError` que no decía nada útil. Normalizar las claves a texto antes de serializar
    lo resuelve sin afectar a la estabilidad del resumen: la misma configuración sigue
    dando el mismo hash, porque la conversión es determinista.

    value  : Any — cualquier valor salido del YAML.
    return : Any — el mismo valor con todas las claves de diccionario como texto.
    """
    if isinstance(value, dict):
        return {str(k): _stringify_keys(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stringify_keys(v) for v in value]
    return value


def compute_config_hash(raw: dict[str, Any]) -> str:
    """Resume una configuración en un identificador estable.

    Viaja en cada evento para que, meses después, se sepa qué parámetros produjeron
    cada registro. Es independiente del orden de las claves: reordenar el YAML no debe
    invalidar la caché ni fingir un cambio.

    raw    : dict — configuración completa.
    return : str  — los 16 primeros caracteres del SHA-256.
    """
    canonical = json.dumps(
        _stringify_keys(raw), sort_keys=True, ensure_ascii=False, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Profile:
    """Un perfil de ejecución ya resuelto.

    strategy    : str   — nombre registrado de la estrategia.
    broker      : str   — nombre registrado del bróker.
    feed        : str   — nombre registrado del feed.
    sizer       : str   — nombre registrado del dimensionador.
    sinks       : list  — nombres registrados de los observadores activos.
    watchlist   : list  — símbolos a vigilar.
    session     : SessionSchedule — franjas horarias.
    levels      : LevelThresholds — umbrales de nivel de registro.
    min_log_level: str  — nivel mínimo que se escribe a disco.
    raw         : dict  — el YAML completo, para lo que no esté modelado.
    config_hash : str   — resumen estable de `raw`.
    """

    strategy: str
    broker: str
    feed: str
    sizer: str
    sinks: list[str]
    watchlist: list[str]
    session: SessionSchedule
    levels: LevelThresholds
    min_log_level: str
    raw: dict[str, Any] = field(default_factory=dict)
    config_hash: str = ""


def _intenta_desactivar_armado(value: Any) -> bool:
    """Detecta si un valor de `armed_requires_all` expresa la intención de apagarlo.

    No basta comparar por identidad con `False`: en YAML son igual de explícitas las
    formas `0`, `"false"`, `"no"` u `"off"`, y todas merecen el mismo aviso. Cualquier
    otro valor (incluido `True` o cualquier cosa "truthy" no reconocida) se trata como
    que no se está intentando desactivar nada.

    value  : Any  — el valor tal cual vino del YAML bajo `signals.levels.armed_requires_all`.
    return : bool — True si el valor expresa la intención de desactivarlo.
    """
    if isinstance(value, bool):
        return value is False
    if isinstance(value, str):
        return value.strip().lower() in {"false", "no", "off", "0", ""}
    if isinstance(value, (int, float)):
        return value == 0
    return False


def load_profile(path: str | Path) -> Profile:
    """Carga un perfil de ejecución desde YAML.

    path   : str | Path — ruta al fichero del perfil.
    return : Profile
    raises : FileNotFoundError — si el fichero no existe.
             ValueError        — si se intenta desactivar `armed_requires_all`.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no existe el perfil: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    signals = raw.get("signals", {})
    level_cfg = signals.get("levels", {})

    # Desactivar esto convierte "casi armado" en "armado": es un cambio de ESTRATEGIA
    # disfrazado de ajuste de registro. Si alguien lo quiere, que lo haga en el código
    # y con conocimiento de causa, no por una línea de YAML.
    #
    # La comparación no puede ser `is False`: alguien que escriba `armed_requires_all: 0`
    # o `armed_requires_all: "false"` en el YAML tiene la misma intención de
    # desactivarlo que quien escribe `false`, y merece el mismo aviso explícito. El
    # valor efectivo sigue siendo seguro en cualquier caso (más abajo se construye
    # `LevelThresholds` con `armed_requires_all=True` fijo), pero ignorar en silencio
    # una clave que el usuario puso a propósito rompe la garantía de "explícito y
    # ruidoso" que esta comprobación existe para dar.
    if "armed_requires_all" in level_cfg and _intenta_desactivar_armado(
        level_cfg["armed_requires_all"]
    ):
        raise ValueError(
            "armed_requires_all no se puede desactivar desde la configuración: "
            "la puntuación decide qué se registra, nunca si se opera"
        )

    return Profile(
        strategy=str(raw.get("strategy", "moris")),
        broker=str(raw.get("broker", "simulated")),
        feed=str(raw.get("feed", "csv_replay")),
        sizer=str(raw.get("sizer", "percent_equity")),
        sinks=list(raw.get("sinks", ["jsonl"])),
        watchlist=list(raw.get("watchlist", [])),
        session=SessionSchedule.from_dict(raw.get("session", {})),
        levels=LevelThresholds(
            near_miss=float(level_cfg.get("near_miss", 0.80)),
            partial=float(level_cfg.get("partial", 0.55)),
            armed_requires_all=True,
        ),
        min_log_level=str(signals.get("min_log_level", "NEAR_MISS")),
        raw=raw,
        config_hash=compute_config_hash(raw),
    )


def load_strategy_params(name: str, root: str | Path = "config/strategies") -> dict[str, Any]:
    """Carga los parámetros de una estrategia.

    name   : str        — nombre de la estrategia, p. ej. "moris".
    root   : str | Path — carpeta donde viven los ficheros de parámetros.
    return : dict       — parámetros; vacío si el fichero no existe.
    """
    path = Path(root) / f"{name}.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
