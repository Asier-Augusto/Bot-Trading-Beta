"""Pruebas del calendario de mercado."""

import datetime as dt

import pytest

from bot.core.calendar import HALF_DAY_CLOSE, REGULAR_CLOSE, MarketCalendar
from bot.core.clock import RealClock, _is_regular_session


def _cal():
    return MarketCalendar.us_equities()


def test_un_martes_normal_es_dia_de_mercado():
    assert _cal().is_trading_day(dt.date(2026, 8, 25)) is True


@pytest.mark.parametrize("dia", [dt.date(2026, 8, 29), dt.date(2026, 8, 30)])
def test_un_fin_de_semana_no_es_dia_de_mercado(dia):
    assert _cal().is_trading_day(dia) is False


@pytest.mark.parametrize(
    "dia",
    [
        dt.date(2026, 1, 1),    # Año Nuevo
        dt.date(2026, 7, 3),    # Independencia, observado
        dt.date(2026, 11, 26),  # Acción de Gracias
        dt.date(2026, 12, 25),  # Navidad
        dt.date(2027, 3, 26),   # Viernes Santo
        dt.date(2027, 12, 24),  # Navidad, observado
    ],
)
def test_un_festivo_no_es_dia_de_mercado(dia):
    """En vivo, sin esto el bot esperaría ticks que no llegan, calentaría con el histórico de
    ayer y llegaría al cierre forzado sin haber visto un precio."""
    assert _cal().is_trading_day(dia) is False


def test_todos_los_festivos_de_la_tabla_caen_en_dia_laborable():
    """Un festivo que cayera en fin de semana se observa otro día, y es ESE el que va en la
    tabla. Uno en sábado sería una entrada copiada sin comprobar."""
    for ano, dias in _cal().holidays.items():
        for dia in dias:
            assert dia.weekday() < 5, f"{dia} de {ano} cae en fin de semana"
            assert dia.year == ano, f"{dia} está listado bajo {ano}"


def test_todas_las_medias_sesiones_son_dias_de_mercado():
    """Una media sesión que además fuera festivo sería una contradicción en la tabla."""
    cal = _cal()
    for dias in cal.half_days.values():
        for dia in dias:
            assert cal.is_trading_day(dia), f"{dia} está como media sesión y como festivo"


def test_una_sesion_normal_cierra_a_las_cuatro():
    assert _cal().close_time(dt.date(2026, 8, 25)) == REGULAR_CLOSE


def test_una_media_sesion_cierra_antes():
    """El día después de Acción de Gracias cierra a la una. Con el cierre normal, el bot
    esperaría tres horas de ticks que no llegan y el cierre forzado de las 15:45 no
    encontraría mercado donde vender."""
    assert _cal().close_time(dt.date(2026, 11, 27)) == HALF_DAY_CLOSE


def test_preguntar_por_un_ano_sin_tabla_falla_ruidosamente():
    """Responder "sí, es día de mercado" para un año desconocido es la respuesta peligrosa:
    el bot arrancaría un festivo y se quedaría mudo toda la jornada. Que falle obliga a
    actualizar la tabla, que es un minuto de trabajo una vez al año."""
    with pytest.raises(ValueError, match="2031"):
        _cal().is_trading_day(dt.date(2031, 1, 8))


def test_el_mensaje_dice_donde_actualizar_la_tabla():
    with pytest.raises(ValueError, match="calendar.py"):
        _cal().close_time(dt.date(2031, 1, 8))


def test_un_fin_de_semana_de_un_ano_sin_tabla_no_falla():
    """El día de la semana no depende de la tabla: responder que un sábado no hay mercado no
    exige saber los festivos de ese año."""
    assert _cal().is_trading_day(dt.date(2031, 1, 4)) is False


# --- El reloj real -------------------------------------------------------------------

#: 11:00 de Nueva York del 3 de julio de 2026, que es festivo observado.
FESTIVO = dt.datetime(2026, 7, 3, 15, 0, tzinfo=dt.timezone.utc)


def test_RealClock_con_calendario_sabe_que_un_festivo_no_hay_sesion():
    reloj = RealClock("America/New_York", _cal())

    assert _is_regular_session(FESTIVO, reloj._tz, reloj._calendar) is False


