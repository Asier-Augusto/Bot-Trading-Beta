"""Pruebas del RSI incremental contra una referencia vectorizada con pandas."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from bot.core.types import Bar
from bot.indicators.rsi import RSI

TZ = dt.timezone.utc


def bars(closes: list[float]) -> list[Bar]:
    """Fabrica velas con los cierres dados; el resto de campos no interviene en el RSI."""
    base = dt.datetime(2026, 3, 10, 9, 30, tzinfo=TZ)
    return [
        Bar("TSLA", "1m", base + dt.timedelta(minutes=i), c, c, c, c, 100)
        for i, c in enumerate(closes)
    ]


def rsi_reference(closes: list[float], period: int = 14) -> list[float | None]:
    """RSI de Wilder calculado en bloque con pandas, con siembra por media simple.

    closes : list[float] — serie de cierres.
    period : int         — periodo del indicador.
    return : list[float | None] — un valor por cierre; None hasta tener datos bastantes.
    """
    serie = pd.Series(closes, dtype=float)
    delta = serie.diff()
    ganancias = delta.clip(lower=0.0)
    perdidas = (-delta).clip(lower=0.0)

    media_g = ganancias.rolling(period).mean()
    media_p = perdidas.rolling(period).mean()

    # Suavizado de Wilder a partir de la primera media simple.
    g = media_g.to_numpy(copy=True)
    p = media_p.to_numpy(copy=True)
    for i in range(period + 1, len(serie)):
        g[i] = (g[i - 1] * (period - 1) + ganancias.iloc[i]) / period
        p[i] = (p[i - 1] * (period - 1) + perdidas.iloc[i]) / period

    salida: list[float | None] = []
    for i in range(len(serie)):
        if np.isnan(g[i]) or np.isnan(p[i]):
            salida.append(None)
        elif p[i] == 0:
            salida.append(100.0)
        else:
            rs = g[i] / p[i]
            salida.append(100 - 100 / (1 + rs))
    return salida


def test_no_hay_valor_hasta_tener_periodo_suficiente():
    rsi = RSI(period=14)

    for bar in bars([100.0] * 14):
        valor = rsi.update(bar)

    assert rsi.ready is False
    assert valor is None


def test_coincide_con_la_referencia_vectorizada():
    rng = np.random.default_rng(seed=20260825)
    cierres = list(100 + np.cumsum(rng.normal(0, 0.5, 200)))

    esperado = rsi_reference(cierres, period=14)

    rsi = RSI(period=14)
    for bar, referencia in zip(bars(cierres), esperado, strict=True):
        obtenido = rsi.update(bar)
        if referencia is None:
            assert obtenido is None
        else:
            assert obtenido == pytest.approx(referencia, abs=1e-9)


def test_serie_solo_alcista_satura_en_cien():
    rsi = RSI(period=14)
    for bar in bars([100.0 + i for i in range(30)]):
        valor = rsi.update(bar)

    assert valor == pytest.approx(100.0)


def test_serie_solo_bajista_satura_en_cero():
    rsi = RSI(period=14)
    for bar in bars([200.0 - i for i in range(30)]):
        valor = rsi.update(bar)

    assert valor == pytest.approx(0.0)


def test_el_indicador_esta_registrado_por_nombre():
    from bot.core.registry import get_indicator

    assert get_indicator("rsi") is RSI
