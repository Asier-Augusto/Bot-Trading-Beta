"""Índice de fuerza relativa con el suavizado de Wilder, en versión incremental."""

from __future__ import annotations

from bot.core.registry import register_indicator
from bot.core.types import Bar
from bot.indicators.base import Indicator


@register_indicator("rsi")
class RSI(Indicator):
    """RSI de Wilder.

    Estado que guarda: media de subidas, media de bajadas y cierre previo. Tres
    números, frente a recorrer las últimas catorce velas en cada actualización.
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
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._seed_gains: list[float] = []
        self._seed_losses: list[float] = []

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
        """Incorpora una vela y recalcula el RSI.

        bar    : Bar — vela recién cerrada.
        return : float | None — RSI en [0, 100], o None hasta tener periodo suficiente.
        """
        close = bar.close

        if self._prev_close is None:
            self._prev_close = close
            return None

        change = close - self._prev_close
        self._prev_close = close
        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        if self._avg_gain is None:
            # Fase de siembra: media simple de los primeros `period` movimientos.
            self._seed_gains.append(gain)
            self._seed_losses.append(loss)
            if len(self._seed_gains) < self.period:
                return None
            self._avg_gain = sum(self._seed_gains) / self.period
            self._avg_loss = sum(self._seed_losses) / self.period
            self._seed_gains.clear()
            self._seed_losses.clear()
        else:
            # Suavizado de Wilder.
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period

        if self._avg_loss == 0:
            self.value = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            self.value = 100.0 - 100.0 / (1.0 + rs)
        return self.value
