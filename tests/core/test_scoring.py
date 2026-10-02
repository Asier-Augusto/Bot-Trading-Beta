"""Pruebas de la puntuación continua de señales.

La comprobación más importante de todo el fichero es
`test_una_puntuacion_altisima_no_arma`: si esa prueba se cae, el sistema estaría
entrando en operaciones por "casi", que es una estrategia distinta de la de Moris.
"""

import pytest

from bot.core.scoring import (
    ConditionResult,
    ConditionSpec,
    LevelThresholds,
    SignalLevel,
    ramp_score,
    score_signal,
)

NIVELES = LevelThresholds(near_miss=0.80, partial=0.55, armed_requires_all=True)

#: Los pesos del diseño: la ubicación y la temporización pesan lo mismo entre sí.
SPECS = {
    "support": ConditionSpec("support", weight=2.0, reach=1.0, min_score=0.15),
    "rsi_ndx": ConditionSpec("rsi_ndx", weight=1.5, reach=8.0),
    "rsi_1m": ConditionSpec("rsi_1m", weight=1.0, reach=10.0),
    "rsi_5m": ConditionSpec("rsi_5m", weight=1.0, reach=10.0),
}


def resultados(**puntuaciones: float) -> list[ConditionResult]:
    """Fabrica resultados de condición con las puntuaciones dadas."""
    return [
        ConditionResult(
            name=nombre,
            raw_value=0.0,
            threshold=0.0,
            score=puntuaciones[nombre],
            weight=SPECS[nombre].weight,
            passed=puntuaciones[nombre] >= 1.0,
            min_score=SPECS[nombre].min_score,
        )
        for nombre in SPECS
    ]


@pytest.mark.parametrize(
    "valor, esperado",
    [
        (30.0, 1.0),    # justo en el umbral
        (25.0, 1.0),    # por debajo del umbral
        (32.0, 0.8),    # a dos puntos, con alcance 10
        (35.0, 0.5),
        (40.0, 0.0),    # justo en el borde del alcance
        (55.0, 0.0),    # muy lejos, sin negativos
    ],
)
def test_la_rampa_lineal_en_sus_puntos_clave(valor, esperado):
    assert ramp_score(valor, threshold=30.0, reach=10.0) == pytest.approx(esperado)


def test_la_rampa_admite_condiciones_al_alza():
    # Para un umbral de tipo ">=", como el estocástico de salida.
    assert ramp_score(85.0, threshold=80.0, reach=10.0, direction="above") == 1.0
    assert ramp_score(75.0, threshold=80.0, reach=10.0, direction="above") == pytest.approx(0.5)


def test_alcance_no_positivo_es_un_error():
    with pytest.raises(ValueError, match="alcance"):
        ramp_score(30.0, threshold=30.0, reach=0.0)


def test_todas_cumplidas_arma_y_puntua_uno():
    puntuacion = score_signal(
        resultados(support=1.0, rsi_ndx=1.0, rsi_1m=1.0, rsi_5m=1.0), NIVELES
    )

    assert puntuacion.armed is True
    assert puntuacion.total == pytest.approx(1.0)
    assert puntuacion.level is SignalLevel.ARMED
    assert puntuacion.blocking == ()


def test_una_puntuacion_altisima_no_arma():
    # Tres condiciones perfectas y una a 0.99: 0.997 de total, pero NO arma.
    puntuacion = score_signal(
        resultados(support=1.0, rsi_ndx=0.99, rsi_1m=1.0, rsi_5m=1.0), NIVELES
    )

    assert puntuacion.total > 0.99
    assert puntuacion.armed is False
    assert puntuacion.level is SignalLevel.NEAR_MISS
    assert puntuacion.blocking == ("rsi_ndx",)


def test_armar_depende_de_passed_y_no_del_total():
    """Con total exactamente 1.0 pero una condición incumplida, NO se arma.

    Pina el contrato: `armed` sale de que todas las condiciones se cumplan, nunca de
    comparar el total contra un umbral. Sin esta prueba, una implementación que armara
    por `total >= 1.0` pasaría igual, porque en datos normales ambas cosas coinciden.
    """
    condiciones = [
        ConditionResult("a", 0.0, 0.0, score=1.0, weight=1.0, passed=True,  min_score=None),
        ConditionResult("b", 0.0, 0.0, score=1.0, weight=1.0, passed=False, min_score=None),
    ]

    puntuacion = score_signal(condiciones, NIVELES)

    assert puntuacion.total == pytest.approx(1.0)
    assert puntuacion.armed is False
    assert puntuacion.blocking == ("b",)
    assert puntuacion.level is SignalLevel.NEAR_MISS


