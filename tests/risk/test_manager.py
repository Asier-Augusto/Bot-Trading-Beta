"""Pruebas del gestor: la cadena de filtros más el recorte de tamaño."""

import datetime as dt

import pytest

from bot.core.context import AccountSnapshot, MarketContext, Position
from bot.core.types import Bar, Intent, Shares, Side
from bot.risk.filters import (
    BuyingPowerFilter,
    DuplicateOrderFilter,
    KillSwitchFilter,
    OnePositionPerSymbolFilter,
    RiskState,
    TradingWindowFilter,
)
from bot.risk.manager import RiskManager
from bot.risk.sizers import PercentEquitySizer

TS = dt.datetime(2026, 3, 10, 14, 32, tzinfo=dt.timezone.utc)


def ctx(
    buying_power: float = 50_000.0,
    entradas: bool = True,
    tramos: tuple[Position, ...] = (),
) -> MarketContext:
    """Contexto de prueba.

    `position` y `positions` se derivan de los mismos tramos a propósito: el motor los saca
    los dos del bróker, y un contexto con posición agregada pero sin tramos es un estado
    que no puede darse — construirlo en una prueba haría que la prueba comprobara algo que
    la realidad nunca produce.
    """
    from bot.core.context import aggregate_positions

    return MarketContext(
        symbol="TSLA",
        bar=Bar("TSLA", "1m", TS, 400.0, 400.0, 400.0, 400.0, 100),
        now=TS,
        indicators={},
        account=AccountSnapshot(cash=20_000.0, equity=25_000.0, buying_power=buying_power),
        position=aggregate_positions(tramos),
        positions=tramos,
        session_label="regular",
        entries_allowed=entradas,
    )


def gestor(state: RiskState | None = None) -> RiskManager:
    return RiskManager(
        filters=[
            TradingWindowFilter(),
            KillSwitchFilter(max_daily_loss_pct=0.03),
            OnePositionPerSymbolFilter(),
            BuyingPowerFilter(),
        ],
        sizer=PercentEquitySizer(pct=0.20),
        state=state or RiskState(starting_equity=25_000.0),
    )


def intent(sizing=None) -> Intent:
    return Intent("sig1", "TSLA", Side.BUY, sizing, 390.0, "moris:armado", "base")


def test_una_intencion_limpia_se_convierte_en_orden():
    decision = gestor().evaluate(intent(), ctx())

    assert decision.approved is True
    assert decision.order is not None
    assert decision.order.shares == 12
    assert decision.order.signal_id == "sig1"
    assert decision.order.stop_price == 390.0


def test_el_primer_filtro_que_bloquea_es_el_que_se_registra():
    estado = RiskState(starting_equity=25_000.0, daily_pnl=-5_000.0)

    decision = gestor(estado).evaluate(intent(), ctx(entradas=False))

    assert decision.approved is False
    # La ventana horaria va antes que el kill switch en la cadena.
    assert decision.blocked_by == "trading_window"
    assert decision.order is None


def test_el_riesgo_recorta_el_tamano_al_poder_de_compra():
    # Se piden 100 acciones a 400 = 40.000, pero solo hay 10.000 de poder de compra.
    decision = gestor().evaluate(intent(Shares(100)), ctx(buying_power=10_000.0))

    assert decision.approved is True
    assert decision.requested_shares == 100
    assert decision.final_shares == 25
    assert decision.clipped_by == "buying_power"
    assert "recortado" in decision.reason


def test_si_el_recorte_deja_cero_acciones_se_anula():
    decision = gestor().evaluate(intent(Shares(100)), ctx(buying_power=100.0))

    assert decision.approved is False
    assert decision.blocked_by == "buying_power"


def test_una_posicion_abierta_bloquea_la_entrada():
    estado = RiskState(
        starting_equity=25_000.0,
        open_positions={"TSLA": Position("TSLA", 10, 400.0, 390.0)},
    )

    decision = gestor(estado).evaluate(intent(), ctx())

    assert decision.approved is False
    assert decision.blocked_by == "one_position_per_symbol"


def test_una_salida_atraviesa_la_cadena_aunque_todo_este_bloqueado():
    estado = RiskState(
        starting_equity=25_000.0,
        daily_pnl=-10_000.0,
        open_positions={"TSLA": Position("TSLA", 10, 400.0, 390.0)},
    )
    salida = Intent("sig1", "TSLA", Side.SELL, Shares(10), None, "force_close", "base")
    abierta = (Position("TSLA", 10, 400.0, 390.0, "base", "sig1"),)

    decision = gestor(estado).evaluate(salida, ctx(entradas=False, tramos=abierta))

    assert decision.approved is True
    assert decision.order.shares == 10


def test_dos_ordenes_distintas_reciben_identificadores_distintos():
    """Distintas por señal, sentido, etiqueta o instante: cualquiera de los cuatro basta."""
    g = gestor()
    a = g.evaluate(intent(), ctx())
    g.state.open_positions.clear()
    g.state.pending_order_keys.clear()
    otra = Intent("sig2", "TSLA", Side.BUY, Shares(12), 390.0, "prueba", "base")
    b = g.evaluate(otra, ctx())

    assert a.order.order_id != b.order.order_id


