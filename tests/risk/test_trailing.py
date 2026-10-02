"""Pruebas del trailing stop (§7.3)."""

import datetime as dt

import pytest

from bot.core.context import Position
from bot.core.types import Bar
from bot.risk.trailing import TrailingParams, TrailingStopManager

TZ = dt.timezone.utc
TS = dt.datetime(2026, 3, 10, 14, 32, tzinfo=TZ)

#: Posición de referencia: entra a 412, stop a 410, así que R vale 2,00 por acción.
POSICION = Position("TSLA", 50, 412.0, 410.0, "aggressive", "sig1")
#: La misma, en el tramo conservador. El trailing NO los trata igual: el agresivo sigue al
#: precio más de lejos porque entró antes y sin confirmación.
CONSERVADORA = Position("TSLA", 50, 412.0, 410.0, "conservative", "sig1")


def _vela(maximo: float, cierre: float | None = None, symbol: str = "TSLA") -> Bar:
    cierre = maximo if cierre is None else cierre
    return Bar(symbol, "1m", TS, cierre, maximo, cierre - 1.0, cierre, 1000)


def _gestor(**extra) -> TrailingStopManager:
    return TrailingStopManager(TrailingParams(**{"activate_at_r": 1.0, "distance_atr": 1.5, **extra}))


def test_no_se_activa_por_debajo_del_umbral_de_R():
    """Una posición a +0,5R sigue con su stop original: el trailing existe para proteger
    beneficio consolidado, no para estrechar una posición que aún respira. A +0,3R el precio
    todavía está dentro del ruido normal de la entrada."""
    subidas = _gestor().review([POSICION], _vela(maximo=413.0), atr=1.0)   # +1,00 = 0,5R

    assert subidas == []


def test_se_activa_al_superar_el_umbral_de_R():
    subidas = _gestor().review([POSICION], _vela(maximo=414.0), atr=1.0)   # +2,00 = 1,0R

    assert len(subidas) == 1
    assert subidas[0][0] == "TSLA"
    assert subidas[0][1] == "aggressive"


def test_el_stop_se_situa_a_distancia_atr_del_maximo_de_la_vela():
    """La referencia es el MÁXIMO, no el cierre: el stop es una orden viva en el mercado, y
    lo que importa es hasta dónde llegó el precio mientras la orden estaba puesta."""
    subidas = _gestor(distance_atr=1.5).review(
        [CONSERVADORA], _vela(maximo=418.0, cierre=415.0), atr=2.0
    )

    assert subidas[0][2] == pytest.approx(415.0)   # 418.0 - 1.5 * 2.0


def test_el_tramo_agresivo_sigue_al_precio_mas_de_lejos():
    """Pedido el 2026-09-10: que el agresivo mueva el stop "igual que la conservadora pero
    un poco menos", dejándole más margen por si el precio se da la vuelta. Con `wide_slack`
    de 1,5, su distancia son 2,25 ATR donde la conservadora usa 1,5."""
    vela = _vela(maximo=418.0, cierre=415.0)

    agresivo = _gestor(distance_atr=1.5).review([POSICION], vela, atr=2.0)
    conservador = _gestor(distance_atr=1.5).review([CONSERVADORA], vela, atr=2.0)

    assert agresivo[0][2] == pytest.approx(413.5)      # 418.0 - 1.5 * 1.5 * 2.0
    assert agresivo[0][2] < conservador[0][2]


def test_al_ganar_1R_el_stop_no_baja_de_la_entrada():
    """La regla de "no devolver nunca lo ganado". El seguido daría 411,0 —por debajo de la
    entrada, o sea una operación que ya ganó 1R y todavía puede acabar en pérdida—, así que
    manda el precio de entrada."""
    subidas = _gestor(distance_atr=1.5, breakeven_at_r=1.0).review(
        [CONSERVADORA], _vela(maximo=414.0), atr=2.0
    )

    assert subidas[0][2] == pytest.approx(412.0)       # el precio de entrada, no 411.0


def test_sin_llegar_a_1R_no_se_sube_a_la_entrada():
    """A +0,5R la posición todavía respira: subir el stop a la entrada ahí es la forma más
    eficaz de convertir una operación buena en una salida prematura."""
    subidas = _gestor(distance_atr=1.5, breakeven_at_r=1.0).review(
        [CONSERVADORA], _vela(maximo=413.0), atr=2.0
    )

    assert subidas == []


