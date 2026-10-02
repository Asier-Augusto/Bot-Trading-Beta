"""Pruebas del dimensionador y de las directivas de tamaño."""

import datetime as dt

import pytest

from bot.core.context import AccountSnapshot, MarketContext, Position
from bot.core.types import Bar, Equity, Fraction, Intent, Multiplier, Risk, Shares, Side
from bot.risk.sizers import PercentEquitySizer

TS = dt.datetime(2026, 3, 10, 14, 32, tzinfo=dt.timezone.utc)
CUENTA = AccountSnapshot(cash=20_000.0, equity=25_000.0, buying_power=50_000.0)
CTX = MarketContext(
    symbol="TSLA",
    bar=Bar("TSLA", "1m", TS, 400.0, 400.0, 400.0, 400.0, 100),
    now=TS,
    indicators={},
    account=CUENTA,
    position=None,
)


def intent(sizing=None, stop=390.0) -> Intent:
    return Intent("sig1", "TSLA", Side.BUY, sizing, stop, "moris", "base")


def test_sin_directiva_usa_el_porcentaje_configurado():
    # 20 % de 25.000 son 5.000; a 400 el precio, 12 acciones.
    assert PercentEquitySizer(pct=0.20).size(intent(), CTX, entry_price=400.0) == 12


def test_directiva_de_acciones_exactas():
    assert PercentEquitySizer(pct=0.20).size(intent(Shares(50)), CTX, 400.0) == 50


def test_directiva_de_porcentaje_de_capital():
    # 25 % de 25.000 son 6.250; a 400, 15 acciones.
    assert PercentEquitySizer(pct=0.20).size(intent(Equity(0.25)), CTX, 400.0) == 15


def test_directiva_de_riesgo_usa_la_distancia_al_stop():
    # 2 % de 25.000 son 500 de riesgo; R = 400 - 390 = 10 -> 50 acciones.
    assert PercentEquitySizer(pct=0.20).size(intent(Risk(0.02)), CTX, 400.0) == 50


def test_directiva_de_riesgo_sin_stop_cae_al_valor_por_defecto():
    # Sin stop no hay R que dividir; se usa el sizer por defecto en vez de reventar.
    assert PercentEquitySizer(pct=0.20).size(intent(Risk(0.02), stop=None), CTX, 400.0) == 12


def test_directiva_de_riesgo_con_stop_por_encima_de_la_entrada():
    # R negativa o nula es un fallo de la estrategia: se cae al tamaño por defecto.
    assert PercentEquitySizer(pct=0.20).size(intent(Risk(0.02), stop=410.0), CTX, 400.0) == 12


def test_multiplicador_escala_el_tamano_por_defecto():
    assert PercentEquitySizer(pct=0.20).size(intent(Multiplier(1.5)), CTX, 400.0) == 18


def test_fraccion_reparte_el_tamano_por_defecto():
    assert PercentEquitySizer(pct=0.20).size(intent(Fraction(0.5)), CTX, 400.0) == 6


def test_precio_no_positivo_devuelve_cero_en_vez_de_dividir_por_cero():
    assert PercentEquitySizer(pct=0.20).size(intent(), CTX, entry_price=0.0) == 0


def test_el_sizer_esta_registrado():
    from bot.core.registry import get_sizer

    assert get_sizer("percent_equity") is PercentEquitySizer


# --- D2: las ventas se dimensionan por la posición abierta ---------------------------


def _contexto_con(tramos):
    """Contexto con unos tramos abiertos y su vista agregada, como lo monta el motor."""
    from bot.core.context import aggregate_positions

    return MarketContext(
        symbol="TSLA",
        bar=Bar("TSLA", "1m", TS, 420.0, 421.0, 419.0, 421.0, 1000),
        now=TS,
        indicators={},
        account=AccountSnapshot(cash=100_000.0, equity=100_000.0, buying_power=100_000.0),
        position=aggregate_positions(tramos),
        positions=tuple(tramos),
    )


def test_una_venta_sin_directiva_vende_las_acciones_de_la_posicion():
    """Antes devolvía `pct x patrimonio / precio`: un número inventado que no tenía nada
    que ver con las acciones en cartera.

    Lo tapaba el bróker simulado, que cierra la posición entera y avisa, así que el
    backtest salía bien mientras `SizeComputed` registraba una cifra falsa. Contra IBKR
    habría sido una orden por la cantidad equivocada.
    """
    ctx = _contexto_con([Position("TSLA", 137, 420.0, 410.0, "base", "s1")])
    venta = Intent("s1", "TSLA", Side.SELL, None, None, "salida", "base")

    assert PercentEquitySizer(pct=0.20).size(venta, ctx, 421.0) == 137


def test_una_venta_de_un_tramo_vende_solo_ese_tramo():
    """La variante mixta abre dos tramos; cerrar uno no puede vender los dos."""
    ctx = _contexto_con(
        [
            Position("TSLA", 60, 420.0, 410.0, "aggressive", "s1"),
            Position("TSLA", 60, 421.0, 419.5, "conservative", "s1"),
        ]
    )
    venta = Intent("s1", "TSLA", Side.SELL, None, None, "salida", "aggressive")

    assert PercentEquitySizer(pct=0.20).size(venta, ctx, 421.0) == 60


def test_una_venta_sin_etiqueta_cierra_todos_los_tramos():
    """Es lo que usan el cierre forzado y las estrategias de un solo tramo."""
    ctx = _contexto_con(
        [
            Position("TSLA", 60, 420.0, 410.0, "aggressive", "s1"),
            Position("TSLA", 60, 421.0, 419.5, "conservative", "s1"),
        ]
    )
    venta = Intent("s1", "TSLA", Side.SELL, None, None, "force_close", "")

    assert PercentEquitySizer(pct=0.20).size(venta, ctx, 421.0) == 120


def test_una_venta_sin_posicion_dimensiona_a_cero():
    """Cero acciones hace que el gestor la bloquee como "el tamaño calculado es cero", que
    es exactamente lo que debe pasar: no hay nada que vender."""
    venta = Intent("s1", "TSLA", Side.SELL, None, None, "salida", "base")

    assert PercentEquitySizer(pct=0.20).size(venta, _contexto_con([]), 421.0) == 0


def test_una_venta_de_un_tramo_que_no_existe_dimensiona_a_cero():
    """NO se cae a la posición entera: vender los dos tramos cuando la estrategia pidió
    cerrar uno sería peor que no vender nada."""
    ctx = _contexto_con([Position("TSLA", 60, 420.0, 410.0, "aggressive", "s1")])
    venta = Intent("s1", "TSLA", Side.SELL, None, None, "salida", "conservative")

    assert PercentEquitySizer(pct=0.20).size(venta, ctx, 421.0) == 0


def test_una_venta_con_directiva_explicita_la_respeta():
    """La directiva manda; el recorte a lo que hay en cartera lo hace el gestor después."""
    ctx = _contexto_con([Position("TSLA", 137, 420.0, 410.0, "base", "s1")])
    venta = Intent("s1", "TSLA", Side.SELL, Shares(50), None, "salida", "base")

    assert PercentEquitySizer(pct=0.20).size(venta, ctx, 421.0) == 50


def test_una_compra_sigue_dimensionando_por_patrimonio():
    """El cambio es solo para las ventas: una compra no mira la cartera."""
    ctx = _contexto_con([])
    compra = Intent("s1", "TSLA", Side.BUY, None, 410.0, "entrada", "base")

    assert PercentEquitySizer(pct=0.20).size(compra, ctx, 400.0) == 50
