"""Rango verdadero medio con el suavizado de Wilder, en versión incremental."""

from __future__ import annotations

from bot.core.registry import register_indicator
from bot.core.types import Bar
from bot.indicators.base import Indicator


@register_indicator("atr")
class ATR(Indicator):
    """ATR de Wilder.

    Estado que guarda: ATR previo y cierre previo. El cierre previo hace falta porque
    el rango verdadero contempla el hueco entre sesiones o entre velas: una vela que
    abre lejos del cierre anterior es más volátil de lo que dice su propio recorrido.
    """

    def __init__(self, period: int = 14) -> None:
        """Crea el indicador.

        period : int — número de velas del promedio. Debe ser positivo.
        raises : ValueError — si el periodo no es positivo.
        """
        super().__init__()
        if period <= 0:
            raise ValueError("el periodo debe ser positivo")
        self.period = period
        self._prev_close: float | None = None
        self._seed: list[float] = []

    @property
    def warmup_bars(self) -> int:
        """Velas de histórico que necesita para llegar listo a la primera vela real.

        Tres veces el periodo, no `periodo + 1`. Con el mínimo justo el valor existe, pero
        la media todavía es la siembra simple de los primeros `periodo` movimientos y no
        el suavizado de Wilder: dos series idénticas salvo en su arranque darían valores
        distintos durante decenas de velas. A las tres vueltas el peso de la siembra ha
        decaído a (13/14)**28 ≈ 12 % y la serie ya es la que sería con histórico infinito.

        return : int — velas cerradas del pasado que hay que darle.
        """
        return self.period * 3

    def start_session(self) -> None:
        """Olvida el cierre previo. Ver `Indicator.start_session` para el porqué y la medición.

        return : None
        """
        self._prev_close = None

    def update(self, bar: Bar) -> float | None:
        """Incorpora una vela y recalcula el ATR.

        bar    : Bar — vela recién cerrada.
        return : float | None — ATR en unidades de precio, o None hasta tener datos.
        """
        if self._prev_close is None:
            true_range = bar.high - bar.low
        else:
            true_range = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
        self._prev_close = bar.close

        if self.value is None:
            self._seed.append(true_range)
            if len(self._seed) < self.period:
                return None
            self.value = sum(self._seed) / self.period
            self._seed.clear()
        else:
            self.value = (self.value * (self.period - 1) + true_range) / self.period
        return self.value
