"""Vocabulario de eventos.

Todo evento es una foto congelada y autocontenida del hecho que registra: nunca lleva
una referencia a un objeto vivo. El motivo está en §2.2 del diseño — el sink escribe
cientos de milisegundos después, y para entonces un objeto mutable ya habría cambiado.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from bot.core.types import Bar, Side

#: Versión del esquema de eventos. Se sube al cambiar la forma de cualquier evento,
#: para poder mezclar logs de distintas épocas al construir el conjunto de datos.
SCHEMA_VERSION = 1


class Priority(enum.IntEnum):
    """Prioridad frente a la contrapresión del bus.

    Cuanto menor el valor, más se protege el evento.
    """

    CRITICAL = 0
    NORMAL = 1
    DROPPABLE = 2


def _plain(value: Any) -> Any:
    """Convierte un valor a algo serializable en JSON.

    value  : Any — valor cualquiera salido de un dataclass.
    return : Any — el mismo valor con fechas en ISO y enumerados como cadena.
    """
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class Event:
    """Base de todo evento.

    ts               : datetime — instante del reloj inyectado.
    signal_id        : str      — identificador de correlación; vacío si no aplica.
    strategy_version : str      — versión del código de estrategia que lo produjo.
    config_hash      : str      — resumen de la configuración activa.
    """

    ts: dt.datetime
    signal_id: str = ""
    strategy_version: str = ""
    config_hash: str = ""

    #: Prioridad por defecto. Cada subclase la ajusta si procede.
    priority: Priority = field(default=Priority.NORMAL, init=False, repr=False)

    @property
    def kind(self) -> str:
        """Discriminador de tipo del evento.

        return : str — nombre de la clase, que es el que va al log.
        """
        return type(self).__name__

    def to_dict(self) -> dict[str, Any]:
        """Serializa el evento a un diccionario apto para JSON.

        Permite que cualquier logger lo escriba sin conocer sus campos.

        return : dict — campos del evento más `kind` y `schema_version`.
        """
        data = {f.name: _plain(getattr(self, f.name)) for f in fields(self)}
        data.pop("priority", None)
        data["kind"] = self.kind
        data["schema_version"] = SCHEMA_VERSION
        return data

    @property
    def schema_version(self) -> int:
        """Versión del esquema con la que se creó el evento.

        return : int
        """
        return SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class BarClosed(Event):
    """Una vela ha cerrado.

    bar : Bar — la vela cerrada.
    """

    bar: Bar | None = None
    priority: Priority = field(default=Priority.DROPPABLE, init=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Serializa incluyendo la vela desplegada.

        return : dict
        """
        data = super().to_dict()
        data["bar"] = _plain(asdict(self.bar)) if self.bar else None
        return data


@dataclass(frozen=True, slots=True)
class SignalEvaluated(Event):
    """Resultado de evaluar la estrategia sobre un cierre de vela.

    symbol     : str   — ticker.
    strategy   : str   — nombre registrado de la estrategia.
    level      : str   — ARMED, NEAR_MISS, PARTIAL o IDLE.
    total      : float — puntuación total normalizada, en [0, 1].
    armed      : bool  — si todas las condiciones se cumplen literalmente.
    conditions : list  — una entrada por condición, con valor crudo y puntuación.
    blocking   : list  — nombres de las condiciones que no se cumplen.
    """

    symbol: str = ""
    strategy: str = ""
    level: str = "IDLE"
    total: float = 0.0
    armed: bool = False
    conditions: tuple[dict[str, Any], ...] = ()
    blocking: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IdleSummary(Event):
    """Resumen agregado de las velas que no llegaron al nivel mínimo de registro.

    symbol        : str   — ticker.
    window_start  : datetime — inicio del periodo agregado.
    bars          : int   — velas evaluadas en el periodo.
    blocked_by    : dict  — cuántas veces bloqueó cada condición.
    mean_total    : float — puntuación media del periodo.
    max_total     : float — puntuación máxima del periodo.
    """

    symbol: str = ""
    window_start: dt.datetime | None = None
    bars: int = 0
    blocked_by: dict[str, int] = field(default_factory=dict)
    mean_total: float = 0.0
    max_total: float = 0.0


@dataclass(frozen=True, slots=True)
class IntentRaised(Event):
    """Una estrategia ha expresado una intención.

    symbol : str  — ticker.
    side   : Side — compra o venta.
    reason : str  — motivo legible.
    tag    : str  — etiqueta del tramo.
    """

    symbol: str = ""
    side: Side = Side.BUY
    reason: str = ""
    tag: str = ""


