"""Pruebas del vocabulario de eventos."""

import datetime as dt
import json

from bot.core.events import (
    SCHEMA_VERSION,
    BarClosed,
    Fill,
    Priority,
    SystemEvent,
)
from bot.core.types import Bar, Side

TS = dt.datetime(2026, 3, 10, 14, 32, tzinfo=dt.timezone.utc)
BAR = Bar("TSLA", "1m", TS, 410.0, 413.0, 409.0, 412.5, 1_000)


def test_todo_evento_lleva_los_campos_comunes():
    evento = BarClosed(ts=TS, bar=BAR)

    assert evento.kind == "BarClosed"
    assert evento.schema_version == SCHEMA_VERSION
    assert evento.signal_id == ""


def test_serializacion_es_json_puro():
    evento = BarClosed(ts=TS, bar=BAR, signal_id="7f3a", config_hash="abc123")

    datos = evento.to_dict()
    json.dumps(datos)  # no debe lanzar

    assert datos["kind"] == "BarClosed"
    assert datos["ts"] == TS.isoformat()
    assert datos["signal_id"] == "7f3a"
    assert datos["bar"]["close"] == 412.5
    assert datos["bar"]["ts"] == TS.isoformat()


def test_prioridades_de_contrapresion():
    # Las velas se pueden descartar bajo presión; las ejecuciones jamás.
    assert BarClosed(ts=TS, bar=BAR).priority is Priority.DROPPABLE
    assert Fill(
        ts=TS, symbol="TSLA", side=Side.BUY, shares=100,
        price=412.5, order_id="o1", signal_id="7f3a",
    ).priority is Priority.CRITICAL
    assert SystemEvent(ts=TS, level="ERROR", message="x").priority is Priority.CRITICAL


def test_el_evento_es_inmutable():
    import dataclasses

    import pytest

    evento = SystemEvent(ts=TS, level="INFO", message="arranque")
    with pytest.raises(dataclasses.FrozenInstanceError):
        evento.message = "otro"


def test_el_rechazo_de_orden_identifica_la_orden_y_su_motivo():
    # Sin `order_id` el motor no podría liberar la clave anti-duplicado de ESA orden,
    # que es justo para lo que existe este evento.
    from bot.core.events import OrderRejected

    evento = OrderRejected(
        ts=TS, order_id="o1", symbol="TSLA", reason="sin posición abierta", signal_id="7f3a"
    )

    assert evento.kind == "OrderRejected"
    assert evento.order_id == "o1"
    assert evento.symbol == "TSLA"
    assert evento.reason == "sin posición abierta"
    assert evento.priority is Priority.CRITICAL
    assert json.dumps(evento.to_dict())


def test_la_cancelacion_de_orden_identifica_la_orden_y_su_motivo():
    from bot.core.events import OrderCancelled

    evento = OrderCancelled(
        ts=TS, order_id="o1", symbol="TSLA", reason="retirada antes de ejecutarse"
    )

    assert evento.kind == "OrderCancelled"
    assert evento.order_id == "o1"
    assert evento.symbol == "TSLA"
    assert evento.priority is Priority.CRITICAL
    assert json.dumps(evento.to_dict())


def test_el_rechazo_y_la_cancelacion_son_inmutables():
    import dataclasses

    import pytest

    from bot.core.events import OrderCancelled, OrderRejected

    for evento in (
        OrderRejected(ts=TS, order_id="o1", symbol="TSLA", reason="x"),
        OrderCancelled(ts=TS, order_id="o1", symbol="TSLA", reason="x"),
    ):
        with pytest.raises(dataclasses.FrozenInstanceError):
            evento.reason = "otro"