def test_la_misma_orden_evaluada_dos_veces_recibe_el_mismo_identificador():
    """El identificador es determinista a propósito (ver `bot.core.ids`): con `uuid4`, dos
    corridas idénticas del backtest producían operaciones que no se podían casar una a
    una, y esa comparación es justo lo que da las cifras de agresiva contra conservadora
    sobre las mismas señales."""
    g = gestor()
    a = g.evaluate(intent(), ctx())
    g.state.open_positions.clear()
    g.state.pending_order_keys.clear()
    b = g.evaluate(intent(), ctx())

    assert a.order.order_id == b.order.order_id


def test_release_order_permite_reenviar_tras_resolverse_la_orden():
    # Escenario de la avería I5: sin liberar la clave, un rechazo del bróker dejaría
    # la posición sin poder cerrarse nunca.
    g = RiskManager(
        filters=[DuplicateOrderFilter()],
        sizer=PercentEquitySizer(pct=0.20),
        state=RiskState(starting_equity=25_000.0),
    )

    primera = g.evaluate(intent(), ctx())
    assert primera.approved is True

    segunda = g.evaluate(intent(), ctx())
    assert segunda.approved is False
    assert segunda.blocked_by == "duplicate_order"

    g.release_order(primera.order)

    tercera = g.evaluate(intent(), ctx())
    assert tercera.approved is True


# --- D2: el recorte de las ventas ----------------------------------------------------


def test_una_venta_de_mas_acciones_de_las_que_hay_se_recorta():
    """Una venta por más acciones de las que se tienen es un corto accidental, y el Plan 1
    no modela cortos."""
    abierta = (Position("TSLA", 137, 400.0, 390.0, "base", "sig1"),)
    salida = Intent("sig1", "TSLA", Side.SELL, Shares(500), None, "salida", "base")

    decision = gestor().evaluate(salida, ctx(tramos=abierta))

    assert decision.approved is True
    assert decision.final_shares == 137
    assert decision.clipped_by == "position"


def test_una_venta_no_se_recorta_por_poder_de_compra():
    """Vender no consume poder de compra. Aplicarle ese recorte dejaría posiciones sin
    poder cerrarse cuando la cuenta está invertida — justo cuando más urge cerrarlas."""
    abierta = (Position("TSLA", 100, 400.0, 390.0, "base", "sig1"),)
    salida = Intent("sig1", "TSLA", Side.SELL, None, None, "salida", "base")

    decision = gestor().evaluate(salida, ctx(buying_power=0.0, tramos=abierta))

    assert decision.approved is True
    assert decision.final_shares == 100
    assert decision.clipped_by == ""


def test_una_venta_sin_nada_abierto_se_bloquea_por_tamano_cero():
    salida = Intent("sig1", "TSLA", Side.SELL, None, None, "salida", "base")

    decision = gestor().evaluate(salida, ctx())

    assert decision.approved is False
    assert decision.final_shares == 0


def test_el_recorte_de_una_venta_dice_cuantas_acciones_habia():
    """El motivo va al log: sin los números concretos, un recorte es indistinguible de un
    fallo de la estrategia."""
    abierta = (Position("TSLA", 20, 400.0, 390.0, "base", "sig1"),)
    salida = Intent("sig1", "TSLA", Side.SELL, Shares(50), None, "salida", "base")

    decision = gestor().evaluate(salida, ctx(tramos=abierta))

    assert "50" in decision.reason and "20" in decision.reason


# --- Entrada con limitada: no pagar mas de lo que la senal vio -------------------------


def test_sin_pedirlo_las_ordenes_siguen_siendo_a_mercado():
    """Quien no active la limitada no se entera de que existe."""
    assert gestor().evaluate(intent(), ctx()).order.limit_price is None


def test_con_la_limitada_activada_el_limite_es_EL_PRECIO_QUE_LA_SENAL_VIO():
    """Ese es el punto entero: la senal se disparo viendo un precio, y la orden a mercado
    paga el que haya en la apertura de la vela siguiente, sea cual sea. El limite dice "no
    mas de lo que vi" --MEDIDO el 2026-09-02: 3,97 de los 6,43 de coste por operacion son
    deslizamiento, el 62 %--."""
    g = gestor()
    g.entry_limit = True
    contexto = ctx()

    decision = g.evaluate(intent(), contexto)

    assert decision.order.limit_price == pytest.approx(contexto.bar.close)


def test_la_limitada_no_se_aplica_a_las_ventas():
    """Salir es urgente: una venta que no ejecuta deja la posicion abierta contra su propio
    motivo de salida, que es la peor forma de ahorrarse unos centimos."""
    estado = RiskState(starting_equity=25_000.0,
                       open_positions={"TSLA": Position("TSLA", 10, 400.0, 390.0)})
    g = gestor(estado)
    g.entry_limit = True
    salida = Intent("sig1", "TSLA", Side.SELL, Shares(10), None, "moris:salida", "base")
    abierta = (Position("TSLA", 10, 400.0, 390.0, "base", "sig1"),)

    decision = g.evaluate(salida, ctx(tramos=abierta))

    assert decision.approved is True
    assert decision.order.limit_price is None
