"""Pruebas del acumulador de IDLE."""

import datetime as dt

from bot.core.idle import IdleAccumulator
from bot.core.scoring import ConditionResult, LevelThresholds, score_signal

TZ = dt.timezone.utc
NIVELES = LevelThresholds()


def _t(minuto: int) -> dt.datetime:
    return dt.datetime(2026, 3, 10, 10, 0, tzinfo=TZ) + dt.timedelta(minutes=minuto)


def _score(total: float, bloqueada: str = "support"):
    """Puntuación de prueba con un valor total controlado y una condición sin cumplir."""
    return score_signal(
        [
            ConditionResult(bloqueada, 0.0, 1.0, total, 1.0, False),
        ],
        NIVELES,
    )


def test_acumula_por_simbolo_y_no_los_mezcla():
    acumulador = IdleAccumulator(window_minutes=60)
    acumulador.add("TSLA", _t(0), _score(0.2))
    acumulador.add("TSLA", _t(1), _score(0.4))
    acumulador.add("NVDA", _t(1), _score(0.1))

    resumenes = {r.symbol: r for r in acumulador.flush(_t(2))}

    assert resumenes["TSLA"].bars == 2
    assert resumenes["NVDA"].bars == 1


def test_la_media_y_el_maximo_salen_de_las_velas_acumuladas():
    acumulador = IdleAccumulator(window_minutes=60)
    acumulador.add("TSLA", _t(0), _score(0.2))
    acumulador.add("TSLA", _t(1), _score(0.6))

    resumen = acumulador.flush(_t(2))[0]

    assert resumen.mean_total == 0.4
    assert resumen.max_total == 0.6


def test_cuenta_cuantas_veces_bloqueo_cada_condicion():
    """La pregunta del §3.3: ¿el bot no opera porque el mercado no da o porque un umbral
    está mal puesto? La responde este recuento y nada más."""
    acumulador = IdleAccumulator(window_minutes=60)
    acumulador.add("TSLA", _t(0), _score(0.2, "support"))
    acumulador.add("TSLA", _t(1), _score(0.3, "support"))
    acumulador.add("TSLA", _t(2), _score(0.3, "rsi_ndx"))

    resumen = acumulador.flush(_t(3))[0]

    assert resumen.blocked_by == {"support": 2, "rsi_ndx": 1}


def test_toca_volcar_cuando_pasa_la_ventana():
    acumulador = IdleAccumulator(window_minutes=60)
    acumulador.add("TSLA", _t(0), _score(0.2))

    assert acumulador.due(_t(59)) is False
    assert acumulador.due(_t(60)) is True


def test_volcar_reinicia_los_contadores():
    acumulador = IdleAccumulator(window_minutes=60)
    acumulador.add("TSLA", _t(0), _score(0.2))
    acumulador.flush(_t(60))

    assert acumulador.flush(_t(120)) == []


def test_un_simbolo_sin_velas_no_produce_resumen():
    """Una línea de ceros no informa de nada y sí ensucia el fichero."""
    acumulador = IdleAccumulator(window_minutes=60)
    assert acumulador.flush(_t(0)) == []


def test_la_ventana_empieza_en_la_primera_vela_acumulada():
    acumulador = IdleAccumulator(window_minutes=60)
    assert acumulador.window_start is None
    acumulador.add("TSLA", _t(5), _score(0.2))
    assert acumulador.window_start == _t(5)


def test_symbols_solo_lista_los_que_tienen_velas():
    acumulador = IdleAccumulator(window_minutes=60)
    acumulador.add("TSLA", _t(0), _score(0.2))
    assert acumulador.symbols() == {"TSLA"}
