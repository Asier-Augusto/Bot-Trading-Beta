"""Filtros de riesgo. Toda intención los atraviesa antes de convertirse en orden.

Cada filtro es una clase independiente registrada por nombre: añadir uno nuevo es un
fichero más, igual que las estrategias.

Regla transversal, con dos partes que no deben confundirse:

- Los limites de riesgo y exposicion (ventana horaria, kill switch, una posicion por
  simbolo, maximo de posiciones, poder de compra) nunca bloquean una venta. Un kill
  switch que impidiera vender dejaria todas las posiciones abiertas justo cuando mas
  urge cerrarlas, que es exactamente lo contrario de su proposito.
- La guarda de envio unico (DuplicateOrderFilter) si se aplica tambien a las ventas:
  si no lo hiciera, una venta reenviada antes de que llegue la confirmacion de la
  primera crearia un corto accidental. Lo que la hace segura, en vez de una trampa que
  deja posiciones atrapadas, es que su clave se libera explicitamente cuando la orden
  se resuelve (ejecutada, cancelada o rechazada): ver RiskState.release y
  RiskManager.release_order. Una clave que nunca se libera convertiria un simple
  rechazo del broker en una posicion que ya no se puede cerrar nunca.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from bot.core.context import MarketContext, Position
from bot.core.registry import register_risk_filter
from bot.core.types import Intent, Side


@dataclass(frozen=True, slots=True)
class FilterVerdict:
    """Resultado de pasar una intención por un filtro.

    allowed : bool — si la intención sigue viva.
    rule    : str  — nombre del filtro que decidió.
    reason  : str  — explicación con los números concretos, para el log.
    """

    allowed: bool
    rule: str = ""
    reason: str = ""

    @staticmethod
    def ok() -> "FilterVerdict":
        """Veredicto de paso.

        return : FilterVerdict — permitido, sin motivo.
        """
        return FilterVerdict(True)


@dataclass
class RiskState:
    """Estado global que los filtros necesitan y que el gestor mantiene al día.

    open_positions     : dict[str, Position] — posiciones vivas, por símbolo.
    daily_pnl          : float — resultado acumulado de la sesión, en divisa.
    starting_equity    : float — patrimonio al abrir la sesión, base del kill switch.
    pending_order_keys : set   — claves (símbolo, sentido, etiqueta) ya enviadas.
    """

    open_positions: dict[str, Position] = field(default_factory=dict)
    daily_pnl: float = 0.0
    starting_equity: float = 0.0
    pending_order_keys: set[tuple[str, str, str]] = field(default_factory=set)

    def release(self, symbol: str, side: str, tag: str) -> None:
        """Libera UNA clave de orden pendiente, y solo esa.

        Se invoca cuando el ciclo de vida de una orden concreta termina (ejecutada,
        cancelada o rechazada), para que DuplicateOrderFilter deje de considerarla
        pendiente. No es un clear(): las demas claves pendientes (por ejemplo, el otro
        tramo de una variante mixta) siguen intactas. Si la clave no esta presente no
        hace nada; liberar de mas nunca debe reventar.

        symbol : str — ticker de la orden que se resuelve.
        side   : str — sentido como texto, p. ej. "BUY" o "SELL".
        tag    : str — etiqueta del tramo.
        return : None
        """
        self.pending_order_keys.discard((symbol, side, tag))


class RiskFilter(ABC):
    """Una regla que puede anular una intención."""

    rule: str = ""

    @abstractmethod
    def check(self, intent: Intent, ctx: MarketContext, state: RiskState) -> FilterVerdict:
        """Decide si la intención sigue adelante.

        intent : Intent        — lo que la estrategia quiere hacer.
        ctx    : MarketContext — contexto de la vela evaluada.
        state  : RiskState     — estado global de la sesión.
        return : FilterVerdict — permitido o bloqueado, con su motivo.
        """


@register_risk_filter("trading_window")
class TradingWindowFilter(RiskFilter):
    """Prohíbe abrir posiciones fuera de la franja de entradas."""

    rule = "trading_window"

    def check(self, intent: Intent, ctx: MarketContext, state: RiskState) -> FilterVerdict:
        """Bloquea entradas fuera de franja; nunca bloquea salidas.

        intent : Intent        — intención evaluada.
        ctx    : MarketContext — lleva la etiqueta de franja y si admite entradas.
        state  : RiskState     — no se usa.
        return : FilterVerdict
        """
        if intent.side is Side.SELL:
            return FilterVerdict.ok()
        if ctx.entries_allowed:
            return FilterVerdict.ok()
        return FilterVerdict(
            False, self.rule, f"entradas cerradas en la franja '{ctx.session_label}'"
        )


@register_risk_filter("kill_switch")
class KillSwitchFilter(RiskFilter):
    """Detiene las entradas cuando la pérdida del día supera el límite."""

    rule = "kill_switch"

    def __init__(self, max_daily_loss_pct: float = 0.03) -> None:
        """Crea el filtro.

        max_daily_loss_pct : float — fracción del patrimonio inicial del día que, al
                                     perderse, corta las entradas.
        """
        self.max_daily_loss_pct = max_daily_loss_pct

    def check(self, intent: Intent, ctx: MarketContext, state: RiskState) -> FilterVerdict:
        """Bloquea entradas si se ha superado la pérdida máxima diaria.

        intent : Intent        — intención evaluada.
        ctx    : MarketContext — no se usa.
        state  : RiskState     — de aquí salen el resultado del día y la base.
        return : FilterVerdict
        """
        if intent.side is Side.SELL:
            return FilterVerdict.ok()
        limit = -abs(self.max_daily_loss_pct * state.starting_equity)
        if state.daily_pnl < limit:
            return FilterVerdict(
                False,
                self.rule,
                f"pérdida diaria {state.daily_pnl:.2f} por debajo del límite {limit:.2f}",
            )
        return FilterVerdict.ok()


@register_risk_filter("one_position_per_symbol")
class OnePositionPerSymbolFilter(RiskFilter):
    """Impide abrir una segunda posición en un símbolo que ya la tiene."""

    rule = "one_position_per_symbol"

    def check(self, intent: Intent, ctx: MarketContext, state: RiskState) -> FilterVerdict:
        """Bloquea si ya hay una posición viva de OTRA señal en el símbolo.

        "De otra señal" es la parte que importa. El §7.1 del diseño dice que los tramos de
        una misma entrada comparten `signal_id` y **cuentan como una sola posición** a
        efectos de este límite. Sin esa distinción, la variante mixta no puede existir: el
        tramo agresivo se ejecuta un minuto antes que el conservador, así que cuando la
        intención del segundo llega aquí ya hay posición abierta en el símbolo y este filtro
        la bloqueaba. Medido en el backtest dorado: la mixta abría solo el tramo agresivo y
        el conservador aparecía en `risk.jsonl` bloqueado, con lo que la comparación entre
        variantes —que es el motivo entero de la variante mixta— no llegaba a producirse.

        Lo que el filtro sigue impidiendo, que es su verdadero propósito, es acumular
        posición en el mismo símbolo por señales distintas: dos entradas seguidas de Moris
        en TSLA siguen siendo dos posiciones y la segunda se bloquea.

        intent : Intent        — intención evaluada.
        ctx    : MarketContext — no se usa.
        state  : RiskState     — de aquí salen las posiciones abiertas, ya agregadas por
                 símbolo. Su `signal_id` es el común de los tramos, o vacío si difieren.
        return : FilterVerdict
        """
        if intent.side is Side.SELL:
            return FilterVerdict.ok()

        abierta = state.open_positions.get(intent.symbol)
        if abierta is None:
            return FilterVerdict.ok()
        if abierta.signal_id and abierta.signal_id == intent.signal_id:
            return FilterVerdict.ok()

        return FilterVerdict(
            False,
            self.rule,
            f"ya hay posición abierta en {intent.symbol} de otra señal "
            f"({abierta.signal_id or 'sin identificador'})",
        )


@register_risk_filter("max_positions")
class MaxPositionsFilter(RiskFilter):
    """Limita cuántas posiciones pueden estar abiertas a la vez."""

    rule = "max_positions"

    def __init__(self, max_positions: int = 3) -> None:
        """Crea el filtro.

        max_positions : int — número máximo de posiciones simultáneas.
        """
        self.max_positions = max_positions

    def check(self, intent: Intent, ctx: MarketContext, state: RiskState) -> FilterVerdict:
        """Bloquea si se ha alcanzado el máximo de posiciones simultáneas.

        intent : Intent        — intención evaluada.
        ctx    : MarketContext — no se usa.
        state  : RiskState     — de aquí sale el recuento de posiciones.
        return : FilterVerdict
        """
        if intent.side is Side.SELL:
            return FilterVerdict.ok()
        open_count = len(state.open_positions)
        if open_count >= self.max_positions:
            return FilterVerdict(
                False, self.rule, f"{open_count} posiciones abiertas, máximo {self.max_positions}"
            )
        return FilterVerdict.ok()


@register_risk_filter("buying_power")
class BuyingPowerFilter(RiskFilter):
    """Impide comprar sin poder de compra."""

    rule = "buying_power"

    def check(self, intent: Intent, ctx: MarketContext, state: RiskState) -> FilterVerdict:
        """Bloquea compras si no queda poder de compra para al menos una acción.

        intent : Intent        — intención evaluada.
        ctx    : MarketContext — de aquí sale la foto de cuenta.
        state  : RiskState     — no se usa.
        return : FilterVerdict
        """
        if intent.side is Side.SELL:
            return FilterVerdict.ok()
        if ctx.account.buying_power < ctx.bar.close:
            return FilterVerdict(
                False,
                self.rule,
                f"poder de compra {ctx.account.buying_power:.2f} < precio {ctx.bar.close:.2f}",
            )
        return FilterVerdict.ok()


@register_risk_filter("duplicate_order")
class DuplicateOrderFilter(RiskFilter):
    """Impide enviar dos veces la misma orden, también cuando es una venta.

    A diferencia de los otros cinco filtros, este SÍ se aplica a Side.SELL: si no lo
    hiciera, una venta reenviada antes de que llegue la confirmación de la primera
    crearía un corto accidental, tan indeseable como quedarse atrapado sin poder
    cerrar. Lo que hace segura esta guarda no es dejar pasar las ventas, sino que su
    clave se libera de forma explícita en cuanto la orden se resuelve (ejecutada,
    cancelada o rechazada) vía RiskState.release / RiskManager.release_order. Sin esa
    liberación, un simple rechazo del bróker dejaría la clave viva para siempre y la
    posición ya no podría cerrarse nunca.

    La clave incluye la etiqueta del tramo, porque los dos tramos de la variante mixta
    son órdenes legítimamente distintas de una misma señal.
    """

    rule = "duplicate_order"

    def check(self, intent: Intent, ctx: MarketContext, state: RiskState) -> FilterVerdict:
        """Bloquea si ya hay una orden pendiente con la misma clave.

        intent : Intent        — intención evaluada.
        ctx    : MarketContext — no se usa.
        state  : RiskState     — de aquí salen las claves pendientes.
        return : FilterVerdict
        """
        key = (intent.symbol, intent.side.value, intent.tag)
        if key in state.pending_order_keys:
            return FilterVerdict(False, self.rule, f"orden duplicada: {key}")
        return FilterVerdict.ok()
