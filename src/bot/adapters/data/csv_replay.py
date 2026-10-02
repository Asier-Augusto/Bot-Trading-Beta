"""Reproducción de históricos desde CSV para el backtest.

Cada vela del fichero se descompone en cuatro ticks (apertura, máximo, mínimo, cierre)
que el agregador del bot vuelve a componer. Podría parecer un rodeo, pero es
deliberado: si el backtest recibiera velas ya hechas, estaría ejercitando un camino de
datos distinto del que se usa con dinero real, y los resultados no valdrían para nada.

Ordenar solo las FILAS del CSV por (instante, símbolo) no basta: al expandir cada vela
en cuatro ticks con desplazamientos de milisegundos, dos símbolos con vela en el mismo
minuto intercalan mal si se recorren fila a fila (los cuatro ticks de uno seguidos de
los del otro retroceden en el tiempo). Por eso aquí se expanden TODOS los ticks
primero y se ordenan globalmente por (instante, símbolo) antes de emitir ninguno.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Iterator

from bot.contracts.feed import MarketDataFeed
from bot.core.registry import register_feed

#: Tope de espera entre dos ticks al reproducir a velocidad humana. Entre el cierre y la
#: apertura hay diecisiete horas; a velocidad 60 serían diecisiete minutos mirando una
#: pantalla quieta.
MAX_REPLAY_WAIT_SECONDS = 2.0


@register_feed("csv_replay")
class CsvReplayFeed(MarketDataFeed):
    """Feed que lee velas de un CSV y las emite como ticks en orden cronológico."""

    def __init__(self, path: str | Path, timeframe: str = "1m",
                 speed: float | None = None, sleep=None) -> None:
        """Crea el feed.

        path      : str | Path — fichero CSV con cabecera
                                 `timestamp,symbol,open,high,low,close,volume`.
        timeframe : str        — marco de las velas del fichero. Informativo.
        speed : float | None — reproduce a esta velocidad respecto al mercado: 60 hace que
                un minuto de sesión pase en un segundo. None va de corrido, que es lo que
                quiere el barrido —2.011 sesiones no se miran, se cuentan— y lo que se ha
                hecho siempre. Está para poder MIRAR una sesión concreta con el panel
                delante, que es la deuda D-B5.
        sleep : callable | None — cómo esperar; None usa `time.sleep`. Está para poder
                probar el ritmo sin esperar de verdad.
        return    : None
        """
        self.path = Path(path)
        self.speed = speed
        self._sleep = sleep
        self.timeframe = timeframe
        self._rows: list[tuple[str, dt.datetime, float, float, float, float, int]] = []
        self._symbols: set[str] | None = None

    def connect(self) -> None:
        """Carga el fichero completo en memoria.

        return : None
        raises : FileNotFoundError — si el fichero no existe.
        """
        if not self.path.exists():
            raise FileNotFoundError(f"no existe el fichero de históricos: {self.path}")

        rows = []
        with self.path.open(newline="", encoding="utf-8") as handle:
            for record in csv.DictReader(handle):
                ts = dt.datetime.fromisoformat(record["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
                rows.append(
                    (
                        record["symbol"],
                        ts,
                        float(record["open"]),
                        float(record["high"]),
                        float(record["low"]),
                        float(record["close"]),
                        int(float(record["volume"])),
                    )
                )
        self._rows = rows

    def subscribe(self, symbols: list[str]) -> None:
        """Restringe la emisión a los símbolos indicados.

        symbols : list[str] — tickers a emitir. Sin llamar a esto se emite todo.
        return  : None
        """
        self._symbols = set(symbols)

    def stream(self) -> Iterator[tuple[str, dt.datetime, float, int]]:
        """Produce los ticks en orden cronológico global.

        Cada vela se descompone en cuatro ticks (apertura, máximo, mínimo, cierre)
        con desplazamientos de 0 a 3 milisegundos. Con un solo símbolo bastaría con
        recorrer las velas en orden y expandir cada una sobre la marcha, pero con
        varios símbolos eso puede producir una secuencia que retrocede en el tiempo:
        si TSLA y NVDA tienen vela en el mismo minuto, expandir fila a fila emitiría
        los cuatro ticks de una (`.000` a `.003`) y luego el primero de la otra
        (`.000`), que va hacia atrás. Por eso aquí se generan TODOS los ticks primero
        y se ordenan globalmente por (instante, símbolo) antes de emitir ninguno; el
        fichero ya está entero en memoria, así que no cambia el coste.

        El volumen de la vela se reparte entre los cuatro ticks sin inventar ni perder
        acciones: el resto de la división entera va al último.

        return : Iterator[tuple] — (símbolo, instante, precio, volumen).
        """
        ticks: list[tuple[str, dt.datetime, float, int]] = []

        for symbol, ts, open_, high, low, close, volume in self._rows:
            if self._symbols is not None and symbol not in self._symbols:
                continue

            share = volume // 4
            remainder = volume - share * 3
            prices = (open_, high, low, close)
            volumes = (share, share, share, remainder)

            for offset, (price, chunk) in enumerate(zip(prices, volumes, strict=True)):
                ticks.append((symbol, ts + dt.timedelta(milliseconds=offset), price, chunk))

        ticks.sort(key=lambda tick: (tick[1], tick[0]))
        if self.speed is None:
            yield from ticks
            return

        # Reproducción a velocidad humana (D-B5). El hueco entre dos ticks se espera a
        # escala, pero con un tope: entre el cierre y la apertura hay diecisiete horas, y
        # esperarlas a escala haría que reproducir un día tardara más que el día.
        import time as _time

        esperar = self._sleep or _time.sleep
        anterior = None
        for tick in ticks:
            if anterior is not None:
                hueco = (tick[1] - anterior).total_seconds() / self.speed
                if hueco > 0:
                    esperar(min(hueco, MAX_REPLAY_WAIT_SECONDS))
            anterior = tick[1]
            yield tick

    def disconnect(self) -> None:
        """Libera las filas cargadas.

        return : None
        """
        self._rows = []
