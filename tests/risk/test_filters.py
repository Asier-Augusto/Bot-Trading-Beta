"""Pruebas de los filtros de riesgo, uno por uno y en sus casos límite."""

import datetime as dt

import pytest

from bot.core.context import AccountSnapshot, MarketContext, Position
from bot.core.types import Bar, Intent, Side
from bot.risk.filters import (
    BuyingPowerFilter,
    DuplicateOrderFilter,
    KillSwitchFilter,
    MaxPositionsFilter,
    OnePositionPerSymbolFilter,
    RiskState,
    TradingWindowFilter,
)

TZ = dt.timezone.utc
TS = dt.datetime(2026, 3, 10, 14, 32, tzinfo=TZ)
BAR = Bar("TSLA", "1m", TS, 412.0, 412.0, 412.0, 412.0, 100)


def ctx(entradas: bool = True, franja: str = "regular", cuenta: AccountSnapshot | None = None,
        posicion: Position | None = None) -> MarketContext:
    return MarketContext(
        symbol="TSLA",
        bar=BAR,
        now=TS,
        indicators={},
        account=cuenta or AccountSnapshot(cash=10_000.0, equity=25_000.0, buying_power=50_000.0),
        position=posicion,
        session_label=franja,
        entries_allowed=entradas,
    )


def intent(side: Side = Side.BUY, tag: str = "base") -> Intent:
    return Intent("sig1", "TSLA", side, None, 410.8, "moris:armado", tag)


def estado(**cambios) -> RiskState:
    base = dict(open_positions={}, daily_pnl=0.0, starting_equity=25_000.0,
                pending_order_keys=set())
    base.update(cambios)
    return RiskState(**base)


# --- Ventana horaria ---

def test_la_ventana_bloquea_entradas_fuera_de_franja():
    veredicto = TradingWindowFilter().check(intent(), ctx(entradas=False, franja="pre-cierre"), estado())

    assert veredicto.allowed is False
    assert veredicto.rule == "trading_window"
    assert "pre-cierre" in veredicto.reason


def test_la_ventana_permite_entradas_en_franja():
    assert TradingWindowFilter().check(intent(), ctx(), estado()).allowed is True


def test_la_ventana_nunca_bloquea_una_salida():
    # Entre 15:00 y 15:45 no se abren posiciones, pero cerrarlas debe seguir siendo
    # posible. Confundir ambas cosas dejaría posiciones atrapadas.
    veredicto = TradingWindowFilter().check(
        intent(side=Side.SELL), ctx(entradas=False, franja="pre-cierre"), estado()
    )

    assert veredicto.allowed is True


# --- Kill switch ---

def test_el_kill_switch_bloquea_al_superar_la_perdida_diaria():
    # 3 % de 25.000 son 750; una pérdida de 800 lo dispara.
    veredicto = KillSwitchFilter(max_daily_loss_pct=0.03).check(
        intent(), ctx(), estado(daily_pnl=-800.0)
    )

    assert veredicto.allowed is False
    assert veredicto.rule == "kill_switch"


def test_el_kill_switch_no_salta_justo_en_el_limite():
    veredicto = KillSwitchFilter(max_daily_loss_pct=0.03).check(
        intent(), ctx(), estado(daily_pnl=-750.0)
    )

    assert veredicto.allowed is True


def test_el_kill_switch_nunca_bloquea_una_salida():
    # Si bloqueara las ventas, al saltar dejaría todo abierto: lo contrario de su fin.
    veredicto = KillSwitchFilter(max_daily_loss_pct=0.03).check(
        intent(side=Side.SELL), ctx(), estado(daily_pnl=-5_000.0)
    )

    assert veredicto.allowed is True


# --- Una posición por símbolo ---

def test_una_posicion_por_simbolo():
    posicion = Position("TSLA", 100, 412.0, 410.8)
    veredicto = OnePositionPerSymbolFilter().check(
        intent(), ctx(), estado(open_positions={"TSLA": posicion})
    )

    assert veredicto.allowed is False
    assert veredicto.rule == "one_position_per_symbol"


def test_otro_simbolo_no_estorba():
    posicion = Position("NVDA", 100, 800.0, 790.0)
    veredicto = OnePositionPerSymbolFilter().check(
        intent(), ctx(), estado(open_positions={"NVDA": posicion})
    )

    assert veredicto.allowed is True


# --- Máximo de posiciones simultáneas ---

def test_maximo_de_posiciones_simultaneas():
    abiertas = {s: Position(s, 10, 100.0, 95.0) for s in ("AAPL", "MSFT", "NVDA")}
    veredicto = MaxPositionsFilter(max_positions=3).check(intent(), ctx(), estado(open_positions=abiertas))

    assert veredicto.allowed is False
    assert veredicto.rule == "max_positions"


def test_por_debajo_del_maximo_pasa():
    abiertas = {s: Position(s, 10, 100.0, 95.0) for s in ("AAPL", "MSFT")}
    assert MaxPositionsFilter(max_positions=3).check(
        intent(), ctx(), estado(open_positions=abiertas)
    ).allowed is True


# --- Poder de compra ---

def test_sin_poder_de_compra_se_bloquea():
    cuenta = AccountSnapshot(cash=0.0, equity=25_000.0, buying_power=0.0)
    veredicto = BuyingPowerFilter().check(intent(), ctx(cuenta=cuenta), estado())

    assert veredicto.allowed is False
    assert veredicto.rule == "buying_power"


