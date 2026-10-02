"""Construcción de velas a partir de ticks.

Las velas las construye el bot y no el bróker, por dos motivos: el proceso es idéntico
en vivo y en backtest — si no lo fuera, los resultados del backtest mentirían — y la
vela se cierra aunque no llegue ningún tick, para que un símbolo poco líquido no
congele sus indicadores.

Esa segunda promesa es del §5.1 del diseño y no es teórica: **ONEQ, la referencia del
Nasdaq, se eligió sabiendo que no cotiza en la mitad de los minutos de la sesión**, y la
medición que justificó esa elección (§7.2) se hizo rellenando los huecos exactamente como
se hace aquí. Sin relleno, la serie de 5' sobre la que se calcula su RSI sale comprimida
y no uniforme: no es la serie que se midió.
"""

from __future__ import annotations

import datetime as dt

from bot.core.types import Bar

#: Marcos temporales admitidos y su duración en segundos.
_TIMEFRAMES = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

#: Tope de velas de relleno que un solo salto de reloj puede producir. Un fin de semana
#: entre dos ticks son 4.320 minutos: fabricar esas velas planas no reconstruiría la
#: serie, la inventaría. Con el tope, un hueco enorme cierra lo que había y sigue.
_MAX_FILL_PERIODS = 1440