def test_el_stop_nunca_baja():
    """La prueba más importante de este módulo. Un trailing que retrocede amplía el riesgo
    de una posición ganadora sin que nadie lo haya decidido, y convierte una operación buena
    en una perdedora."""
    ya_subido = Position("TSLA", 50, 412.0, 416.0, "aggressive", "sig1")

    # Máximo 418 con ATR 2.0 daría 415.0, que es PEOR que el stop actual de 416.0.
    subidas = _gestor().review([ya_subido], _vela(maximo=418.0), atr=2.0)

    assert subidas == []


def test_un_stop_igual_al_actual_tampoco_se_propone():
    """Mover un stop al mismo precio es una orden al bróker que no cambia nada, y en IBKR
    cada modificación es una petición de verdad."""
    ya_subido = Position("TSLA", 50, 412.0, 415.0, "aggressive", "sig1")

    assert _gestor().review([ya_subido], _vela(maximo=418.0), atr=2.0) == []


def test_sin_ATR_no_se_toca_nada():
    """Durante el calentamiento no hay distancia que calcular. Inventar una sería peor que
    dejar el stop original."""
    assert _gestor().review([POSICION], _vela(maximo=420.0), atr=None) == []
    assert _gestor().review([POSICION], _vela(maximo=420.0), atr=0.0) == []


def test_desactivado_no_propone_nada():
    gestor = TrailingStopManager(TrailingParams(enabled=False))

    assert gestor.review([POSICION], _vela(maximo=420.0), atr=1.0) == []


def test_una_posicion_sin_stop_no_se_puede_seguir():
    """Sin riesgo por acción no hay R que medir. Lo que falta ahí es el stop, no el
    trailing, y fabricar uno taparía el problema de verdad."""
    sin_stop = Position("TSLA", 50, 412.0, 0.0, "aggressive", "sig1")

    assert _gestor().review([sin_stop], _vela(maximo=420.0), atr=1.0) == []


def test_cada_tramo_se_revisa_por_separado():
    """El tramo agresivo entró más abajo y con un stop más amplio, así que alcanza su 1R en
    un punto distinto del conservador. Un umbral común los trataría como si fueran la misma
    posición y borraría la diferencia que la variante mixta existe para medir."""
    agresivo = Position("TSLA", 25, 412.0, 410.0, "aggressive", "sig1")      # R = 2,00
    conservador = Position("TSLA", 25, 412.8, 411.6, "conservative", "sig1")  # R = 1,20

    # Máximo 414,0: el agresivo va +2,00 (1,0R) y el conservador +1,20 (1,0R). Los dos.
    ambos = _gestor().review([agresivo, conservador], _vela(maximo=414.0), atr=1.0)
    assert {t for _s, t, _p in ambos} == {"aggressive", "conservative"}

    # Máximo 413,5: el agresivo va +1,50 (0,75R) y el conservador +0,70 (0,58R). Ninguno.
    ninguno = _gestor().review([agresivo, conservador], _vela(maximo=413.5), atr=1.0)
    assert ninguno == []


def test_solo_se_revisan_los_tramos_del_simbolo_de_la_vela():
    """Una vela de TSLA no puede mover el stop de una posición abierta en NVDA."""
    otra = Position("NVDA", 50, 100.0, 98.0, "base", "sig2")

    assert _gestor().review([otra], _vela(maximo=420.0, symbol="TSLA"), atr=1.0) == []


def test_un_tramo_ya_cerrado_no_se_revisa():
    cerrado = Position("TSLA", 0, 412.0, 410.0, "aggressive", "sig1")

    assert _gestor().review([cerrado], _vela(maximo=420.0), atr=1.0) == []


def test_los_parametros_se_leen_del_perfil():
    params = TrailingParams.from_dict({"enabled": True, "activate_at_r": 2.0, "distance_atr": 3.0})

    assert params.activate_at_r == 2.0
    assert params.distance_atr == 3.0


def test_los_parametros_ausentes_toman_los_del_diseno():
    params = TrailingParams.from_dict({})

    assert params.enabled is True
    assert params.activate_at_r == 1.0
    assert params.distance_atr == 1.5
