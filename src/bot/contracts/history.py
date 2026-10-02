"""Contrato de la fuente de históricos: velas ya cerradas del pasado.

Existe porque dos partes del sistema necesitan lo mismo y ninguna debe saber de dónde
sale:

- **El calentamiento de indicadores.** A las 09:30, en vivo, no hay ni una vela de 5' de
  hoy; un RSI-14 sobre ellas no vale nada hasta las 10:45, y del ATR de 5' dependen el
  stop y la tolerancia de las zonas de soporte. Sin calentar, el bot está ciego durante
  los primeros 75 minutos de cada sesión, que es la franja de más volatilidad.
- **Los soportes estructurales (§6).** Se calculan una vez al día sobre velas de 1d, 4h,
  1h y 15m, con lookbacks de hasta un año.

En backtest lo implementa `CsvHistoricalSource`; en vivo lo implementará el adaptador de
IBKR sobre `reqHistoricalData`. Ni el motor ni la estrategia distinguen cuál es.

**Convenio temporal, que es la parte fácil de equivocar:** `Bar.ts` es el instante de
CIERRE de la vela, no el de apertura. Cada implementación se encarga de cumplirlo con lo
que le dé su fuente — IBKR, por ejemplo, devuelve el instante de apertura. Equivocarse
aquí desplaza el calentamiento un periodo entero sin que nada se queje.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod

from bot.core.types import Bar


class HistoricalSource(ABC):
    """Entrega velas cerradas del pasado, por símbolo y marco temporal."""

    @abstractmethod
    def bars(self, symbol: str, timeframe: str, end: dt.datetime, count: int) -> list[Bar]:
        """Últimas `count` velas cerradas de un símbolo y marco, con cierre en `end` o antes.

        El corte por `end` no es una comodidad: es lo que impide que el calentamiento se
        alimente de velas posteriores al instante que se está simulando, que sería mirar
        la respuesta antes del examen y haría que el backtest mintiera a favor.

        symbol    : str      — ticker.
        timeframe : str      — "1m", "5m", "15m", "1h", "4h" o "1d".
        end       : datetime — corte superior, INCLUIDO. Ninguna vela devuelta cierra
                               después de este instante.
        count     : int      — cuántas velas se piden, contando hacia atrás desde `end`.
        return    : list[Bar] — en orden cronológico ascendente, con `ts` en el instante
                                de CIERRE de cada vela. Puede tener menos de `count` si el
                                histórico no llega tan atrás —un histórico corto es un
                                hecho, no un error— y nunca más.
        """
