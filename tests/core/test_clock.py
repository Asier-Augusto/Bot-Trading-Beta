"""Pruebas del reloj inyectable."""

import datetime as dt

import pytest

from bot.core.clock import RealClock, SimulatedClock

NY = "America/New_York"


def test_reloj_simulado_devuelve_el_instante_fijado():
    inicio = dt.datetime(2026, 3, 10, 10, 30, tzinfo=dt.timezone.utc)
    clock = SimulatedClock(start=inicio, timezone=NY)

    assert clock.now() == inicio


def test_reloj_simulado_avanza_y_no_retrocede():
    inicio = dt.datetime(2026, 3, 10, 10, 30, tzinfo=dt.timezone.utc)
    clock = SimulatedClock(start=inicio, timezone=NY)

    clock.advance_to(inicio + dt.timedelta(minutes=5))
    assert clock.now() == inicio + dt.timedelta(minutes=5)

    with pytest.raises(ValueError, match="hacia atrás"):
        clock.advance_to(inicio)


def test_reloj_simulado_sleep_avanza_el_tiempo_sin_esperar():
    inicio = dt.datetime(2026, 3, 10, 10, 30, tzinfo=dt.timezone.utc)
    clock = SimulatedClock(start=inicio, timezone=NY)

    clock.sleep(60)

    assert clock.now() == inicio + dt.timedelta(seconds=60)


@pytest.mark.parametrize(
    "hora_ny, abierto",
    [
        (dt.time(9, 29), False),
        (dt.time(9, 30), True),
        (dt.time(15, 59), True),
        (dt.time(16, 0), False),
    ],
)
def test_mercado_abierto_segun_hora_de_nueva_york(hora_ny, abierto):
    # 10 de marzo de 2026 es un martes laborable.
    momento = dt.datetime(2026, 3, 10, hora_ny.hour, hora_ny.minute)
    clock = SimulatedClock(start=momento, timezone=NY, naive_is_local=True)

    assert clock.is_market_open() is abierto


def test_mercado_cerrado_en_fin_de_semana():
    # 14 de marzo de 2026 es sábado.
    sabado = dt.datetime(2026, 3, 14, 11, 0)
    clock = SimulatedClock(start=sabado, timezone=NY, naive_is_local=True)

    assert clock.is_market_open() is False


def test_reloj_real_devuelve_hora_consciente_de_zona():
    clock = RealClock(timezone=NY)

    assert clock.now().tzinfo is not None
