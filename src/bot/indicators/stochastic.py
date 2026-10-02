"""Oscilador estocástico %K y %D, en versión incremental.

Mide dónde cierra el precio dentro del rango de las últimas `period` velas: 100 significa
que cierra en el máximo del periodo y 0 que cierra en el mínimo. Moris lo usa como señal de
SALIDA (§7.3): con %K y %D por encima de 80, la posición se cierra a mercado.

**Dos clases y no una con dos valores.** El contrato `Indicator` tiene un solo `value`, y
`ctx.indicator(nombre, marco)` devuelve un `float`. Partirlo en dos instancias mantiene
intacto ese contrato y el acceso por nombre que permite añadir indicadores sin tocar
`Strategy` (§4.1 del diseño). El coste es una cola de `period` cierres duplicada:
despreciable frente a cambiar el contrato por el que pasa todo lo demás.
"""

from __future__ import annotations

from collections import deque

from bot.core.registry import register_indicator
from bot.core.types import Bar
from bot.indicators.base import Indicator

#: Valor con el que se responde cuando el rango del periodo es cero. Un símbolo parado
#: `period` velas seguidas tiene máximo igual a mínimo, y la fórmula dividiría por cero. Ni
#: sobrecomprado ni sobrevendido es la única respuesta honesta, y devolverla evita reventar
#: en mitad de la sesión por un símbolo que dejó de cotizar unos minutos.
_FLAT = 50.0


class _StochasticBase(Indicator):
    """Parte común: la ventana de velas y el %K crudo."""

    def __init__(self, period: int = 14, smooth: int = 3) -> None:
        """Crea el indicador.

        period : int — velas del rango sobre el que se sitúa el cierre.
        smooth : int — velas del promedio que convierte el %K crudo en %K lento.
        raises : ValueError — si algún periodo no es positivo.
        """
        super().__init__()
        if period <= 0 or smooth <= 0:
            raise ValueError("los periodos deben ser positivos")
        self.period = period
        self.smooth = smooth
        self._highs: deque[float] = deque(maxlen=period)
        self._lows: deque[float] = deque(maxlen=period)
        self._raw: deque[float] = deque(maxlen=smooth)

    @property
    def warmup_bars(self) -> int:
        """Velas de histórico que necesita para llegar listo a la primera vela real.

        return : int — las del rango más las de los dos promedios, con margen.
        """
        return self.period + self.smooth * 2

    def _push(self, bar: Bar) -> float | None:
        """Incorpora una vela y devuelve el %K lento, si ya hay datos suficientes.

        bar    : Bar — vela recién cerrada.
        return : float | None — %K lento, o None durante el calentamiento.
        """
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        if len(self._highs) < self.period:
            return None

        maximo, minimo = max(self._highs), min(self._lows)
        rango = maximo - minimo
        crudo = _FLAT if rango <= 0 else 100.0 * (bar.close - minimo) / rango

        self._raw.append(crudo)
        if len(self._raw) < self.smooth:
            return None
        return sum(self._raw) / len(self._raw)


@register_indicator("stoch_k")
class StochasticK(_StochasticBase):
    """%K lento: promedio de `smooth` valores del %K crudo sobre `period` velas."""

    def update(self, bar: Bar) -> float | None:
        """Incorpora una vela y recalcula el %K.

        bar    : Bar — vela recién cerrada.
        return : float | None — %K en [0, 100], o None hasta tener velas suficientes.
        """
        valor = self._push(bar)
        if valor is not None:
            self.value = valor
        return self.value


@register_indicator("stoch_d")
class StochasticD(_StochasticBase):
    """%D: promedio móvil de `signal` valores del %K lento.

    Es la línea lenta. Que Moris exija las DOS por encima de 80 (§7.3) y no solo el %K no es
    redundante: el %K entra y sale del umbral en cada tirón, y con él solo la salida se
    dispararía en cualquier vela fuerte. El %D confirma que la sobrecompra se ha sostenido.
    """

    def __init__(self, period: int = 14, smooth: int = 3, signal: int = 3) -> None:
        """Crea el indicador.

        period : int — velas del rango.
        smooth : int — velas del promedio del %K.
        signal : int — velas del promedio del %D sobre el %K.
        raises : ValueError — si algún periodo no es positivo.
        """
        super().__init__(period, smooth)
        if signal <= 0:
            raise ValueError("los periodos deben ser positivos")
        self.signal = signal
        self._k: deque[float] = deque(maxlen=signal)

    @property
    def warmup_bars(self) -> int:
        """Velas de histórico que necesita para llegar listo.

        return : int — las del rango más las de los dos promedios, con margen.
        """
        return self.period + (self.smooth + self.signal) * 2

    def update(self, bar: Bar) -> float | None:
        """Incorpora una vela y recalcula el %D.

        bar    : Bar — vela recién cerrada.
        return : float | None — %D en [0, 100], o None hasta tener velas suficientes.
        """
        k = self._push(bar)
        if k is None:
            return self.value
        self._k.append(k)
        if len(self._k) < self.signal:
            return self.value
        self.value = sum(self._k) / len(self._k)
        return self.value