@dataclass(frozen=True, slots=True)
class RiskBlocked(Event):
    """El riesgo ha anulado una intención.

    symbol : str — ticker.
    rule   : str — nombre del filtro que bloqueó.
    reason : str — explicación con los números concretos.
    """

    symbol: str = ""
    rule: str = ""
    reason: str = ""
    priority: Priority = field(default=Priority.CRITICAL, init=False, repr=False)


@dataclass(frozen=True, slots=True)
class SizeComputed(Event):
    """Resultado del dimensionamiento, con quién recortó y por qué.

    symbol    : str       — ticker.
    requested : int       — acciones que pedía la estrategia.
    final     : int       — acciones finalmente aprobadas.
    clipped_by: str       — filtro que recortó; vacío si no recortó nadie.
    reason    : str       — explicación del recorte.
    """

    symbol: str = ""
    requested: int = 0
    final: int = 0
    clipped_by: str = ""
    reason: str = ""
    priority: Priority = field(default=Priority.CRITICAL, init=False, repr=False)


@dataclass(frozen=True, slots=True)
class OrderSubmitted(Event):
    """Se ha enviado una orden al bróker.

    order_id   : str          — identificador propio.
    symbol     : str          — ticker.
    side       : Side         — compra o venta.
    shares     : int          — acciones.
    stop_price : float | None — stop protector.
    tag        : str          — etiqueta del tramo.
    """

    order_id: str = ""
    symbol: str = ""
    side: Side = Side.BUY
    shares: int = 0
    stop_price: float | None = None
    tag: str = ""
    priority: Priority = field(default=Priority.CRITICAL, init=False, repr=False)


@dataclass(frozen=True, slots=True)
class Fill(Event):
    """Una orden se ha ejecutado.

    order_id : str   — identificador de la orden.
    symbol   : str   — ticker.
    side     : Side  — compra o venta.
    shares   : int   — acciones ejecutadas.
    price    : float — precio de ejecución, ya con deslizamiento aplicado.
    tag      : str   — etiqueta del tramo. Con la variante mixta hay dos ejecuciones del
               mismo símbolo y la misma señal, y sin la etiqueta no se pueden distinguir.
    """

    order_id: str = ""
    symbol: str = ""
    side: Side = Side.BUY
    shares: int = 0
    price: float = 0.0
    tag: str = ""
    priority: Priority = field(default=Priority.CRITICAL, init=False, repr=False)


@dataclass(frozen=True, slots=True)
class OrderRejected(Event):
    """El bróker no ha podido aceptar una orden y la descarta.

    Es uno de los dos finales de ciclo de vida distintos del `Fill`. Importa que
    llegue al motor: sin él, la clave anti-duplicado de esta orden se quedaría viva
    para siempre y ninguna orden equivalente volvería a pasar el filtro — con una
    venta rechazada, eso significa una posición que ya no se puede cerrar.

    order_id : str — identificador de la orden rechazada.
    symbol   : str — ticker.
    reason   : str — motivo legible del rechazo.
    """

    order_id: str = ""
    symbol: str = ""
    reason: str = ""
    priority: Priority = field(default=Priority.CRITICAL, init=False, repr=False)


@dataclass(frozen=True, slots=True)
class OrderCancelled(Event):
    """Una orden viva se ha retirado antes de ejecutarse.

    El otro final de ciclo de vida distinto del `Fill`, con las mismas consecuencias
    para la clave anti-duplicado.

    order_id : str — identificador de la orden cancelada.
    symbol   : str — ticker.
    reason   : str — motivo legible de la cancelación.
    """

    order_id: str = ""
    symbol: str = ""
    reason: str = ""
    priority: Priority = field(default=Priority.CRITICAL, init=False, repr=False)


@dataclass(frozen=True, slots=True)
class TriggerArmed(Event):
    """El gatillo de un símbolo ha quedado ARMADO. Ocurre UNA vez por señal.

    Existe desde el 2026-09-10 y nació de un fallo de lectura: el visor marcaba un ▲ ARMA
    por cada evaluación cuyo nivel era ARMED, y como las cuatro condiciones suelen seguir
    cumpliéndose varios minutos seguidos, se veían tres y cuatro armados en fila donde la
    estrategia había armado UNO. La estrategia nunca armó dos veces —`_maybe_arm` se corta
    si ya hay gatillo—, pero no dejaba constancia de cuándo armaba, así que quien leía los
    registros solo podía adivinarlo. Este evento es esa constancia.

    symbol     : str      — ticker.
    zone_low   : float    — suelo de la zona sobre la que se arma.
    zone_high  : float    — techo de la zona.
    atr        : float    — ATR de 5' congelado al armar; con él se mide el rebote.
    expires_at : datetime | None — cuándo caduca la ventana si no dispara nadie.
    total      : float    — puntuación de la evaluación que armó.
    """

    symbol: str = ""
    zone_low: float = 0.0
    zone_high: float = 0.0
    atr: float = 0.0
    expires_at: dt.datetime | None = None
    total: float = 0.0