def test_sin_poder_de_compra_una_venta_sigue_pasando():
    cuenta = AccountSnapshot(cash=0.0, equity=25_000.0, buying_power=0.0)
    assert BuyingPowerFilter().check(
        intent(side=Side.SELL), ctx(cuenta=cuenta), estado()
    ).allowed is True


# --- Anti-duplicado ---

def test_no_se_envia_dos_veces_la_misma_orden():
    pendientes = {("TSLA", "BUY", "base")}
    veredicto = DuplicateOrderFilter().check(intent(), ctx(), estado(pending_order_keys=pendientes))

    assert veredicto.allowed is False
    assert veredicto.rule == "duplicate_order"


def test_otro_tramo_del_mismo_simbolo_no_es_duplicado():
    # Los tramos de la variante mixta son órdenes distintas de la misma señal.
    pendientes = {("TSLA", "BUY", "tramo_a")}
    veredicto = DuplicateOrderFilter().check(
        intent(tag="tramo_b"), ctx(), estado(pending_order_keys=pendientes)
    )

    assert veredicto.allowed is True


# --- Liberación de la clave anti-duplicado (arreglo I5) ---

def test_liberar_la_clave_permite_reenviar_la_misma_orden():
    estado_actual = estado(pending_order_keys={("TSLA", "BUY", "base")})

    estado_actual.release("TSLA", "BUY", "base")

    assert DuplicateOrderFilter().check(intent(), ctx(), estado_actual).allowed is True


def test_liberar_una_clave_inexistente_no_revienta():
    estado_actual = estado()

    estado_actual.release("TSLA", "BUY", "base")  # no debe lanzar excepción

    assert estado_actual.pending_order_keys == set()


def test_liberar_una_clave_no_afecta_a_las_demas_pendientes():
    # Los dos tramos de la variante mixta son claves independientes: soltar uno no
    # debe soltar el otro por error.
    estado_actual = estado(
        pending_order_keys={("TSLA", "BUY", "tramo_a"), ("TSLA", "BUY", "tramo_b")}
    )

    estado_actual.release("TSLA", "BUY", "tramo_a")

    assert DuplicateOrderFilter().check(
        intent(tag="tramo_a"), ctx(), estado_actual
    ).allowed is True
    assert DuplicateOrderFilter().check(
        intent(tag="tramo_b"), ctx(), estado_actual
    ).allowed is False


def test_la_venta_bloqueada_por_clave_pendiente_se_libera_tras_resolverse():
    # El escenario concreto de la avería I5: una orden de venta rechazada dejaba la
    # clave viva para siempre y la posición ya no podía cerrarse nunca.
    estado_actual = estado(pending_order_keys={("TSLA", "SELL", "base")})
    salida = intent(side=Side.SELL)

    bloqueado = DuplicateOrderFilter().check(salida, ctx(), estado_actual)
    assert bloqueado.allowed is False
    assert bloqueado.rule == "duplicate_order"

    estado_actual.release("TSLA", "SELL", "base")

    assert DuplicateOrderFilter().check(salida, ctx(), estado_actual).allowed is True


def test_todos_los_filtros_estan_registrados():
    from bot.core.registry import get_risk_filter

    for nombre in (
        "trading_window", "kill_switch", "one_position_per_symbol",
        "max_positions", "buying_power", "duplicate_order",
    ):
        assert get_risk_filter(nombre) is not None


# --- §7.1: los tramos de una misma entrada cuentan como una sola posición ------------


def test_un_segundo_tramo_de_la_misma_senal_pasa():
    """Sin esto la variante mixta no puede existir: el tramo agresivo se ejecuta un minuto
    antes que el conservador, así que cuando la intención del segundo llega al filtro ya hay
    posición abierta en el símbolo.

    Medido en el backtest dorado antes del arreglo: la mixta abría solo el tramo agresivo y
    el conservador aparecía en `risk.jsonl` bloqueado por esta regla, con lo que la
    comparación entre variantes —el motivo entero de la variante mixta— no se producía.
    """
    estado = RiskState(
        open_positions={"TSLA": Position("TSLA", 24, 412.5, 409.8, "", "sig-A")}
    )
    segundo_tramo = Intent("sig-A", "TSLA", Side.BUY, None, 411.9, "moris", "conservative")

    veredicto = OnePositionPerSymbolFilter().check(segundo_tramo, ctx(), estado)

    assert veredicto.allowed is True


def test_una_entrada_de_otra_senal_sigue_bloqueada():
    """El verdadero propósito del filtro: no acumular posición en el mismo símbolo por
    señales distintas."""
    estado = RiskState(
        open_positions={"TSLA": Position("TSLA", 24, 412.5, 409.8, "", "sig-A")}
    )
    otra = Intent("sig-B", "TSLA", Side.BUY, None, 411.9, "moris", "aggressive")

    veredicto = OnePositionPerSymbolFilter().check(otra, ctx(), estado)

    assert veredicto.allowed is False
    assert "otra señal" in veredicto.reason


def test_una_posicion_sin_identificador_bloquea_cualquier_entrada():
    """`signal_id` vacío significa que los tramos abiertos no comparten señal: no se puede
    afirmar que la entrada nueva sea "otro tramo de lo mismo", y ante la duda no se
    acumula."""
    estado = RiskState(
        open_positions={"TSLA": Position("TSLA", 24, 412.5, 409.8, "", "")}
    )
    entrada = Intent("sig-A", "TSLA", Side.BUY, None, 411.9, "moris", "aggressive")

    assert OnePositionPerSymbolFilter().check(entrada, ctx(), estado).allowed is False
