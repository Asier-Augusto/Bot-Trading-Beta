"""Pruebas del límite de ritmo.

Todas usan `SimulatedClock`: es lo que permite comprobar el número exacto de segundos de
espera sin que la batería tarde minutos durmiendo de verdad.
"""

import datetime as dt

import pytest

from bot.core.clock import SimulatedClock
from bot.data.rate_limiter import RateLimiter

T0 = dt.datetime(2026, 8, 26, 14, 0, tzinfo=dt.timezone.utc)


def _limitador(max_calls=3, per_seconds=10.0):
    reloj = SimulatedClock(T0, "UTC")
    return RateLimiter(max_calls, per_seconds, reloj), reloj


def test_las_primeras_llamadas_no_esperan():
    """Con hueco de sobra, el limitador no debe estorbar."""
    limitador, _reloj = _limitador()

    assert [limitador.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]


def test_la_llamada_que_pasa_del_limite_espera_hasta_que_caduca_la_primera():
    limitador, reloj = _limitador(max_calls=3, per_seconds=10.0)
    for _ in range(3):
        limitador.acquire()
    reloj.sleep(4)                       # han pasado 4 s desde la primera

    assert limitador.acquire() == pytest.approx(6.0)


def test_esperar_avanza_el_reloj_inyectado():
    """El limitador no llama a `time.sleep`: se lo pide al reloj. Con reloj simulado eso no
    bloquea, y es lo que permite probarlo."""
    limitador, reloj = _limitador(max_calls=1, per_seconds=10.0)
    limitador.acquire()

    limitador.acquire()

    assert reloj.now() == T0 + dt.timedelta(seconds=10)


def test_tras_esperar_vuelve_a_haber_hueco():
    """La tercera llamada espera; al hacerlo, las dos primeras ya han salido de la ventana,
    así que la cuarta vuelve a entrar sin esperar. Es lo que distingue una ventana
    deslizante de una cola que se atasca."""
    limitador, _reloj = _limitador(max_calls=2, per_seconds=10.0)
    for _ in range(2):
        limitador.acquire()

    assert limitador.acquire() > 0        # la tercera espera
    assert limitador.would_wait() == 0.0  # y al esperar liberó hueco para la cuarta


def test_las_fichas_caducan_por_ventana_deslizante_y_no_por_bloques():
    """Con ventanas por bloques, N peticiones al final de un bloque y N al principio del
    siguiente son 2N en un instante, que es justo lo que el límite existe para impedir.

    Se comprueba así: con dos llamadas en el instante 0 y una ventana de 10 s, en el
    instante 10 tiene que haber hueco para DOS más, y ni una antes.
    """
    limitador, reloj = _limitador(max_calls=2, per_seconds=10.0)
    limitador.acquire()
    limitador.acquire()

    reloj.sleep(9.5)
    assert limitador.would_wait() == pytest.approx(0.5)

    reloj.sleep(0.5)
    assert limitador.would_wait() == 0.0


def test_would_wait_no_consume_una_ficha():
    """Preguntar cuánto habría que esperar se usa para decidir en qué orden pedir varias
    cosas. Si contara como petición, preguntar saldría igual de caro que hacer."""
    limitador, _reloj = _limitador(max_calls=2, per_seconds=10.0)

    for _ in range(20):
        assert limitador.would_wait() == 0.0

    assert limitador.acquire() == 0.0
    assert limitador.acquire() == 0.0


def test_would_wait_no_avanza_el_reloj():
    limitador, reloj = _limitador(max_calls=1, per_seconds=10.0)
    limitador.acquire()

    limitador.would_wait()

    assert reloj.now() == T0


def test_un_limite_no_positivo_se_rechaza():
    reloj = SimulatedClock(T0, "UTC")

    with pytest.raises(ValueError, match="positivos"):
        RateLimiter(0, 10.0, reloj)
    with pytest.raises(ValueError, match="positivos"):
        RateLimiter(10, 0.0, reloj)


def test_el_ritmo_de_IBKR_deja_pasar_sesenta_y_para_la_sesenta_y_una():
    """El caso real: IBKR admite del orden de 60 peticiones cada 10 minutos."""
    reloj = SimulatedClock(T0, "UTC")
    limitador = RateLimiter(60, 600.0, reloj)

    esperas = [limitador.acquire() for _ in range(61)]

    assert esperas[:60] == [0.0] * 60
    assert esperas[60] == pytest.approx(600.0)