@dataclass(frozen=True, slots=True)
class ArmExpired(Event):
    """La ventana de armado se cerró sin que dispararan todos los tramos.

    symbol    : str  — ticker.
    fired     : list — etiquetas de los tramos que sí dispararon.
    not_fired : list — etiquetas de los que no. Es el dato que dice, sobre las señales
                       reales, cuántas veces la variante conservadora se quedó esperando
                       una ruptura que no llegó mientras la agresiva ya había entrado:
                       exactamente la comparación que el §7.1 del diseño busca.
    armed_at  : datetime | None — cuándo se armó el gatillo.
    """

    symbol: str = ""
    fired: tuple[str, ...] = ()
    not_fired: tuple[str, ...] = ()
    armed_at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class StopModified(Event):
    """El stop protector de una posición viva se ha movido.

    El stop vive en el bróker como orden real (§7.3), de modo que la posición sigue
    protegida aunque el proceso muera. Lo que el bot hace es MODIFICARLO, y este evento es
    el rastro de esa modificación: sin él no se podría reconstruir después por qué una
    posición salió al precio al que salió.

    symbol   : str   — ticker.
    tag      : str   — etiqueta del tramo.
    old_stop : float — precio anterior del stop.
    new_stop : float — precio nuevo. Nunca menor que el anterior.
    reason   : str   — quién lo movió, p. ej. "trailing".
    """

    symbol: str = ""
    tag: str = ""
    old_stop: float = 0.0
    new_stop: float = 0.0
    reason: str = ""
    priority: Priority = field(default=Priority.CRITICAL, init=False, repr=False)


@dataclass(frozen=True, slots=True)
class PositionOpened(Event):
    """Se ha abierto una posición.

    symbol     : str   — ticker.
    shares     : int   — acciones.
    entry_price: float — precio medio de entrada.
    stop_price : float — stop protector.
    tag        : str   — etiqueta del tramo.
    """

    symbol: str = ""
    shares: int = 0
    entry_price: float = 0.0
    stop_price: float = 0.0
    tag: str = ""
    #: Cuánto recorrido tenía por delante la operación EN EL INSTANTE DE ENTRAR, medido en
    #: múltiplos del riesgo asumido: `(objetivo - entrada) / (entrada - stop)`.
    #:
    #: Son observación pura y no tocan ninguna decisión. Existen porque el barrido de
    #: 2018-2025 dejó una conclusión incómoda: a las operaciones que llegaron a su objetivo
    #: el mercado solo llegó a ofrecerles 0,717R y hacían falta 0,74R para empatar, así que
    #: **ninguna mejora de la salida puede arreglar la estrategia**. Lo que falta es poder
    #: decidir al ENTRAR si la operación merece la pena, y para eso hace falta un objetivo
    #: legible en ese instante — que el estocástico no da, porque al entrar nadie sabe dónde
    #: va a saltar.
    #:
    #: None significa "no se pudo medir", que no es lo mismo que cero: un cero se leería
    #: como "no había recorrido", que es una conclusión y no un dato que falta.
    r_to_vwap: float | None = None
    r_to_resistance: float | None = None
    priority: Priority = field(default=Priority.CRITICAL, init=False, repr=False)


