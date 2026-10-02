"""Pruebas del ATR incremental contra una referencia vectorizada."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from bot.core.types import Bar
from bot.indicators.atr import ATR

TZ = dt.timezone.utc


def bars(ohlc: list[tuple[float, float, float]]) -> list[Bar]:
    """Fabrica velas a partir de tríos (máximo, mínimo, cierre)."""
    base = dt.datetime(2026, 3, 10, 9, 30, tzinfo=TZ)
    return [
        Bar("TSLA", "1m", base + dt.timedelta(minutes=i), c, h, l, c, 100)
        for i, (h, l, c) in enumerate(ohlc)
    ]


def atr_reference(ohlc: list[tuple[float, float, float]], period: int = 14) -> list[float | None]:
    """ATR de Wilder en bloque, con siembra por media simple del rango verdadero.

    ohlc   : list[tuple] — tríos (máximo, mínimo, cierre).
    period : int         — periodo.
    return : list[float | None] — un valor por vela.
    """
    frame = pd.DataFrame(ohlc, columns=["high", "low", "close"])
    cierre_previo = frame["close"].shift(1)
    rango = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - cierre_previo).abs(),
            (frame["low"] - cierre_previo).abs(),
        ],
        axis=1,
    ).max(axis=1)

    valores = rango.rolling(period).mean().to_numpy(copy=True)
    for i in range(period, len(frame)):
        if not np.isnan(valores[i - 1]):
            valores[i] = (valores[i - 1] * (period - 1) + rango.iloc[i]) / period
    return [None if np.isnan(v) else float(v) for v in valores]


def test_coincide_con_la_referencia_vectorizada():
    rng = np.random.default_rng(seed=20260825)
    cierres = 100 + np.cumsum(rng.normal(0, 0.5, 120))
    datos = [
        (float(c + abs(rng.normal(0, 0.3))), float(c - abs(rng.normal(0, 0.3))), float(c))
        for c in cierres
    ]

    esperado = atr_reference(datos, period=14)

    atr = ATR(period=14)
    for bar, referencia in zip(bars(datos), esperado, strict=True):
        obtenido = atr.update(bar)
        if referencia is None:
            assert obtenido is None
        else:
            assert obtenido == pytest.approx(referencia, abs=1e-9)


def test_el_rango_verdadero_tiene_en_cuenta_el_hueco_de_apertura():
    # Segunda vela con hueco al alza: su rango verdadero es 105-100 = 5, no 105-104 = 1.
    atr = ATR(period=2)
    for bar in bars([(100.0, 100.0, 100.0), (105.0, 104.0, 104.5), (106.0, 105.0, 105.5)]):
        valor = atr.update(bar)

    assert valor is not None
    assert valor > 1.0


def test_el_indicador_esta_registrado_por_nombre():
    from bot.core.registry import get_indicator

    assert get_indicator("atr") is ATR
