"""Calendario del mercado: qué días hay sesión y hasta qué hora.

`RealClock` miraba el día de la semana y la hora, y nada más. En vivo eso significa que el
bot creería que el mercado está abierto un 4 de julio: se pondría a esperar ticks que no
llegan, calentaría indicadores con el histórico de ayer y llegaría al cierre forzado de las
15:45 sin haber visto un solo precio. Y en una media sesión sería peor: esperaría tres horas
de datos inexistentes y el cierre forzado no encontraría mercado donde vender.

**La tabla se escribe a mano y hay que revisarla cada año.** No se añade una dependencia
(`pandas_market_calendars` o similar) por diez fechas anuales: la lista cabe de un vistazo, se
audita en un minuto y no puede desactualizarse en silencio por una actualización de
biblioteca. Lo que sí hace el calendario es **fallar ruidosamente** al preguntarle por un año
que no cubre, en vez de responder que sí es día de mercado — porque esa respuesta, la
peligrosa, es justo la que daría un calendario incompleto que se calla.

Fuente: calendario oficial de festivos y horarios de la NYSE
(https://www.nyse.com/markets/hours-calendars). Verificado el 2026-08-26.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

#: Cierre de una sesión regular, hora de Nueva York.
REGULAR_CLOSE = dt.time(16, 0)
#: Cierre de una media sesión.
HALF_DAY_CLOSE = dt.time(13, 0)

#: Días sin sesión, por año. Todos comprobados como laborables: un festivo que cayera en fin
#: de semana se observa otro día y es ese otro el que va aquí.
US_HOLIDAYS: dict[int, tuple[dt.date, ...]] = {
    2026: (
        dt.date(2026, 1, 1),    # Año Nuevo
        dt.date(2026, 1, 19),   # Martin Luther King
        dt.date(2026, 2, 16),   # Día de los Presidentes
        dt.date(2026, 4, 3),    # Viernes Santo
        dt.date(2026, 5, 25),   # Memorial Day
        dt.date(2026, 6, 19),   # Juneteenth
        dt.date(2026, 7, 3),    # Independencia, observado (el 4 cae en sábado)
        dt.date(2026, 9, 7),    # Día del Trabajo
        dt.date(2026, 11, 26),  # Acción de Gracias
        dt.date(2026, 12, 25),  # Navidad
    ),
    2027: (
        dt.date(2027, 1, 1),    # Año Nuevo
        dt.date(2027, 1, 18),   # Martin Luther King
        dt.date(2027, 2, 15),   # Día de los Presidentes
        dt.date(2027, 3, 26),   # Viernes Santo
        dt.date(2027, 5, 31),   # Memorial Day
        dt.date(2027, 6, 18),   # Juneteenth, observado (el 19 cae en sábado)
        dt.date(2027, 7, 5),    # Independencia, observado (el 4 cae en domingo)
        dt.date(2027, 9, 6),    # Día del Trabajo
        dt.date(2027, 11, 25),  # Acción de Gracias
        dt.date(2027, 12, 24),  # Navidad, observado (el 25 cae en sábado)
    ),
}

#: Sesiones que cierran a las 13:00. Son tres por regla y las tres son vísperas: el día
#: después de Acción de Gracias (siempre), el 3 de julio y el 24 de diciembre — estos dos solo
#: si el festivo cae de martes a viernes; si cae en sábado la víspera ES el festivo observado,
#: y si cae en domingo el festivo se va al lunes y la víspera es una sesión entera.
#:
#: Se escriben a mano desde el calendario oficial de la NYSE, y `test_calendar.py` las
#: contrasta contra esa regla implementada aparte: una que FALTE deja al bot esperando tres
#: horas de datos que no llegan, que es molesto; una que SOBRE le hace cerrar a las 13:00 un
#: día que opera hasta las 16:00, y eso cuesta dinero.
US_HALF_DAYS: dict[int, tuple[dt.date, ...]] = {
    2026: (
        dt.date(2026, 11, 27),  # día después de Acción de Gracias
        dt.date(2026, 12, 24),  # Nochebuena
    ),
    2027: (
        dt.date(2027, 11, 26),  # día después de Acción de Gracias
    ),
}


@dataclass(frozen=True, slots=True)
class MarketCalendar:
    """Qué días hay sesión y hasta qué hora.

    holidays  : dict[int, tuple[date, ...]] — días sin sesión, por año.
    half_days : dict[int, tuple[date, ...]] — sesiones que cierran antes, por año.
    """

    holidays: dict[int, tuple[dt.date, ...]] = field(default_factory=dict)
    half_days: dict[int, tuple[dt.date, ...]] = field(default_factory=dict)

    @classmethod
    def us_equities(cls) -> "MarketCalendar":
        """Calendario de la renta variable estadounidense.

        return : MarketCalendar — con los festivos y medias sesiones de los años cubiertos.
        """
        return cls(holidays=US_HOLIDAYS, half_days=US_HALF_DAYS)

    def covers(self, year: int) -> bool:
        """Indica si la tabla cubre un año.

        year   : int — año consultado.
        return : bool
        """
        return year in self.holidays

    def _require(self, day: dt.date) -> None:
        """Falla si el año no está en la tabla.

        Responder "sí, es día de mercado" para un año que no se conoce es la respuesta
        peligrosa: el bot arrancaría un festivo, esperaría datos que no llegan y se quedaría
        mudo toda la jornada. Que falle obliga a actualizar la tabla, que es un minuto de
        trabajo una vez al año.

        day    : date — día consultado.
        return : None
        raises : ValueError — si el año no está cubierto.
        """
        if not self.covers(day.year):
            cubiertos = ", ".join(str(a) for a in sorted(self.holidays))
            raise ValueError(
                f"el calendario de mercado no cubre {day.year}. Años cubiertos: {cubiertos}. "
                f"Actualiza `US_HOLIDAYS` y `US_HALF_DAYS` en bot/core/calendar.py con el "
                f"calendario oficial de la NYSE."
            )

    def is_trading_day(self, day: dt.date) -> bool:
        """Indica si un día tiene sesión.

        day    : date — día consultado.
        return : bool — False en fines de semana y festivos.
        raises : ValueError — si el año no está cubierto por la tabla.
        """
        if day.weekday() >= 5:
            return False
        self._require(day)
        return day not in self.holidays[day.year]

    def is_half_day(self, day: dt.date) -> bool:
        """Indica si un día cierra antes de lo normal.

        day    : date — día consultado.
        return : bool
        raises : ValueError — si el año no está cubierto por la tabla.
        """
        self._require(day)
        return day in self.half_days.get(day.year, ())

    def close_time(self, day: dt.date) -> dt.time:
        """Hora de cierre de la sesión de un día.

        day    : date — día consultado.
        return : time — 16:00 en una sesión normal, 13:00 en una media sesión.
        raises : ValueError — si el año no está cubierto por la tabla.
        """
        return HALF_DAY_CLOSE if self.is_half_day(day) else REGULAR_CLOSE
