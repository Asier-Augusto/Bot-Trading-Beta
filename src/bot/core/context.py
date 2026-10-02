"""Contexto de solo lectura que recibe la estrategia.

Deliberadamente NO contiene bróker, bus ni reloj del sistema. Si el atributo no existe,
no hay tentación de usarlo, y la pureza de la estrategia deja de depender de la
disciplina de quien la escribe.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Sequence

from bot.core.types import Bar


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Foto del estado de la cuenta en un instante.

    cash         : float — efectivo disponible.
    equity       : float — patrimonio total, incluidas posiciones abiertas.
    buying_power : float — poder de compra, ya contando el apalancamiento.
    """

    cash: float
    equity: float
    buying_power: float


@dataclass(frozen=True, slots=True)
class Position:
    """Posición abierta.

    symbol      : str   — ticker.
    shares      : int   — acciones; positivo en largo.
    entry_price : float — precio medio de entrada.
    stop_price  : float — precio del stop protector, que vive en el bróker.
    tag         : str   — etiqueta del tramo, para la variante mixta.
    signal_id   : str   — identificador de correlación de la señal que la abrió.
    """

    symbol: str
    shares: int
    entry_price: float
    stop_price: float
    tag: str = ""
    signal_id: str = ""


def aggregate_positions(positions: "Sequence[Position]") -> "Position | None":
    """Funde los tramos abiertos de un símbolo en una posición equivalente.

    Los tramos de una misma entrada cuentan como UNA posición a efectos del límite de una
    por símbolo (§7.1 del diseño), así que los filtros de riesgo miran esta vista agregada
    y no la lista. Sin ella, `MaxPositionsFilter` con un máximo de tres dejaría abrir una
    entrada mixta y media.

    shares      = suma de los tramos.
    entry_price = media ponderada por acciones.
    stop_price  = el MÍNIMO de los tramos. El agregado es informativo: cada tramo conserva
                  su propio stop en el bróker —el agresivo amplio y el conservador ajustado,
                  que es todo el sentido de la variante mixta—, y el mínimo es el precio por
                  debajo del cual ya no quedaría ninguno vivo.
    tag         = "" — el agregado no es de ningún tramo.
    signal_id   = el común si todos coinciden, "" si no.

    positions : Sequence[Position] — tramos del MISMO símbolo.
    return    : Position | None — None si la lista está vacía.
    """
    vivos = [p for p in positions if p.shares > 0]
    if not vivos:
        return None

    acciones = sum(p.shares for p in vivos)
    entrada = sum(p.entry_price * p.shares for p in vivos) / acciones
    identificadores = {p.signal_id for p in vivos}
    return Position(
        symbol=vivos[0].symbol,
        shares=acciones,
        entry_price=entrada,
        stop_price=min(p.stop_price for p in vivos),
        tag="",
        signal_id=identificadores.pop() if len(identificadores) == 1 else "",
    )


@dataclass(frozen=True, slots=True)
class SupportZone:
    """Zona de soporte detectada.

    low        : float — suelo de la zona.
    high       : float — techo de la zona.
    score      : float — puntuación por toques, recencia, volumen y marco temporal. Sin
                         normalizar: solo tiene sentido comparada con la de otras zonas
                         del mismo día y la misma configuración.
    touches    : int   — número de toques que la formaron.
    timeframes : tuple[str, ...] — marcos en los que aparece este nivel. Un nivel presente
                         en diario, en 4 h y en 1 h a la vez es la confluencia que el §6.2
                         premia, y guardarlo permite reconocerla después en los registros
                         en vez de deducirla de la puntuación.
    """

    low: float
    high: float
    score: float
    touches: int
    timeframes: tuple[str, ...] = ()

    def contains(self, price: float) -> bool:
        """Indica si un precio cae dentro de la zona.

        price  : float — precio a comprobar.
        return : bool
        """
        return self.low <= price <= self.high


@dataclass(frozen=True, slots=True)
class MarketContext:
    """Todo lo que la estrategia puede ver, y nada más.

    symbol          : str   — ticker que se está evaluando.
    bar             : Bar   — vela recién cerrada.
    now             : datetime — instante del reloj inyectado.
    indicators      : dict  — (nombre, marco, símbolo) -> valor.
    account         : AccountSnapshot — foto de cuenta, para dimensionar con exactitud.
    position        : Position | None — vista AGREGADA de la posición abierta en este
                      símbolo, si la hay. Es la que miran los filtros de riesgo, porque los
                      tramos de una misma entrada cuentan como una sola posición (§7.1).
    positions       : tuple[Position, ...] — los tramos por separado, cada uno con su
                      etiqueta, su precio de entrada y su stop. Es lo que una salida
                      necesita para vender el tramo correcto y no otro.
    support_zones   : tuple[SupportZone, ...] — zonas vigentes, ordenadas por suelo.
    session_label   : str   — etiqueta de la franja horaria, p. ej. "pre-cierre".
    entries_allowed : bool  — si la franja actual permite abrir posiciones.
    params          : dict  — parámetros de la estrategia leídos del YAML.
    """

    symbol: str
    bar: Bar
    now: dt.datetime
    indicators: dict[tuple[str, str, str | None], float]
    account: AccountSnapshot
    position: Position | None
    positions: tuple[Position, ...] = ()
    support_zones: tuple[SupportZone, ...] = ()
    session_label: str = ""
    entries_allowed: bool = True
    params: dict[str, Any] = field(default_factory=dict)

    def indicator(
        self,
        name: str,
        timeframe: str,
        symbol: str | None = None,
    ) -> float | None:
        """Lee un indicador por nombre.

        Este acceso por nombre es lo que permite añadir indicadores nuevos sin tocar
        el contrato `Strategy` ni las estrategias existentes.

        name      : str        — nombre registrado, p. ej. "rsi".
        timeframe : str        — marco temporal, p. ej. "5m".
        symbol    : str | None — ticker; None usa el símbolo del contexto.
        return    : float | None — valor, o None si aún no está listo o no se pidió.
        """
        return self.indicators.get((name, timeframe, symbol or self.symbol))
