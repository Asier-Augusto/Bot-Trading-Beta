"""Tipos del dominio que atraviesan todo el sistema.

Todos inmutables: una vez creado un hecho, nadie lo modifica. Eso hace que se puedan
guardar en eventos y leer minutos después con la garantía de que siguen diciendo lo
que decían al crearse.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass


class Side(enum.StrEnum):
    """Sentido de una operación."""

    BUY = "BUY"
    SELL = "SELL"


class Timing(enum.StrEnum):
    """Cuándo se ejecuta una orden respecto a la vela que la produjo.

    Hasta el 2026-09-10 solo existía `NEXT_OPEN`: la estrategia decidía al cierre de una
    vela y el bróker compraba en la apertura de la siguiente. Eso metía **un minuto de
    retraso en todo**, y en una estrategia que entra en el rebote de un soporte el minuto
    es justo donde está el movimiento: medido sobre MRVL del 18-12-2025, la conservadora
    disparaba a las 14:53 y aparecía comprada a las 14:54, 21 céntimos más arriba.

    Las tres formas de aquí se pueden ejecutar de verdad en vivo, y ninguna mira el futuro:

    NEXT_OPEN  — apertura de la vela siguiente. Es lo que se hacía siempre y sigue siendo
                 lo correcto para una orden que se decide *después* de que pase algo (el
                 cierre forzado de la sesión, por ejemplo).
    THIS_CLOSE — cierre de la MISMA vela que disparó. Es lo que hace un operador que ve el
                 rebote formarse y aprieta el botón antes de que acabe el minuto, y lo que
                 hace el bot en vivo, donde los indicadores incluyen la vela en curso.
    THIS_STOP  — dentro de la MISMA vela, en `trigger_price`: modela una orden *stop* ya
                 puesta en el mercado que salta sola cuando el precio la cruza. No es
                 adivinar: el precio pasó por ahí dentro de esa vela, y la orden estaba
                 puesta desde antes. Con hueco de apertura por encima se ejecuta en la
                 apertura, que es peor y es lo que pasaría de verdad.
    """

    NEXT_OPEN = "next_open"
    THIS_CLOSE = "this_close"
    THIS_STOP = "this_stop"


@dataclass(frozen=True, slots=True)
class Bar:
    """Vela ya cerrada.

    symbol    : str      — ticker.
    timeframe : str      — tamaño de la vela, p. ej. "1m" o "5m".
    ts        : datetime — instante de CIERRE de la vela, consciente de zona.
    open      : float    — precio de apertura.
    high      : float    — máximo.
    low       : float    — mínimo.
    close     : float    — precio de cierre.
    volume    : int      — volumen negociado; 0 si no hubo operaciones.
    """

    symbol: str
    timeframe: str
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def range(self) -> float:
        """Recorrido de la vela.

        return : float — máximo menos mínimo. Cero en una vela plana.
        """
        return self.high - self.low

    @property
    def closes_in_upper_third(self) -> bool:
        """Indica si la vela cierra en el tercio alto de su propio rango.

        Es uno de los disparadores de la variante agresiva de Moris.

        return : bool — False en una vela plana, para no dividir por cero.
        """
        if self.range <= 0:
            return False
        return (self.close - self.low) / self.range >= 2 / 3


class SizingDirective:
    """Base de las directivas de tamaño que una estrategia puede expresar."""


@dataclass(frozen=True, slots=True)
class Risk(SizingDirective):
    """Arriesgar una fracción de la cuenta en esta operación.

    fraction : float — fracción del patrimonio a arriesgar, en (0, 1].
    """

    fraction: float

    def __post_init__(self) -> None:
        if not 0 < self.fraction <= 1:
            raise ValueError("la fracción debe estar entre 0 y 1")


@dataclass(frozen=True, slots=True)
class Equity(SizingDirective):
    """Emplear una fracción del capital disponible.

    fraction : float — fracción del capital a emplear, en (0, 1].
    """

    fraction: float

    def __post_init__(self) -> None:
        if not 0 < self.fraction <= 1:
            raise ValueError("la fracción debe estar entre 0 y 1")


@dataclass(frozen=True, slots=True)
class Shares(SizingDirective):
    """Número exacto de acciones.

    count : int — cantidad de acciones, positiva.
    """

    count: int

    def __post_init__(self) -> None:
        if self.count <= 0:
            raise ValueError("el número de acciones debe ser positivo")


@dataclass(frozen=True, slots=True)
class Multiplier(SizingDirective):
    """Escalar el tamaño normal por convicción.

    factor : float — multiplicador sobre el tamaño por defecto, positivo.
    """

    factor: float

    def __post_init__(self) -> None:
        if self.factor <= 0:
            raise ValueError("el multiplicador debe ser positivo")


@dataclass(frozen=True, slots=True)
class Fraction(SizingDirective):
    """Una fracción del tamaño normal. Lo que usa la variante mixta en cada tramo.

    part : float — fracción del tamaño por defecto, en (0, 1].
    """

    part: float

    def __post_init__(self) -> None:
        if not 0 < self.part <= 1:
            raise ValueError("la fracción debe estar entre 0 y 1")


@dataclass(frozen=True, slots=True)
class Intent:
    """Lo que una estrategia quiere hacer. No es todavía una orden.

    signal_id  : str                    — identificador de correlación de la señal.
    symbol     : str                    — ticker.
    side       : Side                   — compra o venta.
    sizing     : SizingDirective | None — tamaño deseado; None usa el sizer por defecto.
    stop_price : float | None           — precio del stop; None en una salida.
    reason     : str                    — motivo legible, p. ej. "moris:agresiva".
    tag        : str                    — etiqueta del tramo, para la variante mixta.
    timing     : Timing                 — cuándo debe ejecutarse. Ver `Timing`.
    trigger_price : float | None        — precio de disparo cuando `timing` es THIS_STOP.
    """

    signal_id: str
    symbol: str
    side: Side
    sizing: SizingDirective | None = None
    stop_price: float | None = None
    reason: str = ""
    tag: str = ""
    timing: Timing = Timing.NEXT_OPEN
    trigger_price: float | None = None


@dataclass(frozen=True, slots=True)
class Order:
    """Orden ya dimensionada y aprobada, lista para el bróker.

    order_id   : str          — identificador propio de la orden.
    symbol     : str          — ticker.
    side       : Side         — compra o venta.
    shares     : int          — número de acciones, positivo.
    stop_price : float | None — precio del stop protector, si lo lleva.
    signal_id  : str          — identificador de correlación heredado de la señal.
    tag        : str          — etiqueta del tramo.
    limit_price: float | None — precio máximo a pagar. None es orden a mercado, que es lo
                 que se ha hecho siempre: comprar en la apertura de la vela siguiente, sea
                 cual sea. MEDIDO el 2026-09-02 sobre 9.186 operaciones: de los 6,43 de
                 coste por operación, **3,97 son deslizamiento** —el 62 %— y salen de
                 perseguir al precio. La estrategia entra en una zona de soporte, que es un
                 nivel conocido de antemano: es justo donde una limitada tiene sentido.
    reason     : str          — motivo heredado de la intención, p. ej. "moris:stochastic".
                 Viaja hasta `PositionClosed.exit_reason` cuando la orden es la venta que
                 cierra una posición: sin él, toda salida de la estrategia se registraría
                 como un genérico "sell_order" y el informe no podría separar la salida por
                 sobrecompra de cualquier otra venta.
    timing     : Timing       — cuándo se ejecuta respecto a la vela que la produjo.
    trigger_price : float | None — precio al que salta, si `timing` es THIS_STOP.
    """

    order_id: str
    symbol: str
    side: Side
    shares: int
    stop_price: float | None
    signal_id: str
    tag: str = ""
    reason: str = ""
    limit_price: float | None = None
    timing: Timing = Timing.NEXT_OPEN
    trigger_price: float | None = None
