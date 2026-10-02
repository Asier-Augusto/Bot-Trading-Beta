"""Pruebas del contexto de solo lectura que recibe la estrategia."""

import datetime as dt

import pytest

from bot.contracts.strategy import IndicatorSpec
from bot.core.context import AccountSnapshot, MarketContext, Position, aggregate_positions
from bot.core.types import Bar

TS = dt.datetime(2026, 3, 10, 14, 32, tzinfo=dt.timezone.utc)
BAR = Bar("TSLA", "1m", TS, 410.0, 413.0, 409.0, 412.5, 1_000)
CUENTA = AccountSnapshot(cash=10_000.0, equity=25_000.0, buying_power=50_000.0)


def contexto(**cambios) -> MarketContext:
    base = dict(
        symbol="TSLA",
        bar=BAR,
        now=TS,
        indicators={
            ("rsi", "1m", "TSLA"): 28.4,
            ("rsi", "5m", "TSLA"): 33.1,
            ("rsi", "5m", "ONEQ"): 31.2,
            ("atr", "5m", "TSLA"): 2.10,
        },
        account=CUENTA,
        position=None,
        support_zones=(),
        session_label="regular",
        entries_allowed=True,
        params={},
    )
    base.update(cambios)
    return MarketContext(**base)


def test_acceso_a_indicador_por_nombre_y_marco():
    ctx = contexto()

    assert ctx.indicator("rsi", "1m") == 28.4
    assert ctx.indicator("atr", "5m") == 2.10


def test_acceso_a_indicador_de_otro_simbolo():
    # Así lee Moris la referencia del Nasdaq sin que el contrato la conozca.
    ctx = contexto()

    assert ctx.indicator("rsi", "5m", symbol="ONEQ") == 31.2


def test_indicador_ausente_devuelve_none_en_vez_de_reventar():
    # Durante el calentamiento aún no hay valores, y eso es normal.
    assert contexto().indicator("macd", "5m") is None


def test_el_contexto_es_inmutable():
    import dataclasses

    ctx = contexto()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.symbol = "NVDA"


def test_el_contexto_no_expone_bróker_ni_reloj_del_sistema():
    # La pureza de la estrategia depende de esto: si no hay atributo, no hay tentación.
    campos = {f.name for f in __import__("dataclasses").fields(MarketContext)}

    assert "broker" not in campos
    assert "bus" not in campos
    assert "clock" not in campos


def test_la_especificacion_de_indicador_es_hashable_y_tiene_clave():
    spec = IndicatorSpec("rsi", "5m", symbol="ONEQ", params={"period": 14})

    assert spec.key == ("rsi", "5m", "ONEQ")
    assert len({spec, IndicatorSpec("rsi", "5m", symbol="ONEQ", params={"period": 14})}) == 1


def test_la_especificacion_sin_simbolo_usa_el_del_contexto():
    assert IndicatorSpec("rsi", "1m").symbol is None


# --- La vista agregada de los tramos ------------------------------------------------


def test_aggregate_positions_pondera_la_entrada_por_acciones():
    agregada = aggregate_positions(
        [
            Position("TSLA", 100, 400.0, 390.0, "aggressive", "s1"),
            Position("TSLA", 50, 410.0, 405.0, "conservative", "s1"),
        ]
    )

    assert agregada.shares == 150
    assert agregada.entry_price == pytest.approx(403.3333, abs=1e-4)


def test_aggregate_positions_se_queda_con_el_stop_mas_bajo():
    """El agregado es informativo: cada tramo conserva su propio stop en el bróker, y el
    mínimo es el precio por debajo del cual ya no quedaría ninguno vivo."""
    agregada = aggregate_positions(
        [
            Position("TSLA", 100, 400.0, 390.0, "aggressive", "s1"),
            Position("TSLA", 50, 410.0, 405.0, "conservative", "s1"),
        ]
    )

    assert agregada.stop_price == 390.0


def test_aggregate_positions_conserva_el_identificador_comun():
    """Los tramos de una misma entrada comparten `signal_id` (§7.1): conservarlo permite
    correlacionar la posición agregada con la señal que la abrió."""
    agregada = aggregate_positions(
        [
            Position("TSLA", 100, 400.0, 390.0, "aggressive", "s1"),
            Position("TSLA", 50, 410.0, 405.0, "conservative", "s1"),
        ]
    )

    assert agregada.signal_id == "s1"


def test_aggregate_positions_no_inventa_un_identificador_si_difieren():
    agregada = aggregate_positions(
        [
            Position("TSLA", 100, 400.0, 390.0, "a", "s1"),
            Position("TSLA", 50, 410.0, 405.0, "b", "s2"),
        ]
    )

    assert agregada.signal_id == ""


def test_aggregate_positions_no_lleva_etiqueta():
    """El agregado no es de ningún tramo: darle una haría que una venta lo confundiera con
    uno de ellos."""
    agregada = aggregate_positions([Position("TSLA", 100, 400.0, 390.0, "aggressive", "s1")])

    assert agregada.tag == ""


def test_aggregate_positions_de_una_lista_vacia_es_None():
    assert aggregate_positions([]) is None
