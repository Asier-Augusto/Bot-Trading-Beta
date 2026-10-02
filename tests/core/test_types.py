"""Pruebas de los tipos del dominio."""

import dataclasses
import datetime as dt

import pytest

from bot.core.types import Bar, Equity, Intent, Shares, Side

TS = dt.datetime(2026, 3, 10, 14, 32, tzinfo=dt.timezone.utc)


def _bar(**cambios) -> Bar:
    base = dict(
        symbol="TSLA", timeframe="1m", ts=TS,
        open=410.0, high=413.0, low=409.0, close=412.5, volume=1_000,
    )
    base.update(cambios)
    return Bar(**base)


def test_la_vela_es_inmutable():
    bar = _bar()

    with pytest.raises(dataclasses.FrozenInstanceError):
        bar.close = 999.0


def test_rango_de_la_vela():
    assert _bar(high=413.0, low=409.0).range == pytest.approx(4.0)


def test_cierre_en_el_tercio_alto():
    # Rango 409-413; el tercio alto empieza en 411.667.
    assert _bar(close=412.5).closes_in_upper_third is True
    assert _bar(close=410.0).closes_in_upper_third is False


def test_vela_sin_rango_no_cierra_en_tercio_alto():
    # Una vela plana no debe provocar división por cero.
    assert _bar(high=410.0, low=410.0, close=410.0).closes_in_upper_third is False


def test_la_intencion_lleva_el_identificador_de_correlacion():
    intent = Intent(
        signal_id="7f3a",
        symbol="TSLA",
        side=Side.BUY,
        sizing=Equity(0.25),
        stop_price=408.1,
        reason="moris:agresiva",
        tag="tramo_a",
    )

    assert intent.signal_id == "7f3a"
    assert intent.sizing == Equity(0.25)


def test_las_directivas_de_tamano_validan_su_rango():
    with pytest.raises(ValueError, match="entre 0 y 1"):
        Equity(1.5)
    with pytest.raises(ValueError, match="positivo"):
        Shares(0)