class BarAggregator:
    """Agrega ticks en velas de un marco temporal, alineadas al reloj de pared."""

    def __init__(self, symbol: str, timeframe: str) -> None:
        """Crea el agregador.

        symbol    : str — ticker.
        timeframe : str — marco temporal; uno de "1m", "5m", "15m", "1h", "4h", "1d".
        raises    : ValueError — si el marco no está admitido.
        """
        if timeframe not in _TIMEFRAMES:
            raise ValueError(f"marco temporal no admitido: {timeframe}")
        self.symbol = symbol
        self.timeframe = timeframe
        self._seconds = _TIMEFRAMES[timeframe]
        self._period_end: dt.datetime | None = None
        self.current: Bar | None = None
        #: Último instante visto, por tick o por reloj. Es la guarda de monotonía.
        self._last_ts: dt.datetime | None = None
        #: Cierre de la última vela sellada. Es lo que arrastra una vela de relleno.
        self._last_close: float | None = None
        #: Cierre de periodo de la última vela sellada, real o de relleno. Es el punto
        #: desde el que continúa el relleno en la llamada siguiente.
        self._last_sealed_end: dt.datetime | None = None

    def _check_forward(self, ts: dt.datetime) -> None:
        """Rechaza un instante anterior al último visto.

        Hasta ahora la única protección del sistema contra una secuencia fuera de orden
        era que `SimulatedClock.advance_to` lanzaba ante un retroceso. `RealClock` no
        tiene `advance_to`, así que en vivo esa protección desaparecía y un tick tardío
        habría corrompido en silencio la vela en curso.

        Instantes **iguales** se admiten: el feed de reproducción desplaza los cuatro
        ticks de una vela por milisegundos, pero dos símbolos distintos pueden coincidir
        y eso no es retroceder.

        ts     : datetime — instante entrante.
        return : None
        raises : ValueError — si el instante va hacia atrás.
        """
        if self._last_ts is not None and ts < self._last_ts:
            raise ValueError(
                f"el agregador de {self.symbol}/{self.timeframe} no puede ir hacia atrás: "
                f"{ts} < {self._last_ts}"
            )
        self._last_ts = ts

    def _period_end_for(self, ts: dt.datetime) -> dt.datetime:
        """Calcula el cierre del periodo al que pertenece un instante.

        ts     : datetime — instante del tick.
        return : datetime — límite superior del periodo, alineado al reloj de pared.
        """
        epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        elapsed = int((ts - epoch).total_seconds())
        end = (elapsed // self._seconds + 1) * self._seconds
        return epoch + dt.timedelta(seconds=end)

    def _close(self) -> Bar | None:
        """Cierra la vela en curso y limpia el estado.

        return : Bar | None — la vela cerrada, o None si no había ninguna en curso.
        """
        closed = self.current
        self.current = None
        self._period_end = None
        return closed

    def on_tick(self, ts: dt.datetime, price: float, volume: int) -> Bar | None:
        """Incorpora un tick, cerrando la vela anterior si empieza un periodo nuevo.

        ts     : datetime — instante del tick, consciente de zona.
        price  : float    — precio negociado.
        volume : int      — acciones negociadas en el tick.
        return : Bar | None — la vela recién cerrada, o None si sigue el mismo periodo.
        raises : ValueError — si el instante va hacia atrás.
        """
        self._check_forward(ts)
        closed: Bar | None = None

        if self._period_end is not None and ts >= self._period_end:
            closed = self._finish_at(self._period_end)

        if self.current is None:
            self._period_end = self._period_end_for(ts)
            self.current = Bar(
                symbol=self.symbol,
                timeframe=self.timeframe,
                ts=self._period_end,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=volume,
            )
            return closed

        bar = self.current
        self.current = Bar(
            symbol=bar.symbol,
            timeframe=bar.timeframe,
            ts=bar.ts,
            open=bar.open,
            high=max(bar.high, price),
            low=min(bar.low, price),
            close=price,
            volume=bar.volume + volume,
        )
        return closed

    def _finish_at(self, period_end: dt.datetime) -> Bar | None:
        """Sella la vela en curso con el instante de cierre de su periodo.

        period_end : datetime — cierre del periodo.
        return     : Bar | None — la vela sellada.
        """
        if self.current is None:
            return None
        bar = self.current
        sealed = Bar(
            symbol=bar.symbol,
            timeframe=bar.timeframe,
            ts=period_end,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        self._close()
        self._last_close = sealed.close
        self._last_sealed_end = sealed.ts
        return sealed

    def _flat_bar(self, period_end: dt.datetime) -> Bar:
        """Fabrica la vela de un periodo en el que no se negoció nada.

        Arrastra el cierre anterior en los cuatro precios y lleva volumen cero. Un
        consumidor puede distinguirla de una vela real justamente por eso: recorrido cero
        **y** volumen cero.

        period_end : datetime — cierre del periodo vacío.
        return     : Bar      — vela plana al último cierre conocido.
        """
        precio = self._last_close
        self._last_sealed_end = period_end
        return Bar(
            symbol=self.symbol,
            timeframe=self.timeframe,
            ts=period_end,
            open=precio,
            high=precio,
            low=precio,
            close=precio,
            volume=0,
        )

    def on_clock(self, ts: dt.datetime) -> list[Bar]:
        """Cierra por paso del tiempo todas las velas que ya deberían haber cerrado.

        Devuelve una LISTA y no una sola vela porque un símbolo poco líquido puede pasar
        varios periodos sin una sola operación, y el §5.1 promete una vela por periodo.
        Con la firma anterior (`Bar | None`) esas velas de relleno no existían: el motor
        llamaba a `on_clock` una única vez por ejecución y un periodo sin operaciones no
        producía vela ninguna.

        ts     : datetime — instante actual del reloj inyectado.
        return : list[Bar] — velas cerradas en orden cronológico; vacía si no tocaba
                             cerrar nada o si nunca ha habido una vela que arrastrar.
        raises : ValueError — si el instante va hacia atrás.
        """
        self._check_forward(ts)
        cerradas: list[Bar] = []

        # 1) La vela en curso, si su periodo ya terminó.
        if self._period_end is not None and ts >= self._period_end:
            sellada = self._finish_at(self._period_end)
            if sellada is not None:
                cerradas.append(sellada)

        # 2) Los periodos vacíos que van desde ahí hasta el instante actual.
        if self._last_close is None:
            # Nunca ha cerrado una vela: no hay cierre anterior que arrastrar, e
            # inventar un precio sería peor que no producir nada.
            return cerradas

        # El relleno arranca donde acabó la última vela sellada —sea real o de relleno—
        # y llega hasta el último periodo COMPLETO antes de `ts`. El periodo en el que
        # cae `ts` está todavía en curso: cerrarlo aquí adelantaría una vela.
        siguiente = self._last_sealed_end
        objetivo = self._period_end_for(ts) - dt.timedelta(seconds=self._seconds)
        rellenos = 0
        while siguiente < objetivo and rellenos < _MAX_FILL_PERIODS:
            siguiente = siguiente + dt.timedelta(seconds=self._seconds)
            cerradas.append(self._flat_bar(siguiente))
            rellenos += 1

        return cerradas