def test_el_caso_del_documento_de_diseno():
    # rsi_ndx falla por 1.2 con alcance 8 -> 0.85; el resto perfecto.
    puntuacion = score_signal(
        resultados(support=1.0, rsi_ndx=0.85, rsi_1m=1.0, rsi_5m=1.0), NIVELES
    )

    assert puntuacion.total == pytest.approx(0.960, abs=0.001)
    assert puntuacion.level is SignalLevel.NEAR_MISS


def test_los_casos_limite_del_diseno():
    # Falla solo rsi_1m del todo -> 0.819 -> NEAR_MISS.
    p = score_signal(resultados(support=1.0, rsi_ndx=1.0, rsi_1m=0.0, rsi_5m=1.0), NIVELES)
    assert p.total == pytest.approx(0.818, abs=0.002)
    assert p.level is SignalLevel.NEAR_MISS

    # Falla rsi_ndx del todo -> 0.727 -> PARTIAL.
    p = score_signal(resultados(support=1.0, rsi_ndx=0.0, rsi_1m=1.0, rsi_5m=1.0), NIVELES)
    assert p.total == pytest.approx(0.727, abs=0.002)
    assert p.level is SignalLevel.PARTIAL


def test_el_veto_del_soporte_tumba_a_idle_pese_al_total():
    # Los tres RSI perfectos y el soporte a cero: 0.636 sería PARTIAL...
    puntuacion = score_signal(
        resultados(support=0.0, rsi_ndx=1.0, rsi_1m=1.0, rsi_5m=1.0), NIVELES
    )

    assert puntuacion.total == pytest.approx(0.636, abs=0.002)
    assert puntuacion.level is SignalLevel.IDLE  # ...pero el veto manda


def test_fallar_el_soporte_nunca_alcanza_near_miss():
    # Justo por encima del suelo de veto, para que el veto no intervenga.
    puntuacion = score_signal(
        resultados(support=0.16, rsi_ndx=1.0, rsi_1m=1.0, rsi_5m=1.0), NIVELES
    )

    assert puntuacion.level is SignalLevel.PARTIAL
    assert puntuacion.level is not SignalLevel.NEAR_MISS


def test_los_umbrales_no_dependen_del_numero_de_condiciones():
    # Siete condiciones de peso 1, todas a 0.8: el total sigue siendo 0.8.
    siete = [
        ConditionResult(f"c{i}", 0.0, 0.0, 0.8, 1.0, False, None) for i in range(7)
    ]
    assert score_signal(siete, NIVELES).total == pytest.approx(0.8)


def test_sin_condiciones_el_resultado_es_idle_y_no_divide_por_cero():
    puntuacion = score_signal([], NIVELES)

    assert puntuacion.total == 0.0
    assert puntuacion.level is SignalLevel.IDLE
    assert puntuacion.armed is False


def test_la_carga_util_lleva_valor_crudo_y_puntuacion():
    puntuacion = score_signal(
        [ConditionResult("rsi_1m", 28.4, 30.0, 1.0, 1.0, True, None)], NIVELES
    )

    (fila,) = puntuacion.to_payload()
    assert fila["name"] == "rsi_1m"
    assert fila["raw_value"] == 28.4
    assert fila["threshold"] == 30.0
    assert fila["score"] == 1.0
    assert fila["passed"] is True


def test_un_peso_negativo_revienta_al_puntuar():
    """Un peso negativo saca el total fuera de [0, 1].

    Con eso se cae el argumento de equivalencia que sostiene la garantía capital del
    módulo —que un total alto no puede armar el gatillo— y los umbrales de nivel dejan de
    significar nada. La validación va en `score_signal` y no en `ConditionSpec` porque el
    invariante es del total, no de la descripción de una condición suelta.
    """
    condicion = ConditionResult("x", 1.0, 1.0, 1.0, -1.0, True)
    with pytest.raises(ValueError, match="peso negativo"):
        score_signal([condicion], LevelThresholds())


def test_un_peso_de_cero_se_admite():
    """Cero es "esta condición no pondera", que es una configuración legítima: se sigue
    exigiendo que se cumpla para armar, pero no mueve el total."""
    condiciones = [
        ConditionResult("a", 1.0, 1.0, 1.0, 1.0, True),
        ConditionResult("b", 1.0, 1.0, 0.0, 0.0, True),
    ]
    resultado = score_signal(condiciones, LevelThresholds())
    assert resultado.total == 1.0
    assert resultado.armed is True
