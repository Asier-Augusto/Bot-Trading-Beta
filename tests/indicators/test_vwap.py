"""Pruebas del VWAP anclado a la sesión.

El VWAP es la referencia natural de una estrategia intradía de reversión: se entra lejos de
él y se sale cerca. Lo que lo hace útil aquí y no otro promedio es que **se puede leer en el
instante de la entrada**: la distancia al VWAP es el recorrido que la operación tiene por
delante, y comparada con la distancia al stop dice si la operación merece la pena ANTES de
tomarla. El estocástico no puede decir eso: al entrar nadie sabe dónde va a saltar.
"""

import datetime as dt

import pytest

from bot.core.types import Bar
from bot.indicators.vwap import VWAP

TZ = dt.timezone.utc


def vela(minuto, precio, volumen=100, high=None, low=None):
    ts = dt.datetime(2026, 3, 10, 14, minuto, tzinfo=TZ)
    return Bar("TSLA", "1m", ts, precio, high or precio, low or precio, precio, volumen)


def test_una_sola_vela_da_su_precio_tipico():
    """El precio típico es (máximo + mínimo + cierre) / 3, no el cierre: el VWAP pondera por
    dónde se negoció de verdad, y una vela con cola larga no se negoció en su cierre."""
    v = VWAP()

    v.update(vela(30, 100.0, volumen=500, high=102.0, low=98.0))

    assert v.value == pytest.approx(100.0)


def test_pondera_por_volumen_y_no_por_numero_de_velas():
    """Es la diferencia entera con una media móvil: mil acciones a 90 pesan diez veces más
    que cien a 110, y una media simple diría 100."""
    v = VWAP()

    v.update(vela(30, 90.0, volumen=1000))
    v.update(vela(31, 110.0, volumen=100))

    assert v.value == pytest.approx((90.0 * 1000 + 110.0 * 100) / 1100)


def test_una_vela_sin_volumen_no_mueve_el_VWAP():
    """Una vela sin negociar no dice a qué precio se negoció. Contarla como un dato más
    arrastraría el VWAP hacia un precio que nadie pagó."""
    v = VWAP()
    v.update(vela(30, 100.0, volumen=1000))

    v.update(vela(31, 200.0, volumen=0))

    assert v.value == pytest.approx(100.0)


def test_la_sesion_nueva_empieza_de_cero():
    """Anclado a la sesión: el VWAP de hoy no sabe nada de ayer. Un VWAP que arrastrase la
    sesión anterior estaría anclado a un precio que ya no significa nada."""
    v = VWAP()
    v.update(vela(30, 100.0, volumen=1000))

    v.start_session()
    v.update(vela(31, 200.0, volumen=100))

    assert v.value == pytest.approx(200.0)


def test_sin_velas_no_hay_valor():
    """Un cero se leería como "el VWAP está en cero", que a efectos de distancia es la
    conclusión más equivocada posible."""
    assert VWAP().value is None
    assert not VWAP().ready


def test_no_necesita_calentamiento():
    """Se ancla a la sesión: pedirle velas de ayer sería contradecir su propia definición."""
    assert VWAP().warmup_bars == 0
