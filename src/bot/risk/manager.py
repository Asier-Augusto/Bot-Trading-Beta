"""Gestor de riesgo: encadena los filtros, dimensiona y recorta.

Es el único punto por el que pasa toda orden, venga de la estrategia que venga. Dicho
de otro modo: el único sitio del sistema capaz de perder toda la cuenta, y por tanto el
único que hay que acertar del todo.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot.contracts.sizer import PositionSizer
from bot.core.context import MarketContext
from bot.core.ids import order_id as make_order_id
from bot.core.types import Intent, Order, Side
from bot.risk.filters import RiskFilter, RiskState
from bot.risk.sizers import _shares_held


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Lo que el riesgo decidió sobre una intención.

    approved         : bool        — si sobrevivió la cadena.
    order            : Order | None— la orden resultante, si se aprobó.
    blocked_by       : str         — filtro que la anuló; vacío si no la anuló nadie.
    reason           : str         — explicación con los números concretos.
    requested_shares : int         — acciones que pedía la estrategia.
    final_shares     : int         — acciones finalmente aprobadas.
    clipped_by       : str         — quién recortó; vacío si nadie recortó.
    """

    approved: bool
    order: Order | None = None
    blocked_by: str = ""
    reason: str = ""
    requested_shares: int = 0
    final_shares: int = 0
    clipped_by: str = ""


class RiskManager:
    """Cadena de filtros más dimensionamiento y recorte a los límites."""

    def __init__(
        self,
        filters: list[RiskFilter],
        sizer: PositionSizer,
        state: RiskState,
        entry_limit: bool = False,
    ) -> None:
        """Crea el gestor.

        filters : list[RiskFilter] — filtros en el orden en que se aplican.
        sizer   : PositionSizer    — dimensionador por defecto.
        state   : RiskState        — estado global de la sesión, mutable.
        entry_limit : bool — True hace que las COMPRAS salgan con precio límite en el precio
                      que la señal vio, en vez de a mercado. MEDIDO el 2026-09-02 sobre
                      9.186 operaciones: de los 6,43 de coste por operación, **3,97 son
                      deslizamiento** —el 62 %— y salen de comprar en la apertura de la vela
                      siguiente sea cual sea. La estrategia entra en una zona de soporte,
                      que es un nivel conocido de antemano.
        """
        self.filters = filters
        self.sizer = sizer
        self.state = state
        self.entry_limit = entry_limit

    def evaluate(self, intent: Intent, ctx: MarketContext) -> RiskDecision:
        """Somete una intención a la cadena completa.

        intent : Intent        — lo que la estrategia quiere hacer.
        ctx    : MarketContext — contexto de la vela evaluada.
        return : RiskDecision  — orden aprobada, o el motivo del bloqueo.
        """
        for risk_filter in self.filters:
            verdict = risk_filter.check(intent, ctx, self.state)
            if not verdict.allowed:
                return RiskDecision(
                    approved=False, blocked_by=verdict.rule, reason=verdict.reason
                )

        entry_price = ctx.bar.close
        requested = self.sizer.size(intent, ctx, entry_price)
        final, clipped_by, reason = self._clip(intent, ctx, requested, entry_price)

        if final <= 0:
            return RiskDecision(
                approved=False,
                blocked_by=clipped_by or "sizer",
                reason=reason or "el tamaño calculado es cero",
                requested_shares=requested,
                final_shares=0,
                clipped_by=clipped_by,
            )

        order = Order(
            # Determinista: ver `bot.core.ids`. Incluye la etiqueta porque los dos tramos
            # de una variante mixta nacen de la misma señal y son órdenes distintas.
            order_id=make_order_id(
                intent.signal_id, intent.side.value, intent.tag, ctx.bar.ts
            ),
            symbol=intent.symbol,
            side=intent.side,
            shares=final,
            stop_price=intent.stop_price,
            signal_id=intent.signal_id,
            tag=intent.tag,
            reason=intent.reason,
            # Solo en las COMPRAS. Salir es urgente: una venta que no ejecuta deja la
            # posición abierta contra su propio motivo de salida, que es la peor forma de
            # ahorrarse unos céntimos.
            limit_price=(
                entry_price
                if self.entry_limit and intent.side is Side.BUY
                else None
            ),
            # La temporizacion viaja intacta de la intencion a la orden: es la estrategia
            # quien sabe si lo que ha visto es un rebote que se compra al cierre o una
            # ruptura que ya tenia una orden stop puesta. El riesgo decide CUANTO, no
            # CUANDO.
            timing=intent.timing,
            trigger_price=intent.trigger_price,
        )
        self.state.pending_order_keys.add((intent.symbol, intent.side.value, intent.tag))

        return RiskDecision(
            approved=True,
            order=order,
            requested_shares=requested,
            final_shares=final,
            clipped_by=clipped_by,
            reason=reason,
        )

    def release_order(self, order: Order) -> None:
        """Libera la clave anti-duplicado de una orden cuyo ciclo de vida ha terminado.

        Debe invocarse cuando la orden queda resuelta -- ejecutada, cancelada o
        rechazada -- para que DuplicateOrderFilter deje de considerarla pendiente. Sin
        esta llamada, la clave (símbolo, sentido, etiqueta) se quedaría en
        `pending_order_keys` para siempre, y un simple rechazo del bróker dejaría esa
        posición sin poder cerrarse nunca: exactamente el fallo que este método existe
        para evitar.

        order  : Order — la orden cuyo ciclo de vida ha terminado.
        return : None
        """
        self.state.release(order.symbol, order.side.value, order.tag)

    def _clip(
        self,
        intent: Intent,
        ctx: MarketContext,
        requested: int,
        entry_price: float,
    ) -> tuple[int, str, str]:
        """Recorta el tamaño a los límites globales.

        intent      : Intent        — intención evaluada.
        ctx         : MarketContext — de aquí sale el poder de compra.
        requested   : int           — acciones pedidas.
        entry_price : float         — precio de referencia.
        return      : tuple[int, str, str] — (acciones finales, quién recortó, motivo).
        """
        if intent.side is Side.SELL:
            # Vender NO consume poder de compra, así que ese recorte no se le aplica:
            # hacerlo dejaría posiciones sin poder cerrarse justo cuando la cuenta está
            # invertida, que es cuando más urge cerrarlas. Lo que sí lo limita es lo que
            # hay en cartera: una venta por más acciones de las que se tienen es un corto
            # accidental, y el Plan 1 no modela cortos.
            en_cartera = _shares_held(ctx, intent.tag)
            if requested > en_cartera:
                return (
                    en_cartera,
                    "position",
                    f"recortado de {requested} a {en_cartera} acciones: es lo que hay "
                    f"abierto en {intent.symbol}"
                    + (f" con etiqueta '{intent.tag}'" if intent.tag else ""),
                )
            return requested, "", ""

        affordable = int(ctx.account.buying_power / entry_price) if entry_price > 0 else 0
        if requested > affordable:
            return (
                affordable,
                "buying_power",
                f"recortado de {requested} a {affordable} acciones por poder de compra",
            )
        return requested, "", ""
