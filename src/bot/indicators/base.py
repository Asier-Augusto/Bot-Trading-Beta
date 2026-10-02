"""Base común de los indicadores incrementales.

Todos se actualizan en O(1) por vela guardando dos o tres números de estado. En el
camino crítico solo hay `float`: nada de `pandas`, que se reserva para los bordes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from bot.core.types import Bar


class Indicator(ABC):
    """Indicador que se actualiza vela a vela."""

    def __init__(self) -> None:
        self.value: float | None = None

    @property
    def ready(self) -> bool:
        """Indica si el indicador ya tiene un valor utilizable.

        return : bool — True cuando ha visto velas suficientes.
        """
        return self.value is not None

    @property
    def warmup_bars(self) -> int:
        """Velas de histórico que necesita para llegar listo a la primera vela real.

        El conjunto de indicadores se las pide a la fuente de históricos antes del primer
        tick de la sesión. Cero significa "no necesita calentamiento".

        return : int — número de velas cerradas del pasado que hay que darle.
        """
        return 0

    def start_session(self) -> None:
        """Olvida el cierre previo, para que el primer cambio del día no sea el hueco.

        Se invoca justo después del calentamiento, entre las velas del histórico y las de
        la sesión que empieza. Sin esto, el salto entre el cierre de ayer y la apertura de
        hoy entra en el indicador como un movimiento intradía más — y no lo es.

        MEDIDO sobre el fixture dorado, RSI-14 de Wilder en velas de 5' de TSLA,
        comparando incluir el hueco frente a descartarlo, en las 30 primeras velas:

            hueco   dif. máxima   dif. media   velas que difieren > 1,5 puntos
            +0,5 %     36,4          10,5        25 de 30
            +1,0 %     49,1          17,5        30 de 30
            +2,0 %     59,0          26,2        30 de 30
            -2,0 %     22,8           6,6        26 de 30

        Un hueco del 1 % —un día cualquiera en TSLA— deja el RSI de 5' en 78 al abrir
        cuando el movimiento intradía es neutro. Con el umbral de Moris en 35, eso
        significa que la condición `rsi_5m` estaría dominada por el hueco durante las dos
        primeras horas y media de sesión, que es justo la ventana de entrada. La
        distorsión no es un matiz: es mayor que el rango entero de la decisión.

        El coste de descartarlo es que la primera vela de la sesión no actualiza el
        indicador; el valor calentado se conserva intacto hasta la segunda.

        Los indicadores que no guarden cierre previo no tienen nada que hacer aquí, y por
        eso el defecto es no hacer nada.

        return : None
        """

    @abstractmethod
    def update(self, bar: Bar) -> float | None:
        """Incorpora una vela cerrada y devuelve el valor actualizado.

        bar    : Bar — vela recién cerrada.
        return : float | None — nuevo valor, o None si aún no hay datos bastantes.
        """
