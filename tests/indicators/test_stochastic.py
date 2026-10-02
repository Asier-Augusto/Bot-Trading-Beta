"""Pruebas del oscilador estocástico."""

import datetime as dt

import pytest

from bot.core.types import Bar
from bot.indicators.stochastic import StochasticD, StochasticK

TZ = dt.timezone.utc


def _vela(indice: int, alto: float, bajo: float, cierre: float) -> Bar:
    ts = dt.datetime(2026, 3, 10, 14, 0, tzinfo=TZ) + dt.timedelta(minutes=indice)
    return Bar("TSLA", "1m", ts, cierre, alto, bajo, cierre, 1000)


def _alimentar(indicador, velas):
    for vela in velas:
        indicador.update(vela)
    return indicador.value


def test_el_K_vale_100_cuando_el_cierre_es_el_maximo_del_periodo():
    """El estocástico mide DÓNDE cierra el precio dentro del rango del periodo. Cerrar en el
    máximo es el 100 % del rango."""
    indicador = StochasticK(period=5, smooth=1)
    velas = [_vela(i, 110.0, 90.0, 100.0) for i in range(4)]
    velas.append(_vela(4, 110.0, 90.0, 110.0))

    assert _alimentar(indicador, velas) == pytest.approx(100.0)


def test_el_K_vale_0_cuando_el_cierre_es_el_minimo_del_periodo():
    indicador = StochasticK(period=5, smooth=1)
    velas = [_vela(i, 110.0, 90.0, 100.0) for i in range(4)]
    velas.append(_vela(4, 110.0, 90.0, 90.0))

    assert _alimentar(indicador, velas) == pytest.approx(0.0)


def test_el_K_vale_50_a_mitad_del_rango():
    indicador = StochasticK(period=5, smooth=1)
    velas = [_vela(i, 110.0, 90.0, 100.0) for i in range(5)]

    assert _alimentar(indicador, velas) == pytest.approx(50.0)


def test_un_rango_plano_no_divide_por_cero():
    """Un símbolo parado `period` velas seguidas tiene máximo igual a mínimo. Ni
    sobrecomprado ni sobrevendido es la única respuesta honesta, y devolverla evita reventar
    en mitad de la sesión por un símbolo que dejó de cotizar unos minutos — que es
    exactamente lo que le pasa a ONEQ en la mitad de los minutos (§7.2)."""
    indicador = StochasticK(period=5, smooth=1)
    velas = [_vela(i, 100.0, 100.0, 100.0) for i in range(5)]

    assert _alimentar(indicador, velas) == pytest.approx(50.0)


def test_no_hay_valor_hasta_tener_velas_suficientes():
    """Hacen falta `period` velas para el primer %K crudo y `smooth` crudos para el %K
    lento: con periodo 5 y suavizado 3, el primer valor sale en la séptima vela."""
    indicador = StochasticK(period=5, smooth=3)

    for i in range(7):
        indicador.update(_vela(i, 110.0, 90.0, 100.0))
        if i < 6:
            assert indicador.value is None, f"vela {i}"

    assert indicador.value is not None


def test_el_periodo_no_positivo_se_rechaza():
    with pytest.raises(ValueError, match="positivos"):
        StochasticK(period=0)
    with pytest.raises(ValueError, match="positivos"):
        StochasticK(period=5, smooth=0)
    with pytest.raises(ValueError, match="positivos"):
        StochasticD(period=5, smooth=3, signal=0)


#: Siete velas con rango fijo [90, 110], así que el máximo y el mínimo del periodo son
#: siempre 110 y 90 y el %K crudo de cada vela es `(cierre - 90) / 20 * 100`. Solo las tres
#: últimas producen crudo, porque las cuatro primeras no completan el periodo de cinco:
#:
#:     vela      0    1    2    3     4     5     6
#:     cierre   90   95  100  105   110   105   100
#:     %K crudo  -    -    -    -   100    75    50
_SERIE_A_MANO = [
    _vela(0, 110.0, 90.0, 90.0),
    _vela(1, 110.0, 90.0, 95.0),
    _vela(2, 110.0, 90.0, 100.0),
    _vela(3, 110.0, 90.0, 105.0),
    _vela(4, 110.0, 90.0, 110.0),
    _vela(5, 110.0, 90.0, 105.0),
    _vela(6, 110.0, 90.0, 100.0),
]


def test_contra_valores_calculados_a_mano():
    """Los números salen de la tabla de `_SERIE_A_MANO`, calculada aparte, para que la
    prueba no valide la implementación contra sí misma.

    Con `smooth=1` el %K lento es el último crudo: 50.
    Con `smooth=3` es el promedio de los tres: (100 + 75 + 50) / 3 = 75.
    """
    assert _alimentar(StochasticK(period=5, smooth=1), _SERIE_A_MANO) == pytest.approx(50.0)
    assert _alimentar(StochasticK(period=5, smooth=3), _SERIE_A_MANO) == pytest.approx(75.0)


def test_el_D_es_el_promedio_de_los_ultimos_K():
    """Con `smooth=1` y `signal=3`, el %D promedia los tres %K crudos: (100 + 75 + 50) / 3."""
    d = StochasticD(period=5, smooth=1, signal=3)

    assert _alimentar(d, _SERIE_A_MANO) == pytest.approx(75.0)


def test_el_D_va_por_detras_del_K():
    """Es su razón de ser: el %K entra y sale del umbral en cada tirón, y el %D confirma que
    la sobrecompra se ha sostenido. Que Moris exija los dos por encima de 80 no es
    redundante."""
    velas = [_vela(i, 110.0, 90.0, 90.0 + i) for i in range(20)]
    k = StochasticK(period=5, smooth=3)
    d = StochasticD(period=5, smooth=3, signal=3)

    assert _alimentar(k, velas) > _alimentar(d, velas)


def test_los_dos_estan_registrados_por_nombre():
    from bot.core.registry import get_indicator

    assert get_indicator("stoch_k") is StochasticK
    assert get_indicator("stoch_d") is StochasticD


def test_declaran_su_calentamiento():
    """Sin calentamiento, la salida por sobrecompra no existiría durante los primeros
    minutos de cada sesión: justo cuando una posición abierta la víspera podría necesitarla."""
    assert StochasticK(period=14, smooth=3).warmup_bars >= 14
    assert StochasticD(period=14, smooth=3, signal=3).warmup_bars >= 14


def test_dos_periodos_distintos_no_colisionan_en_el_conjunto_de_indicadores():
    """Comprueba la clave con parámetros desde el consumidor que la motivó: un `stoch_k` de
    14 y otro de 5 en el mismo marco son dos instancias, no una."""
    from bot.contracts.strategy import IndicatorSpec
    from bot.core.indicator_set import IndicatorSet

    with pytest.raises(ValueError, match="stoch_k"):
        IndicatorSet(
            ["TSLA"],
            [
                IndicatorSpec("stoch_k", "1m", params={"period": 14}),
                IndicatorSpec("stoch_k", "1m", params={"period": 5}),
            ],
        )
