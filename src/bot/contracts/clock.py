"""Contrato del reloj. Ningún componente consulta la hora del sistema directamente."""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod


class Clock(ABC):
    """Fuente de tiempo del sistema.

    Existe para que la misma estrategia corra en vivo y en backtest sin cambios: en
    vivo el tiempo pasa solo, en backtest lo hace avanzar el reproductor de históricos.
    """

    @abstractmethod
    def now(self) -> dt.datetime:
        """Instante actual.

        return : datetime — consciente de zona horaria, nunca ingenuo.
        """

    @abstractmethod
    def sleep(self, seconds: float) -> None:
        """Espera el tiempo indicado.

        seconds : float — segundos a esperar. En backtest no espera: salta.
        return  : None
        """

    @abstractmethod
    def is_market_open(self) -> bool:
        """Indica si el mercado está en sesión regular.

        return : bool — True entre la apertura y el cierre de un día laborable.
        """
