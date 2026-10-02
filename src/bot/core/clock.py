"""Implementaciones del reloj: una para vivo y otra para backtest."""

from __future__ import annotations

import datetime as dt
import time
import zoneinfo

from bot.contracts.clock import Clock

#: Sesión regular del mercado estadounidense, en hora local de la bolsa.
SESSION_OPEN = dt.time(9, 30)
SESSION_CLOSE = dt.time(16, 0)


def _is_regular_session(
    moment: dt.datetime, tz: zoneinfo.ZoneInfo, calendar=None
) -> bool:
    """Indica si un instante cae dentro de la sesión regular.

    Con calendario, contempla festivos y medias sesiones. Sin él, solo el día de la semana y
    la hora — que es suficiente en backtest, donde los días reproducidos existen por
    construcción, y peligroso en vivo: el bot creería que el mercado abre un 4 de julio.

    moment   : datetime — instante consciente de zona horaria.
    tz       : ZoneInfo — zona horaria de la bolsa.
    calendar : MarketCalendar | None — festivos y horarios. None se comporta como antes.
    return   : bool — True si hay sesión y la hora local está dentro de ella.
    """
    local = moment.astimezone(tz)
    if local.weekday() >= 5:
        return False

    cierre = SESSION_CLOSE
    if calendar is not None:
        if not calendar.is_trading_day(local.date()):
            return False
        cierre = calendar.close_time(local.date())

    return SESSION_OPEN <= local.time() < cierre


class RealClock(Clock):
    """Reloj de pared, para operar en vivo."""

    def __init__(self, timezone: str, calendar=None) -> None:
        """Crea el reloj.

        timezone : str — zona horaria de la bolsa, p. ej. "America/New_York".
        calendar : MarketCalendar | None — festivos y medias sesiones. En vivo debería
                   pasarse siempre: sin él, `is_market_open()` responde que sí un 4 de julio.
        """
        self._tz = zoneinfo.ZoneInfo(timezone)
        self._calendar = calendar

    def now(self) -> dt.datetime:
        """Instante actual del sistema.

        return : datetime — en UTC, consciente de zona.
        """
        return dt.datetime.now(dt.timezone.utc)

    def sleep(self, seconds: float) -> None:
        """Bloquea el hilo el tiempo indicado.

        seconds : float — segundos a esperar.
        return  : None
        """
        time.sleep(seconds)

    def is_market_open(self) -> bool:
        """Indica si el mercado está en sesión regular ahora mismo.

        return : bool
        """
        return _is_regular_session(self.now(), self._tz, self._calendar)


class SimulatedClock(Clock):
    """Reloj que avanza a mano, para backtest y pruebas."""

    def __init__(
        self,
        start: dt.datetime,
        timezone: str,
        naive_is_local: bool = False,
    ) -> None:
        """Crea el reloj en un instante dado.

        start          : datetime — instante inicial.
        timezone       : str      — zona horaria de la bolsa.
        naive_is_local : bool     — si `start` no lleva zona, interpretarlo como hora
                                    local de la bolsa (True) o como UTC (False).
        """
        self._tz = zoneinfo.ZoneInfo(timezone)
        if start.tzinfo is None:
            start = start.replace(tzinfo=self._tz if naive_is_local else dt.timezone.utc)
        self._now = start

    def now(self) -> dt.datetime:
        """Instante simulado actual.

        return : datetime — consciente de zona.
        """
        return self._now

    def sleep(self, seconds: float) -> None:
        """Avanza el reloj sin esperar de verdad.

        seconds : float — segundos que avanza el tiempo simulado.
        return  : None
        """
        self._now += dt.timedelta(seconds=seconds)

    def advance_to(self, ts: dt.datetime) -> None:
        """Sitúa el reloj en un instante posterior al actual.

        ts     : datetime — nuevo instante. Debe ser mayor o igual al actual.
        return : None
        raises : ValueError — si el instante va hacia atrás.
        """
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        if ts < self._now:
            raise ValueError(f"el reloj no puede ir hacia atrás: {ts} < {self._now}")
        self._now = ts

    def is_market_open(self) -> bool:
        """Indica si el instante simulado cae en sesión regular.

        return : bool
        """
        return _is_regular_session(self._now, self._tz)
