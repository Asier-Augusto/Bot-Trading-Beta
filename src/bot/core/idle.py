"""Acumulación de las evaluaciones que no llegan al nivel mínimo de registro.

Casi todas las velas de casi todas las sesiones son IDLE: escribir una línea por cada
una llenaría el disco de ruido y enterraría lo que importa. Pero borrarlas del todo
perdería la respuesta a la pregunta que el §3.3 del diseño quiere poder hacer —*¿qué
condición fue la que más veces bloqueó hoy?*—, que es exactamente lo que dice si el bot
no opera porque el mercado no da señales o porque un umbral está mal puesto.

La salida es un `IdleSummary` por símbolo cada N minutos, con cuántas velas, la media y
el máximo de la puntuación, y el recuento de bloqueos por condición.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import TYPE_CHECKING

from bot.core.events import IdleSummary

if TYPE_CHECKING:
    from bot.core.scoring import SignalScore


class IdleAccumulator:
    """Contadores de IDLE por símbolo, con volcado periódico."""

    def __init__(self, window_minutes: int = 60) -> None:
        """Crea el acumulador.

        window_minutes : int — cada cuántos minutos de tiempo simulado se vuelca el
                               resumen. La ventana se mide con el reloj del motor, no con
                               el del sistema: en backtest una sesión entera pasa en
                               milisegundos y con la hora de pared nunca se volcaría nada.
        """
        self.window_minutes = window_minutes
        self._counters: dict[str, dict] = defaultdict(
            lambda: {"bars": 0, "blocked_by": defaultdict(int), "sum": 0.0, "max": 0.0}
        )
        self._window_start: dt.datetime | None = None

    @property
    def window_start(self) -> dt.datetime | None:
        """Inicio de la ventana en curso.

        return : datetime | None — None si no se ha acumulado nada desde el último volcado.
        """
        return self._window_start

    def pending(self) -> bool:
        """Indica si hay algo acumulado sin volcar.

        return : bool — True si al menos una evaluación entró desde el último volcado.
        """
        return self._window_start is not None

    def symbols(self) -> set[str]:
        """Símbolos con velas acumuladas en la ventana en curso.

        Es una vista de solo lectura para que una prueba pueda comprobar a QUIÉN se le
        atribuyó una evaluación sin abrir los contadores.

        return : set[str] — tickers con al menos una vela IDLE acumulada.
        """
        return {s for s, c in self._counters.items() if c["bars"] > 0}

    def add(self, symbol: str, ts: dt.datetime, score: "SignalScore") -> None:
        """Suma una evaluación IDLE a los contadores de su símbolo.

        symbol : str         — ticker evaluado.
        ts     : datetime    — instante de la vela.
        score  : SignalScore — resultado de la puntuación.
        return : None
        """
        if self._window_start is None:
            self._window_start = ts

        counter = self._counters[symbol]
        counter["bars"] += 1
        counter["sum"] += score.total
        counter["max"] = max(counter["max"], score.total)
        for name in score.blocking:
            counter["blocked_by"][name] += 1

    def due(self, ts: dt.datetime) -> bool:
        """Indica si toca volcar el resumen.

        ts     : datetime — instante de la vela recién evaluada.
        return : bool
        """
        if self._window_start is None:
            return False
        elapsed = (ts - self._window_start).total_seconds() / 60
        return elapsed >= self.window_minutes

    def flush(self, ts: dt.datetime, config_hash: str = "") -> list[IdleSummary]:
        """Produce los resúmenes acumulados y reinicia la ventana.

        Devuelve los eventos en vez de publicarlos: quien produce hechos en este sistema
        no conoce el bus. Es el mismo patrón que el bróker y que la fuente de soportes.

        ts          : datetime — instante del volcado, que fecha los eventos.
        config_hash : str      — resumen de la configuración activa.
        return      : list[IdleSummary] — uno por símbolo con velas acumuladas; vacía si
                                          no había nada. Un símbolo sin velas no produce
                                          resumen: una línea de ceros no informa de nada.
        """
        resumenes = [
            IdleSummary(
                ts=ts,
                symbol=symbol,
                window_start=self._window_start,
                bars=counter["bars"],
                blocked_by=dict(counter["blocked_by"]),
                mean_total=counter["sum"] / counter["bars"],
                max_total=counter["max"],
                config_hash=config_hash,
            )
            for symbol, counter in self._counters.items()
            if counter["bars"] > 0
        ]
        self._counters.clear()
        self._window_start = ts
        return resumenes
