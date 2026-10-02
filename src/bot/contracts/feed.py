"""Contrato de la fuente de datos de mercado."""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from typing import Iterator


class MarketDataFeed(ABC):
    """Entrega ticks al motor.

    Separado del bróker a propósito: en backtest esto lee un CSV mientras el bróker es
    simulado; en paper ambos los sirve IBKR sobre una única conexión.
    """

    @abstractmethod
    def connect(self) -> None:
        """Establece la conexión con la fuente.

        return : None
        """

    @abstractmethod
    def subscribe(self, symbols: list[str]) -> None:
        """Suscribe los símbolos a vigilar.

        symbols : list[str] — tickers.
        return  : None
        """

    @abstractmethod
    def stream(self) -> Iterator[tuple[str, dt.datetime, float | None, int]]:
        """Produce ticks en orden cronológico.

        **Un tick con `price` a `None` es un LATIDO**: dice qué hora es y no dice nada del
        precio. El motor lo usa para cerrar por reloj las velas de todos los símbolos, sin
        incorporar nada a ninguna.

        Existe porque en vivo el feed no se agota nunca, y un símbolo que no cotiza no
        produce ticks: sin latido, sus agregadores no avanzan y sus indicadores se congelan
        el resto de la sesión. No es un caso raro — medido contra IBKR, ONEQ no tiene
        ninguna operación en el 46,6 % de los minutos, y ONEQ es la referencia del Nasdaq de
        Moris, así que la condición `rsi_ndx` dejaría de poder cumplirse.

        Los feeds de fichero NO los producen: el backtest se agota y el motor cierra lo que
        quede al terminar.

        return : Iterator[tuple] — (símbolo, instante, precio, volumen). Con `price` a None,
                 el símbolo y el volumen se ignoran.
        """

    @abstractmethod
    def disconnect(self) -> None:
        """Cierra la conexión.

        return : None
        """