def test_RealClock_sin_calendario_se_comporta_como_antes():
    """Compatibilidad: el backtest no lo necesita y no debe obligarse a montarlo. Con esto se
    ve además por qué en vivo hay que pasarlo: sin él, un festivo pasa por día normal."""
    reloj = RealClock("America/New_York")

    assert _is_regular_session(FESTIVO, reloj._tz, None) is True


def test_una_media_sesion_cierra_antes_tambien_para_el_reloj():
    reloj = RealClock("America/New_York", _cal())
    temprano = dt.datetime(2026, 11, 27, 15, 0, tzinfo=dt.timezone.utc)   # 10:00 en NY
    tarde = dt.datetime(2026, 11, 27, 19, 0, tzinfo=dt.timezone.utc)      # 14:00 en NY

    assert _is_regular_session(temprano, reloj._tz, reloj._calendar) is True
    assert _is_regular_session(tarde, reloj._tz, reloj._calendar) is False


def test_una_sesion_normal_sigue_abierta_a_las_dos_de_la_tarde():
    reloj = RealClock("America/New_York", _cal())
    tarde = dt.datetime(2026, 8, 25, 18, 0, tzinfo=dt.timezone.utc)       # 14:00 en NY

    assert _is_regular_session(tarde, reloj._tz, reloj._calendar) is True


# --- F4: la tabla de medias sesiones, contra la regla que la genera ------------------


def _medias_sesiones_por_regla(anio: int) -> set[dt.date]:
    """Las medias sesiones que la NYSE tiene por regla en un año.

    Son tres y solo tres, y las tres son vísperas:

    - **El día después de Acción de Gracias**, siempre. Acción de Gracias es el cuarto jueves
      de noviembre, así que el viernes siguiente cae siempre en día de mercado.
    - **El 3 de julio**, pero solo si el 4 cae de martes a viernes. Si el 4 cae en sábado, el
      viernes 3 es el festivo OBSERVADO y no hay media sesión; si cae en domingo, el festivo
      se va al lunes 5 y el viernes 2 es una sesión entera.
    - **El 24 de diciembre**, con la misma condición sobre el 25.

    Está aquí y no en el código de producción a propósito: la tabla se escribe a mano desde el
    calendario oficial, y una regla que se comprobara contra sí misma no probaría nada. Lo que
    hace esta función es dar una segunda opinión independiente sobre lo que alguien tecleó.

    anio   : int — año.
    return : set[date] — las medias sesiones que tocan.
    """
    jueves_de_noviembre = [
        dt.date(anio, 11, d) for d in range(1, 31) if dt.date(anio, 11, d).weekday() == 3
    ]
    medias = {jueves_de_noviembre[3] + dt.timedelta(days=1)}
    for mes, dia_fiesta in ((7, 4), (12, 25)):
        fiesta = dt.date(anio, mes, dia_fiesta)
        if 1 <= fiesta.weekday() <= 4:          # de martes a viernes
            medias.add(fiesta - dt.timedelta(days=1))
    return medias


@pytest.mark.parametrize("anio", sorted(MarketCalendar.us_equities().half_days))
def test_la_tabla_de_medias_sesiones_dice_lo_que_dice_la_regla(anio):
    """Una media sesión que FALTA deja al bot esperando tres horas de datos que no llegan
    --molesto--. Una que SOBRA le hace cerrar a las 13:00 un día que opera hasta las 16:00
    --eso sí cuesta dinero--. Esta prueba caza las dos, y sobre todo cazará la omisión el día
    que alguien añada un año nuevo a mano."""
    calendario = MarketCalendar.us_equities()

    assert set(calendario.half_days[anio]) == _medias_sesiones_por_regla(anio)


def test_ninguna_media_sesion_coincide_con_un_festivo():
    """Listar un día como media sesión Y como festivo es decir dos cosas incompatibles; gana
    el festivo y la media sesión no se cumple nunca, así que nadie se entera del error."""
    calendario = MarketCalendar.us_equities()

    for anio, dias in calendario.half_days.items():
        for dia in dias:
            assert dia not in calendario.holidays.get(anio, ()), f"{dia} está en las dos tablas"


def test_todos_los_anios_con_festivos_tienen_su_tabla_de_medias_sesiones():
    """Un año con festivos pero sin medias sesiones no es "no tiene": es "se olvidaron".
    Siempre hay al menos una, la del día después de Acción de Gracias."""
    calendario = MarketCalendar.us_equities()

    assert set(calendario.holidays) <= set(calendario.half_days)
