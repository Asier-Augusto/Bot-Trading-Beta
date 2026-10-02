"""Dimensionamiento de posiciones y resolución de las directivas de tamaño."""

from __future__ import annotations

from bot.contracts.sizer import PositionSizer
from bot.core.context import MarketContext
from bot.core.registry import register_sizer
from bot.core.types import Equity, Fraction, Intent, Multiplier, Risk, Shares, Side


def _shares_held(ctx: MarketContext, tag: str) -> int:
    """Acciones abiertas del tramo indicado de este símbolo.

    ctx    : MarketContext — de aquí salen los tramos abiertos.
    tag    : str — etiqueta del tramo que se quiere cerrar. La cadena vacía significa "toda
             la posición", que es lo que usan el cierre forzado y las estrategias de un solo
             tramo.
    return : int — acciones; 0 si no hay nada que vender, con lo que el gestor bloqueará la
             intención como "el tamaño calculado es cero", que es exactamente lo que debe
             pasar.
    """
    if tag:
        del_tramo = [p for p in ctx.positions if p.tag == tag]
        if del_tramo:
            return sum(p.shares for p in del_tramo)
        # Sin tramo con esa etiqueta no hay nada que vender. NO se cae a la posición
        # entera: vender los dos tramos cuando la estrategia pidió cerrar uno sería peor
        # que no vender nada.
        return 0
    if ctx.positions:
        return sum(p.shares for p in ctx.positions)
    return ctx.position.shares if ctx.position is not None else 0


@register_sizer("percent_equity")
class PercentEquitySizer(PositionSizer):
    """Dimensiona por porcentaje del patrimonio, con directivas que lo sobrescriben."""

    def __init__(self, pct: float = 0.20) -> None:
        """Crea el dimensionador.

        pct : float — fracción del patrimonio a emplear por operación, en (0, 1].
        """
        if not 0 < pct <= 1:
            raise ValueError("el porcentaje debe estar entre 0 y 1")
        self.pct = pct

    def size(self, intent: Intent, ctx: MarketContext, entry_price: float) -> int:
        """Calcula cuántas acciones corresponden a una intención.

        Una directiva mal formada (riesgo sin stop, stop del lado equivocado) no
        revienta: cae al tamaño por defecto. Un fallo de la estrategia no debe abortar
        la operativa, y el recorte del riesgo sigue actuando después.

        intent      : Intent        — intención, con su directiva opcional.
        ctx         : MarketContext — contexto, para la foto de cuenta.
        entry_price : float         — precio de referencia de la entrada.
        return      : int           — acciones, nunca negativo.
        """
        if entry_price <= 0:
            return 0

        directive = intent.sizing

        # Una VENTA sin directiva significa "cierra lo que hay", no "abre una posición del
        # tamaño normal". Antes devolvía `pct x patrimonio / precio`: un número inventado
        # que no tenía nada que ver con las acciones en cartera. Hoy lo tapaba el bróker
        # simulado, que cierra la posición entera y avisa, así que el backtest salía bien
        # mientras `SizeComputed` registraba una cifra falsa; contra IBKR habría sido una
        # orden por la cantidad equivocada, y con dos tramos abiertos, por la del tramo que
        # no era.
        if intent.side is Side.SELL and directive is None:
            return _shares_held(ctx, intent.tag)

        default = int(self.pct * ctx.account.equity / entry_price)

        if directive is None:
            return max(default, 0)
        if isinstance(directive, Shares):
            return directive.count
        if isinstance(directive, Equity):
            return max(int(directive.fraction * ctx.account.equity / entry_price), 0)
        if isinstance(directive, Multiplier):
            return max(int(default * directive.factor), 0)
        if isinstance(directive, Fraction):
            return max(int(default * directive.part), 0)
        if isinstance(directive, Risk):
            if intent.stop_price is None:
                return max(default, 0)
            r = entry_price - intent.stop_price
            if r <= 0:
                return max(default, 0)
            return max(int(directive.fraction * ctx.account.equity / r), 0)

        return max(default, 0)
