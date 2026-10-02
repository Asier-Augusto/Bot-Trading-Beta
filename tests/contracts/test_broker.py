"""El contrato del bróker es una barrera, no documentación.

Un bróker sin `close_all` deja el cierre forzado de las 15:45 sin ejecutar. Antes eso
era un `hasattr` que devolvía False en silencio; ahora es un TypeError al construirlo.
"""

import pytest

from bot.contracts.broker import BarDrivenBroker, Broker


class _BrokerSinCerrarTodo(Broker):
    """Bróker al que le falta `close_all` a propósito."""

    def submit(self, order):
        return order.order_id

    def cancel(self, order_id):
        return []

    def positions(self):
        return []

    def account(self):
        return None

    def modify_stop(self, symbol, tag, new_stop):
        return []


class _BrokerCompleto(Broker):
    """Bróker síncrono mínimo que cumple el contrato entero."""

    def submit(self, order):
        return ""

    def cancel(self, order_id):
        return []

    def positions(self):
        return []

    def account(self):
        return None

    def close_all(self, bars, reason):
        return []

    def modify_stop(self, symbol, tag, new_stop):
        return []


def test_un_broker_sin_close_all_no_se_puede_instanciar():
    with pytest.raises(TypeError, match="close_all"):
        _BrokerSinCerrarTodo()


def test_drain_events_tiene_valor_por_defecto_vacio():
    # Un bróker síncrono como el simulado no entrega nada por esta vía; uno asíncrono
    # (IBKR) sí. Que el defecto sea `[]` es lo que permite que el motor la llame siempre.
    assert _BrokerCompleto().drain_events() == []


def test_bar_driven_broker_exige_on_bar():
    class _SinOnBar(BarDrivenBroker):
        def submit(self, order):
            return ""

        def cancel(self, order_id):
            return []

        def positions(self):
            return []

        def account(self):
            return None

        def close_all(self, bars, reason):
            return []

        def modify_stop(self, symbol, tag, new_stop):
            return []

    with pytest.raises(TypeError, match="on_bar"):
        _SinOnBar()


def test_el_broker_simulado_es_dirigido_por_velas():
    from bot.adapters.brokers.simulated import SimulatedBroker

    assert issubclass(SimulatedBroker, BarDrivenBroker)
