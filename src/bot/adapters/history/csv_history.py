"""Históricos multimarco desde un fichero CSV.

Es la implementación de backtest de `HistoricalSource`. El fichero lleva una columna
`timeframe`, así que un mismo CSV puede traer un año de velas diarias, tres meses de 4 h y
el día anterior en velas de 1' sin que ocupe nada: los soportes estructurales no necesitan
un año de velas de un minuto, necesitan un año de velas diarias.

**Dos ficheros con dos papeles.** Este no es el CSV del feed (`csv_replay`):

|            | fichero del feed          | fichero de históricos          |
|------------|---------------------------|--------------------------------|
| cabecera   | sin `timeframe`           | con `timeframe`                |
| `timestamp`| INICIO del minuto         | CIERRE de la vela              |
| para qué   | producir ticks de la sesión | responder preguntas del pasado |

Confundirlos desplaza todo un periodo. Por eso la cabecera de uno no vale para el otro y
este lee `timeframe` como obligatoria.

**Marcos ausentes.** Si se pide un marco que el fichero no trae, se agrega desde el más
fino disponible usando `BarAggregator` — el mismo código que construye las velas en vivo.
Usar otra agregación aquí haría que la serie del calentamiento difiriera de la de la
sesión, y esa diferencia se colaría en el RSI sin que nada la denunciara.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

from bot.contracts.history import HistoricalSource
from bot.core.registry import register_history
from bot.core.types import Bar
from bot.indicators.aggregator import BarAggregator

#: Marcos ordenados de más fino a más grueso. Es el orden en que se busca un candidato
#: desde el que agregar cuando el fichero no trae el marco pedido.
_FINENESS = ("1m", "5m", "15m", "1h", "4h", "1d")


#: Desplazamiento de los cuatro ticks con los que se descompone una vela fina. Son
#: negativos respecto a su cierre porque la vela cubre el intervalo que TERMINA en su
#: `ts`: un tick situado en el `ts` exacto caería ya en el periodo siguiente.
_TICK_OFFSETS = (-4, -3, -2, -1)


def _ticks_de(bar: Bar) -> list[tuple[dt.datetime, float, int]]:
    """Descompone una vela en los cuatro ticks que la reconstruyen.

    bar    : Bar — vela fina del fichero.
    return : list[tuple] — (instante, precio, volumen), en orden
             apertura-máximo-mínimo-cierre. El volumen entero va con el primero para no
             contarlo cuatro veces.
    """
    precios = (bar.open, bar.high, bar.low, bar.close)
    volumenes = (bar.volume, 0, 0, 0)
    return [
        (bar.ts + dt.timedelta(microseconds=desplazamiento), precio, volumen)
        for desplazamiento, precio, volumen in zip(_TICK_OFFSETS, precios, volumenes)
    ]


@register_history("csv")
class CsvHistoricalSource(HistoricalSource):
    """Fuente de históricos que lee un CSV multimarco y agrega lo que falte."""

    def __init__(self, path: str | Path) -> None:
        """Crea la fuente.

        No lee nada todavía: el fichero se carga en la primera consulta. Así, un perfil
        que declare un histórico que no se llega a usar no paga su lectura.

        path : str | Path — CSV con cabecera
                            `timestamp,symbol,timeframe,open,high,low,close,volume`,
                            donde `timestamp` es el CIERRE de la vela.
        """
        self.path = Path(path)
        #: (símbolo, marco) -> velas ordenadas por cierre. Se rellena de forma perezosa,
        #: y lo que se agrega se guarda aquí también: pedir 1d, 4h, 1h y 15m de dos
        #: símbolos son ocho consultas, y sin caché serían ocho lecturas del fichero.
        self._series: dict[tuple[str, str], list[Bar]] = {}
        self._loaded = False

    def _load(self) -> None:
        """Carga el fichero completo en memoria, indexado por símbolo y marco.

        return : None
        raises : FileNotFoundError — si el fichero no existe.
                 KeyError          — si falta la columna `timeframe`, que es lo que
                                     distingue este fichero del CSV del feed.
        """
        if self._loaded:
            return
        if not self.path.exists():
            raise FileNotFoundError(f"no existe el fichero de históricos: {self.path}")

        with self.path.open(newline="", encoding="utf-8") as handle:
            lector = csv.DictReader(handle)
            if lector.fieldnames is None or "timeframe" not in lector.fieldnames:
                raise KeyError(
                    f"{self.path} no tiene columna 'timeframe'. ¿Es el CSV del feed? "
                    f"Ese lleva el instante de APERTURA del minuto y este el de CIERRE "
                    f"de la vela: no son intercambiables."
                )
            for record in lector:
                ts = dt.datetime.fromisoformat(record["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
                clave = (record["symbol"], record["timeframe"])
                self._series.setdefault(clave, []).append(
                    Bar(
                        symbol=record["symbol"],
                        timeframe=record["timeframe"],
                        ts=ts,
                        open=float(record["open"]),
                        high=float(record["high"]),
                        low=float(record["low"]),
                        close=float(record["close"]),
                        volume=int(float(record["volume"])),
                    )
                )

        for serie in self._series.values():
            serie.sort(key=lambda bar: bar.ts)
        self._loaded = True

    def _finer_available(self, symbol: str, timeframe: str) -> str | None:
        """Marco más fino disponible desde el que se podría agregar el pedido.

        symbol    : str — ticker.
        timeframe : str — marco pedido.
        return    : str | None — nombre del marco candidato, o None si no hay ninguno
                                 más fino cargado para ese símbolo.
        """
        if timeframe not in _FINENESS:
            return None
        objetivo = _FINENESS.index(timeframe)
        for candidato in _FINENESS[:objetivo]:
            if (symbol, candidato) in self._series:
                return candidato
        return None

    def _aggregate(self, symbol: str, timeframe: str) -> list[Bar]:
        """Construye una serie agregando desde el marco más fino disponible.

        Se usa el mismo `BarAggregator` que en vivo, y se le entregan las velas finas
        como si fueran ticks. Los cuatro precios se pasan en orden
        apertura-máximo-mínimo-cierre para que el máximo y el mínimo de la vela gruesa
        salgan bien; el volumen entero va con el primero de los cuatro para no contarlo
        cuatro veces.

        **Los ticks se sitúan justo ANTES del cierre de la vela fina, no en él.** Una vela
        de 1' que cierra a las 10:05 cubre el intervalo (10:04, 10:05] y pertenece al
        bloque de 5' que termina a las 10:05. Entregarla en el instante 10:05 exacto la
        empujaba al bloque siguiente, y con ello el máximo y el mínimo de cada vela gruesa
        salían corridos un periodo — un error que no revienta nada y falsea toda la serie.

        symbol    : str — ticker.
        timeframe : str — marco a construir.
        return    : list[Bar] — la serie agregada, en orden cronológico. Vacía si no hay
                                ningún marco más fino desde el que construirla.
        """
        origen = self._finer_available(symbol, timeframe)
        if origen is None:
            return []

        agregador = BarAggregator(symbol, timeframe)
        construidas: list[Bar] = []
        for fina in self._series[(symbol, origen)]:
            for instante, precio, volumen in _ticks_de(fina):
                cerrada = agregador.on_tick(instante, precio, volumen)
                if cerrada is not None:
                    construidas.append(cerrada)

        # La última vela gruesa queda en curso: se sella con su propio `period_end`, igual
        # que hace el motor al vaciar los agregadores al final de una sesión.
        if agregador.current is not None:
            despues = agregador.current.ts + dt.timedelta(microseconds=1)
            construidas.extend(agregador.on_clock(despues))

        return construidas

    def _serie(self, symbol: str, timeframe: str) -> list[Bar]:
        """Serie de un símbolo y marco, cargada del fichero o agregada al vuelo.

        symbol    : str — ticker.
        timeframe : str — marco.
        return    : list[Bar] — en orden cronológico; vacía si no se puede construir.
        """
        self._load()
        clave = (symbol, timeframe)
        if clave not in self._series:
            self._series[clave] = self._aggregate(symbol, timeframe)
        return self._series[clave]

    def bars(self, symbol: str, timeframe: str, end: dt.datetime, count: int) -> list[Bar]:
        """Últimas `count` velas cerradas de un símbolo y marco, con cierre en `end` o antes.

        symbol    : str      — ticker.
        timeframe : str      — marco temporal.
        end       : datetime — corte superior, incluido.
        count     : int      — cuántas velas se piden hacia atrás desde `end`.
        return    : list[Bar] — orden cronológico ascendente; puede tener menos de
                                `count`, nunca más.
        """
        if count <= 0:
            return []
        serie = self._serie(symbol, timeframe)
        hasta_el_corte = [bar for bar in serie if bar.ts <= end]
        return hasta_el_corte[-count:]
