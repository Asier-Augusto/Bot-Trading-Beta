"""Precio medio ponderado por volumen, anclado a la sesión.

Sirve a una pregunta que la estrategia no sabe responder hoy: **cuánto recorrido tiene por
delante una operación, en el instante de entrar**. MEDIDO el 2026-09-02 sobre 2.011 sesiones:
las operaciones que llegaron a su objetivo solo llegaron a ofrecer 0,717R, y para no perder
hacían falta 0,74R. La salida por estocástico no puede arreglar eso porque al entrar nadie
sabe dónde va a saltar; la distancia al VWAP sí se puede leer antes de comprometerse.

Dos decisiones y su porqué:

1. **Precio típico** `(máximo + mínimo + cierre) / 3` y no el cierre. El VWAP pondera dónde se
   negoció de verdad, y una vela con cola larga no se negoció en su cierre.
2. **Anclado a la sesión.** El VWAP de hoy no sabe nada de ayer: arrastrar la sesión anterior
   lo anclaría a un precio que ya no significa nada. Por eso `warmup_bars` es cero — pedirle
   velas del histórico sería contradecir su propia definición.
"""

from __future__ import annotations

from bot.core.registry import register_indicator
from bot.core.types import Bar
from bot.indicators.base import Indicator


@register_indicator("vwap")
class VWAP(Indicator):
    """VWAP de la sesión en curso.

    Estado que guarda: dos acumuladores. Ni ventana ni historia.
    """

    def __init__(self) -> None:
        """Crea el indicador."""
        super().__init__()
        self._precio_por_volumen = 0.0
        self._volumen = 0

    @property
    def warmup_bars(self) -> int:
        """Velas de histórico que necesita.

        return : int — cero: está anclado a la sesión.
        """
        return 0

    def update(self, bar: Bar) -> float | None:
        """Incorpora una vela cerrada.

        Una vela **sin volumen no cuenta**: no dice a qué precio se negoció, y contarla como
        un dato más arrastraría el VWAP hacia un precio que nadie pagó.

        bar    : Bar — vela recién cerrada.
        return : float | None — el VWAP de la sesión, o None si aún no hay volumen.
        """
        if bar.volume <= 0:
            return self.value

        tipico = (bar.high + bar.low + bar.close) / 3.0
        self._precio_por_volumen += tipico * bar.volume
        self._volumen += bar.volume
        self.value = self._precio_por_volumen / self._volumen
        return self.value

    def start_session(self) -> None:
        """Empieza una sesión nueva, olvidando la anterior.

        return : None
        """
        self._precio_por_volumen = 0.0
        self._volumen = 0
        self.value = None