@dataclass(frozen=True, slots=True)
class PositionClosed(Event):
    """Se ha cerrado una posición, con su resultado completo.

    symbol      : str   — ticker.
    shares      : int   — acciones.
    entry_price : float — precio medio de entrada.
    exit_price  : float — precio medio de salida.
    pnl         : float — resultado NETO en divisa: bruto menos comisiones menos
                          deslizamiento. Coincide exactamente con lo que se movió la caja.
    gross_pnl   : float — resultado BRUTO: `(salida - entrada) * acciones` con los precios
                          SIN deslizamiento y sin comisiones. Está separado porque es la
                          única forma de saber si la estrategia tiene ventaja propia o si
                          solo parece tenerla: MEDIDO el 2026-09-01 sobre 9.186 operaciones,
                          el bruto era positivo y el neto negativo, y esa diferencia decide
                          si hay algo que ajustar o algo que abandonar.
    commission  : float — comisiones de las DOS patas, entrada y salida.
    slippage    : float — lo que costó el deslizamiento en las dos patas, en divisa.
    r_multiple  : float — resultado en múltiplos de R.
    exit_reason : str   — qué la cerró. Los motivos que el código produce hoy son
                          exactamente estos:
                            "stop"          — el recorrido de la vela perforó el stop
                                              protector con el que se abrió la posición.
                            "trailing_stop" — ídem, pero sobre un stop que el trailing
                                              ya había subido. Distinguirlos es lo que
                                              permite responder después "¿cuánto aportó
                                              el trailing?".
                            "sell_order"    — venta enviada por la estrategia, ejecutada
                                              en la apertura de la vela siguiente.
                            "stochastic"    — la salida por sobrecompra de Moris (§7.3):
                                              es un `sell_order` cuyo motivo concreto
                                              viaja desde el `Intent` hasta aquí.
                            "force_close"   — hora de cierre forzado de la sesión.
                            "end_of_data"   — se agotó el feed con la posición abierta.
                          Los dos primeros los decide `SimulatedBroker` por su cuenta;
                          los demás los recibe como texto, del motor (`close_all`) o de
                          la propia orden (`Order.reason`). Al añadir una vía de cierre
                          nueva hay que ampliar esta lista en el mismo commit.
    mae         : float — máxima excursión adversa, en divisa por acción.
    mfe         : float — máxima excursión favorable, en divisa por acción.
    tag         : str   — etiqueta del tramo.
    """

    symbol: str = ""
    shares: int = 0
    entry_price: float = 0.0
    exit_price: float = 0.0
    pnl: float = 0.0
    gross_pnl: float = 0.0
    commission: float = 0.0
    slippage: float = 0.0
    r_multiple: float = 0.0
    exit_reason: str = ""
    mae: float = 0.0
    mfe: float = 0.0
    tag: str = ""
    priority: Priority = field(default=Priority.CRITICAL, init=False, repr=False)


@dataclass(frozen=True, slots=True)
class ShadowTradeOpened(Event):
    """Se ha abierto una operación SOMBRA: la que se habría tomado si nada la hubiera frenado.

    No existe en el mercado ni consume capital. La abre el rastreador contrafactual (§10.3) al
    ver una evaluación que se quedó cerca, y sirve para responder a la única pregunta que los
    registros de operaciones reales no pueden responder: **si este filtro no hubiera
    bloqueado, ¿habría ganado o perdido dinero?**

    symbol      : str   — ticker.
    blocked_by  : tuple[str, ...] — condiciones que impidieron la entrada. ES el dato del
                  análisis entero: sin él no se puede decir qué filtro cuesta dinero.
    level       : str   — nivel de la evaluación que la originó.
    total       : float — puntuación total de esa evaluación.
    entry_price : float — precio al que habría entrado.
    stop_price  : float — dónde habría ido el stop.
    """

    symbol: str = ""
    blocked_by: tuple[str, ...] = ()
    level: str = ""
    total: float = 0.0
    entry_price: float = 0.0
    stop_price: float = 0.0


@dataclass(frozen=True, slots=True)
class ShadowTradeClosed(Event):
    """Una operación sombra ha terminado, con las MISMAS reglas de salida que las reales.

    **No lleva P&L en divisa a propósito.** La sombra no consumió capital, así que una cifra
    en euros invitaría a sumarla al resultado real, y el §10.3 avisa explícitamente de que no
    son sumables: esa operación habría consumido capital y quizá habría bloqueado otra que sí
    se tomó. R sí es comparable, porque es adimensional.

    symbol      : str   — ticker.
    blocked_by  : tuple[str, ...] — condiciones que impidieron la entrada.
    entry_price : float — precio de entrada imaginario.
    exit_price  : float — precio de salida imaginario.
    r_multiple  : float — resultado en múltiplos del riesgo asumido.
    exit_reason : str   — qué la cerró; los mismos motivos que una real.
    bars_held   : int   — velas que duró.
    """

    symbol: str = ""
    blocked_by: tuple[str, ...] = ()
    entry_price: float = 0.0
    exit_price: float = 0.0
    r_multiple: float = 0.0
    exit_reason: str = ""
    bars_held: int = 0


@dataclass(frozen=True, slots=True)
class SystemEvent(Event):
    """Hecho del sistema: arranque, parada, reconexión, error.

    level   : str — INFO, WARNING o ERROR.
    message : str — descripción legible.
    detail  : dict— datos adicionales.
    """

    level: str = "INFO"
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    priority: Priority = field(default=Priority.CRITICAL, init=False, repr=False)
