"""Pruebas del bróker simulado y su modelo de ejecución conservador."""

import datetime as dt

import pytest

from bot.adapters.brokers.simulated import ExecutionModel, SimulatedBroker
from bot.core.events import PositionClosed
from bot.core.types import Bar, Order, Side, Timing

TZ = dt.timezone.utc
SIN_COSTES = ExecutionModel(slippage_pct=0.0, commission_per_share=0.0, min_commission=0.0)


def bar(minuto: int, o: float, h: float, l: float, c: float) -> Bar:
    ts = dt.datetime(2026, 3, 10, 14, minuto, tzinfo=TZ)
    return Bar("TSLA", "1m", ts, o, h, l, c, 1_000)


def bar_de(symbol: str, minuto: int, o: float, h: float, l: float, c: float) -> Bar:
    ts = dt.datetime(2026, 3, 10, 14, minuto, tzinfo=TZ)
    return Bar(symbol, "1m", ts, o, h, l, c, 1_000)


def compra(shares: int = 10, stop: float | None = 390.0) -> Order:
    return Order("o1", "TSLA", Side.BUY, shares, stop, "sig1", "base")


def test_la_orden_no_se_ejecuta_en_la_vela_que_la_disparo():
    # Ejecutar al precio que disparó la señal es la mentira clásica del backtest.
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(compra())

    assert broker.positions() == []


def test_se_ejecuta_en_la_apertura_de_la_vela_siguiente():
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(compra())

    eventos = broker.on_bar(bar(33, o=402.0, h=405.0, l=401.0, c=404.0))

    assert len(eventos) == 1
    assert eventos[0].price == pytest.approx(402.0)
    posicion = broker.positions()[0]
    assert posicion.shares == 10
    assert posicion.entry_price == pytest.approx(402.0)


def test_el_deslizamiento_empeora_el_precio_en_ambos_sentidos():
    modelo = ExecutionModel(slippage_pct=0.0002, commission_per_share=0.0, min_commission=0.0)
    broker = SimulatedBroker(starting_cash=100_000.0, execution=modelo)

    broker.submit(compra())
    (ejecucion,) = broker.on_bar(bar(33, o=400.0, h=405.0, l=399.0, c=404.0))

    # Comprando se paga más: 400 * 1.0002.
    assert ejecucion.price == pytest.approx(400.08)


def test_la_comision_respeta_el_minimo():
    modelo = ExecutionModel(slippage_pct=0.0, commission_per_share=0.005, min_commission=1.0)
    broker = SimulatedBroker(starting_cash=100_000.0, execution=modelo)

    broker.submit(compra(shares=10))
    broker.on_bar(bar(33, o=400.0, h=405.0, l=399.0, c=404.0))

    # 10 acciones a 0.005 son 0.05, por debajo del mínimo de 1.
    assert broker.account().cash == pytest.approx(100_000.0 - 4_000.0 - 1.0)


def test_el_stop_se_evalua_por_el_recorrido_completo_de_la_vela():
    # El mínimo perfora el stop aunque el cierre quede por encima: hay que salir.
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(compra(stop=395.0))
    broker.on_bar(bar(33, o=400.0, h=402.0, l=399.0, c=401.0))

    eventos = broker.on_bar(bar(34, o=400.0, h=401.0, l=390.0, c=399.0))

    cierres = [e for e in eventos if type(e).__name__ == "PositionClosed"]
    assert len(cierres) == 1
    assert cierres[0].exit_reason == "stop"
    assert cierres[0].exit_price == pytest.approx(395.0)
    assert broker.positions() == []


def test_un_hueco_bajo_el_stop_ejecuta_en_la_apertura_no_en_el_stop():
    # Si la vela abre ya por debajo, el stop no te salva al precio prometido.
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(compra(stop=395.0))
    broker.on_bar(bar(33, o=400.0, h=402.0, l=399.0, c=401.0))

    eventos = broker.on_bar(bar(34, o=380.0, h=385.0, l=378.0, c=384.0))

    (cierre,) = [e for e in eventos if type(e).__name__ == "PositionClosed"]
    assert cierre.exit_price == pytest.approx(380.0)


def test_el_cierre_registra_pnl_multiplo_de_r_y_excursiones():
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(compra(shares=10, stop=390.0))
    broker.on_bar(bar(33, o=400.0, h=402.0, l=399.0, c=401.0))
    broker.on_bar(bar(34, o=401.0, h=410.0, l=396.0, c=408.0))

    (cierre,) = [e for e in broker.close_all({"TSLA": bar(35, 410.0, 412.0, 409.0, 410.0)}, "force_close")]

    assert cierre.pnl == pytest.approx(100.0)          # (410 - 400) * 10
    assert cierre.r_multiple == pytest.approx(1.0)     # R = 400 - 390 = 10
    assert cierre.mae == pytest.approx(4.0)            # peor punto: 396 -> 4 por acción
    assert cierre.mfe == pytest.approx(12.0)           # mejor punto: 412 -> 12 por acción
    assert cierre.exit_reason == "force_close"
    assert cierre.signal_id == "sig1"


def test_cancelar_una_orden_pendiente_evita_su_ejecucion():
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    order_id = broker.submit(compra())

    broker.cancel(order_id)
    eventos = broker.on_bar(bar(33, o=402.0, h=405.0, l=401.0, c=404.0))

    assert eventos == []
    assert broker.positions() == []


def test_el_broker_esta_registrado():
    from bot.core.registry import get_broker

    assert get_broker("simulated") is SimulatedBroker


def test_el_seguimiento_de_excursiones_no_contamina_otros_simbolos():
    # Dos posiciones abiertas a la vez, en símbolos distintos. Una vela extrema de
    # TSLA no debe tocar el high_water/low_water de la posición de NVDA: cada bróker
    # solo recibe velas de UN símbolo por llamada a on_bar, así que solo la posición
    # de ese símbolo puede haberse movido.
    broker = SimulatedBroker(starting_cash=200_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 10, 200.0, "sig_tsla", "base"))
    broker.submit(Order("o2", "NVDA", Side.BUY, 5, 700.0, "sig_nvda", "base"))

    broker.on_bar(bar_de("TSLA", 33, 400.0, 402.0, 399.0, 401.0))
    broker.on_bar(bar_de("NVDA", 33, 800.0, 802.0, 799.0, 801.0))

    # Vela extremadamente ancha de TSLA: si el bug estuviera presente, el low_water
    # de NVDA caería hasta 300 y su MAE se dispararía a 500.
    broker.on_bar(bar_de("TSLA", 34, 401.0, 500.0, 300.0, 450.0))

    # close_all cierra TODAS las posiciones abiertas (TSLA y NVDA), cada una a su
    # propia vela (mapa símbolo -> vela); nos interesa solo el cierre de NVDA para
    # comprobar que sus excursiones no se contaminaron durante los on_bar anteriores.
    velas_de_cierre = {
        "TSLA": bar_de("TSLA", 35, 450.0, 452.0, 448.0, 452.0),
        "NVDA": bar_de("NVDA", 35, 801.0, 803.0, 800.0, 802.0),
    }
    cierres = broker.close_all(velas_de_cierre, "force_close")
    cierre_nvda = next(c for c in cierres if c.symbol == "NVDA")

    assert cierre_nvda.mae == pytest.approx(1.0)   # peor punto propio de NVDA: 799
    assert cierre_nvda.mfe == pytest.approx(3.0)    # mejor punto propio de NVDA: 803


def test_una_venta_enviada_por_submit_cierra_la_posicion():
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(compra(shares=10, stop=390.0))
    broker.on_bar(bar(33, o=400.0, h=402.0, l=399.0, c=401.0))

    broker.submit(Order("o2", "TSLA", Side.SELL, 10, None, "sig1", "base"))
    eventos = broker.on_bar(bar(34, o=405.0, h=406.0, l=404.0, c=405.0))

    fills = [e for e in eventos if type(e).__name__ == "Fill"]
    cierres = [e for e in eventos if type(e).__name__ == "PositionClosed"]

    assert len(fills) == 1
    assert fills[0].price == pytest.approx(405.0)
    assert len(cierres) == 1
    assert cierres[0].exit_price == pytest.approx(405.0)
    assert cierres[0].pnl == pytest.approx(50.0)  # (405 - 400) * 10
    assert cierres[0] in broker.closed_trades

    assert broker.positions() == []
    cuenta = broker.account()
    # 100_000 - 4_000 (compra) + 4_050 (venta), sin duplicar ni el efectivo ni el
    # valor invertido.
    assert cuenta.cash == pytest.approx(100_050.0)
    assert cuenta.equity == pytest.approx(100_050.0)


def test_una_venta_sin_posicion_abierta_produce_un_rechazo():
    # El Plan 1 es solo largo: una venta sin posición no puede fabricar un corto que
    # nada en el bróker sabe seguir. No se ejecuta, pero tampoco se descarta en
    # silencio: se devuelve un OrderRejected con el `order_id`, que es lo único que
    # permite al motor liberar la clave anti-duplicado de ESA orden. Un aviso de
    # Python no sirve para eso, porque no llega al motor.
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.SELL, 10, None, "sig1", "base"))

    eventos = broker.on_bar(bar(33, o=400.0, h=402.0, l=399.0, c=401.0))

    (rechazo,) = eventos
    assert type(rechazo).__name__ == "OrderRejected"
    assert rechazo.order_id == "o1"
    assert rechazo.symbol == "TSLA"
    assert rechazo.signal_id == "sig1"
    assert "sin tramo abierto" in rechazo.reason
    assert broker.positions() == []
    assert broker.account().cash == pytest.approx(100_000.0)


def test_cancelar_devuelve_el_evento_de_cancelacion_de_esa_orden():
    # El bróker no ve el bus: la única forma de que la cancelación llegue al motor es
    # devolverla, igual que `on_bar` devuelve sus ejecuciones.
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    order_id = broker.submit(compra())

    eventos = broker.cancel(order_id)

    (cancelacion,) = eventos
    assert type(cancelacion).__name__ == "OrderCancelled"
    assert cancelacion.order_id == order_id
    assert cancelacion.symbol == "TSLA"
    assert cancelacion.signal_id == "sig1"


def test_cancelar_una_orden_desconocida_no_produce_evento():
    # Cancelar de más nunca debe reventar ni inventar un evento de una orden que el
    # bróker no tiene: sin orden retirada no hay nada que resolver.
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)

    assert broker.cancel("no-existe") == []


def test_una_venta_parcial_cierra_la_posicion_completa_con_aviso():
    # El Plan 1 no modela cierres parciales: en vez de dejar un remanente cuyas
    # excursiones no se podrían seguir, se liquida la posición completa y se avisa
    # del descuadre entre lo pedido y lo que había abierto.
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(compra(shares=10, stop=390.0))
    broker.on_bar(bar(33, o=400.0, h=402.0, l=399.0, c=401.0))

    broker.submit(Order("o2", "TSLA", Side.SELL, 4, None, "sig1", "base"))

    with pytest.warns(UserWarning, match="parcial"):
        eventos = broker.on_bar(bar(34, o=405.0, h=406.0, l=404.0, c=405.0))

    (cierre,) = [e for e in eventos if type(e).__name__ == "PositionClosed"]
    assert cierre.shares == 10  # se cierra la posición entera, no solo las 4 pedidas
    assert broker.positions() == []


def test_close_all_liquida_cada_posicion_a_su_propia_vela():
    # CRÍTICA: antes close_all recibía UNA sola vela y la aplicaba a TODAS las
    # posiciones abiertas, aunque esa vela solo pudiera ser de un símbolo. Con TSLA y
    # NVDA abiertas a la vez, el cierre forzado de las 15:45 liquidaba una al precio
    # de cierre de la otra, falseando exit_price, pnl y r_multiple. Precios muy
    # separados a propósito: si el bug estuviera presente, una de las dos posiciones
    # se cerraría con el precio de la otra.
    broker = SimulatedBroker(starting_cash=200_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 10, 200.0, "sig_tsla", "base"))
    broker.submit(Order("o2", "NVDA", Side.BUY, 5, 700.0, "sig_nvda", "base"))

    broker.on_bar(bar_de("TSLA", 33, 400.0, 402.0, 399.0, 401.0))
    broker.on_bar(bar_de("NVDA", 33, 800.0, 802.0, 799.0, 801.0))

    velas_de_cierre = {
        "TSLA": bar_de("TSLA", 34, 450.0, 452.0, 448.0, 451.0),
        "NVDA": bar_de("NVDA", 34, 900.0, 902.0, 898.0, 901.0),
    }
    cierres = broker.close_all(velas_de_cierre, "force_close")

    cierre_tsla = next(c for c in cierres if c.symbol == "TSLA")
    cierre_nvda = next(c for c in cierres if c.symbol == "NVDA")

    assert cierre_tsla.exit_price == pytest.approx(451.0)
    assert cierre_nvda.exit_price == pytest.approx(901.0)
    assert cierre_tsla.pnl == pytest.approx((451.0 - 400.0) * 10)
    assert cierre_nvda.pnl == pytest.approx((901.0 - 800.0) * 5)
    assert broker.positions() == []


def test_close_all_deja_abierta_y_avisa_la_posicion_sin_vela():
    # Inventar un precio de salida es peor que no cerrar: si no llegó ninguna vela de
    # un símbolo en esta vuelta, esa posición se queda abierta y se avisa, en vez de
    # liquidarla con datos de otro símbolo.
    broker = SimulatedBroker(starting_cash=200_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 10, 200.0, "sig_tsla", "base"))
    broker.submit(Order("o2", "NVDA", Side.BUY, 5, 700.0, "sig_nvda", "base"))

    broker.on_bar(bar_de("TSLA", 33, 400.0, 402.0, 399.0, 401.0))
    broker.on_bar(bar_de("NVDA", 33, 800.0, 802.0, 799.0, 801.0))

    with pytest.warns(UserWarning, match="NVDA"):
        cierres = broker.close_all({"TSLA": bar_de("TSLA", 34, 450.0, 452.0, 448.0, 451.0)},
                                    "force_close")

    assert [c.symbol for c in cierres] == ["TSLA"]
    simbolos_abiertos = {p.symbol for p in broker.positions()}
    assert simbolos_abiertos == {"NVDA"}


def test_la_venta_por_submit_aplica_deslizamiento_y_comision_exactamente_una_vez():
    # Con costes en cero (como usan las otras cuatro pruebas de venta) un doble
    # descuento sería indetectable: aplicar dos veces un factor de 1±0 da lo mismo.
    # Aquí se usan costes reales para comprobar que el deslizamiento y la comisión
    # se aplican EXACTAMENTE UNA VEZ en la venta enviada por submit().
    modelo = ExecutionModel(slippage_pct=0.001, commission_per_share=1.0, min_commission=0.0)
    broker = SimulatedBroker(starting_cash=100_000.0, execution=modelo)
    broker.submit(compra(shares=10, stop=390.0))
    broker.on_bar(bar(33, o=400.0, h=402.0, l=399.0, c=401.0))

    broker.submit(Order("o2", "TSLA", Side.SELL, 10, None, "sig1", "base"))
    eventos = broker.on_bar(bar(34, o=410.0, h=411.0, l=409.0, c=410.0))

    (fill,) = [e for e in eventos if type(e).__name__ == "Fill"]
    (cierre,) = [e for e in eventos if type(e).__name__ == "PositionClosed"]

    # Compra: entrada 400 * 1.001 = 400.4; comisión 10 * 1.0 = 10.
    entrada_esperada = 400.0 * 1.001
    # Venta: salida 410 * (1 - 0.001) = 409.59; comisión 10 * 1.0 = 10.
    salida_esperada = 410.0 * 0.999
    # LAS DOS comisiones. Esta prueba restaba solo la de venta al P&L mientras restaba las
    # dos a la caja, que es justo la incoherencia que tenía el bróker: la comisión de entrada
    # salía del efectivo pero no del resultado informado, y cada operación se contaba mejor
    # de lo que fue.
    pnl_esperado = (salida_esperada - entrada_esperada) * 10 - 20.0

    assert fill.price == pytest.approx(salida_esperada)
    assert cierre.exit_price == pytest.approx(salida_esperada)
    assert cierre.pnl == pytest.approx(pnl_esperado)

    cash_esperado = (
        100_000.0
        - (10 * entrada_esperada + 10.0)   # compra
        + (10 * salida_esperada - 10.0)    # venta
    )
    assert broker.account().cash == pytest.approx(cash_esperado)


# --- Ejecución DENTRO de la vela que la disparó (Timing) ------------------------------
#
# Hasta el 2026-09-10 todo se ejecutaba en la apertura de la vela siguiente, y eso metía un
# minuto de retraso en cada entrada y en cada salida. Ninguna de las formas de aquí mira el
# futuro: `THIS_CLOSE` compra o vende al cierre de la vela que acaba de cerrar, y `THIS_STOP`
# es una orden stop ya puesta que se ejecuta cuando el precio la cruza.


def test_al_cierre_se_ejecuta_en_la_misma_vela_que_lo_pidio():
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    orden = Order("o1", "TSLA", Side.BUY, 10, 390.0, "sig1", "base",
                  timing=Timing.THIS_CLOSE)
    broker.submit(orden)

    eventos = broker.settle(bar(32, o=402.0, h=405.0, l=401.0, c=404.0))

    assert [e.price for e in eventos] == [pytest.approx(404.0)]
    assert broker.positions()[0].entry_price == pytest.approx(404.0)


def test_la_stop_de_ruptura_se_ejecuta_en_su_precio_no_en_el_cierre():
    """El precio pasó por ahí dentro de la vela y la orden estaba puesta desde antes."""
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 10, 390.0, "sig1", "base",
                        timing=Timing.THIS_STOP, trigger_price=403.0))

    eventos = broker.settle(bar(32, o=401.0, h=405.0, l=400.5, c=404.0))

    assert eventos[0].price == pytest.approx(403.0)


def test_un_hueco_por_encima_del_disparo_ejecuta_en_la_apertura():
    """Darse el precio del stop cuando la vela ya abrió por encima es regalarse el hueco."""
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 10, 390.0, "sig1", "base",
                        timing=Timing.THIS_STOP, trigger_price=403.0))

    eventos = broker.settle(bar(32, o=406.0, h=408.0, l=405.0, c=407.0))

    assert eventos[0].price == pytest.approx(406.0)


def test_una_entrada_intravela_puede_saltar_por_su_stop_en_esa_misma_vela():
    """La orden salta en mitad del minuto y el minuto puede seguir bajando.

    No se sabe en qué orden ocurrieron el máximo y el mínimo dentro de la vela, y ante esa
    duda se elige la lectura desfavorable: es la única que no puede inflar el resultado."""
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 10, 402.0, "sig1", "base",
                        timing=Timing.THIS_STOP, trigger_price=403.0))

    eventos = broker.settle(bar(32, o=401.5, h=405.0, l=400.0, c=401.0))

    cierres = [e for e in eventos if isinstance(e, PositionClosed)]
    assert len(cierres) == 1
    assert cierres[0].exit_price == pytest.approx(402.0)
    assert cierres[0].exit_reason == "stop"
    assert broker.positions() == []


def test_una_compra_al_cierre_no_mira_el_minimo_de_su_propia_vela():
    """Se compra AL CIERRE: el recorrido de esa vela ya había pasado cuando la posición
    existe, así que cerrarla por el mínimo de esa misma vela sería matarla por algo anterior
    a su nacimiento."""
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 10, 402.0, "sig1", "base",
                        timing=Timing.THIS_CLOSE))

    eventos = broker.settle(bar(32, o=401.5, h=405.0, l=400.0, c=404.0))

    assert not [e for e in eventos if isinstance(e, PositionClosed)]
    assert broker.positions()[0].entry_price == pytest.approx(404.0)


def test_la_salida_al_cierre_se_ejecuta_en_la_vela_que_la_marco():
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(compra())
    broker.on_bar(bar(32, o=400.0, h=401.0, l=399.0, c=400.5))

    broker.submit(Order("o2", "TSLA", Side.SELL, 10, None, "sig1", "base",
                        reason="moris:stochastic", timing=Timing.THIS_CLOSE))
    eventos = broker.settle(bar(33, o=402.0, h=406.0, l=401.0, c=405.0))

    cierres = [e for e in eventos if isinstance(e, PositionClosed)]
    assert cierres[0].exit_price == pytest.approx(405.0)
    assert cierres[0].exit_reason == "stochastic"


def test_lo_de_la_vela_siguiente_no_lo_toca_settle():
    """Una orden `NEXT_OPEN` sigue esperando a la apertura de la siguiente."""
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(compra())

    assert broker.settle(bar(32, o=400.0, h=401.0, l=399.0, c=400.5)) == []
    assert broker.positions() == []


# --- Posiciones por tramo: lo que hace posible la variante mixta ---------------------


#: Contador de minutos para que las velas del bloque avancen en el tiempo.
_MINUTO = [30]


def vela(o: float, h: float, l: float, c: float) -> Bar:
    """Vela de TSLA en el minuto siguiente. El instante avanza solo."""
    _MINUTO[0] += 1
    return bar(_MINUTO[0], o, h, l, c)


def test_dos_tramos_del_mismo_simbolo_coexisten():
    """El fallo que impedía la variante mixta: el índice por símbolo hacía que el segundo
    tramo pisara al primero en silencio — sin cierre, sin evento y sin stop."""
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 50, 405.0, "s1", "aggressive"))
    broker.submit(Order("o2", "TSLA", Side.BUY, 50, 409.0, "s1", "conservative"))

    broker.on_bar(vela(410.0, 411.0, 409.5, 410.5))

    tramos = broker.positions()
    assert len(tramos) == 2
    assert {p.tag for p in tramos} == {"aggressive", "conservative"}
    assert sum(p.shares for p in tramos) == 100


def test_el_stop_de_un_tramo_no_cierra_el_otro():
    """En la variante mixta el stop agresivo es amplio y el conservador ajustado, y esa
    diferencia es justo lo que la variante existe para medir. Un solo stop por símbolo
    cerraría los dos a la vez y la comparación no diría nada."""
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 50, 405.0, "s1", "aggressive"))
    broker.submit(Order("o2", "TSLA", Side.BUY, 50, 409.0, "s1", "conservative"))
    broker.on_bar(vela(410.0, 411.0, 409.5, 410.5))

    # Vela que perfora 409 pero no 405: solo debe saltar el stop ajustado.
    eventos = broker.on_bar(vela(410.0, 410.5, 408.0, 408.5))

    cierres = [e for e in eventos if isinstance(e, PositionClosed)]
    assert len(cierres) == 1
    assert cierres[0].tag == "conservative"
    assert {p.tag for p in broker.positions()} == {"aggressive"}


def test_una_venta_etiquetada_cierra_solo_su_tramo():
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 50, 405.0, "s1", "aggressive"))
    broker.submit(Order("o2", "TSLA", Side.BUY, 50, 405.0, "s1", "conservative"))
    broker.on_bar(vela(410.0, 411.0, 409.5, 410.5))

    broker.submit(Order("o3", "TSLA", Side.SELL, 50, None, "s1", "aggressive"))
    eventos = broker.on_bar(vela(412.0, 413.0, 411.0, 412.5))

    cierres = [e for e in eventos if isinstance(e, PositionClosed)]
    assert len(cierres) == 1
    assert cierres[0].tag == "aggressive"
    assert {p.tag for p in broker.positions()} == {"conservative"}


def test_close_all_cierra_todos_los_tramos():
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 50, 405.0, "s1", "aggressive"))
    broker.submit(Order("o2", "TSLA", Side.BUY, 50, 405.0, "s1", "conservative"))
    ultima = vela(410.0, 411.0, 409.5, 410.5)
    broker.on_bar(ultima)

    cierres = broker.close_all({"TSLA": ultima}, "force_close")

    assert len(cierres) == 2
    assert broker.positions() == []


def test_las_excursiones_se_siguen_por_tramo():
    """MAE y MFE de cada tramo se miden desde SU precio de entrada, que es distinto:
    compartirlos falsearía los del que entró más abajo."""
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 10, 400.0, "s1", "aggressive"))
    broker.on_bar(vela(410.0, 410.5, 409.5, 410.0))          # entra a 410
    broker.submit(Order("o2", "TSLA", Side.BUY, 10, 400.0, "s1", "conservative"))
    broker.on_bar(vela(415.0, 415.5, 414.5, 415.0))          # entra a 415
    ultima = vela(415.0, 420.0, 414.0, 419.0)
    broker.on_bar(ultima)

    cierres = {c.tag: c for c in broker.close_all({"TSLA": ultima}, "force_close")}

    assert cierres["aggressive"].entry_price == 410.0
    assert cierres["conservative"].entry_price == 415.0
    assert cierres["aggressive"].mfe > cierres["conservative"].mfe


def test_un_tramo_con_etiqueta_distinta_no_se_confunde_entre_simbolos():
    """La clave es (símbolo, etiqueta): dos símbolos con la misma etiqueta son dos tramos."""
    broker = SimulatedBroker(starting_cash=200_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 10, 400.0, "s1", "base"))
    broker.submit(Order("o2", "NVDA", Side.BUY, 10, 100.0, "s2", "base"))
    broker.on_bar(vela(410.0, 411.0, 409.0, 410.0))
    broker.on_bar(bar_de("NVDA", 59, 110.0, 111.0, 109.0, 110.0))

    assert {(p.symbol, p.tag) for p in broker.positions()} == {("TSLA", "base"), ("NVDA", "base")}


# --- §7.3: el trailing modifica una orden viva --------------------------------------


def test_modify_stop_sube_el_stop_y_lo_dice():
    from bot.core.events import StopModified

    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 50, 405.0, "s1", "aggressive"))
    broker.on_bar(vela(410.0, 411.0, 409.5, 410.5))

    eventos = broker.modify_stop("TSLA", "aggressive", 408.0)

    assert len(eventos) == 1
    assert isinstance(eventos[0], StopModified)
    assert eventos[0].old_stop == 405.0
    assert eventos[0].new_stop == 408.0
    assert eventos[0].reason == "trailing"
    assert broker.positions()[0].stop_price == 408.0


def test_modify_stop_rechaza_bajar_el_stop():
    """Doble barrera: el gestor no lo propone y el bróker no lo aceptaría igualmente. Es la
    clase de error que solo se descubre con dinero puesto."""
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 50, 405.0, "s1", "aggressive"))
    broker.on_bar(vela(410.0, 411.0, 409.5, 410.5))

    assert broker.modify_stop("TSLA", "aggressive", 400.0) == []
    assert broker.positions()[0].stop_price == 405.0


def test_modify_stop_de_un_tramo_que_no_existe_no_hace_nada():
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)

    assert broker.modify_stop("TSLA", "aggressive", 408.0) == []


def test_modify_stop_solo_toca_su_tramo():
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 25, 405.0, "s1", "aggressive"))
    broker.submit(Order("o2", "TSLA", Side.BUY, 25, 409.0, "s1", "conservative"))
    broker.on_bar(vela(410.0, 411.0, 409.5, 410.5))

    broker.modify_stop("TSLA", "aggressive", 408.0)

    stops = {p.tag: p.stop_price for p in broker.positions()}
    assert stops == {"aggressive": 408.0, "conservative": 409.0}


def test_el_cierre_por_stop_trailing_se_distingue_del_stop_original():
    """Sin esta distinción no se puede responder después "¿cuánto aportó el trailing?": un
    cierre por trailing y uno por el stop de la entrada serían indistinguibles."""
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 50, 405.0, "s1", "aggressive"))
    broker.on_bar(vela(410.0, 411.0, 409.5, 410.5))
    broker.modify_stop("TSLA", "aggressive", 409.0)

    eventos = broker.on_bar(vela(410.0, 410.5, 408.0, 408.5))

    cierres = [e for e in eventos if isinstance(e, PositionClosed)]
    assert cierres[0].exit_reason == "trailing_stop"


def test_el_cierre_por_el_stop_de_la_entrada_sigue_diciendo_stop():
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 50, 409.0, "s1", "aggressive"))
    broker.on_bar(vela(410.0, 411.0, 409.5, 410.5))

    eventos = broker.on_bar(vela(410.0, 410.5, 408.0, 408.5))

    cierres = [e for e in eventos if isinstance(e, PositionClosed)]
    assert cierres[0].exit_reason == "stop"


def test_una_venta_de_la_estrategia_lleva_su_motivo_al_cierre():
    """§7.3: sin esto, toda salida de la estrategia se registraría como un genérico
    "sell_order" y el informe no podría separar la salida por sobrecompra de las demás."""
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 50, 405.0, "s1", "aggressive"))
    broker.on_bar(vela(410.0, 411.0, 409.5, 410.5))

    broker.submit(
        Order("o2", "TSLA", Side.SELL, 50, None, "s1", "aggressive", "moris:stochastic")
    )
    eventos = broker.on_bar(vela(412.0, 413.0, 411.5, 412.5))

    cierres = [e for e in eventos if isinstance(e, PositionClosed)]
    assert cierres[0].exit_reason == "stochastic"


def test_una_venta_sin_motivo_se_registra_como_sell_order():
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 50, 405.0, "s1", "aggressive"))
    broker.on_bar(vela(410.0, 411.0, 409.5, 410.5))

    broker.submit(Order("o2", "TSLA", Side.SELL, 50, None, "s1", "aggressive"))
    eventos = broker.on_bar(vela(412.0, 413.0, 411.5, 412.5))

    cierres = [e for e in eventos if isinstance(e, PositionClosed)]
    assert cierres[0].exit_reason == "sell_order"


def test_R_se_mide_contra_el_stop_de_la_entrada_y_no_contra_el_trailado():
    """El riesgo de una operación es el que se asumió al ENTRAR.

    Medido en el backtest dorado antes de separar los dos stops: un tramo con R real de 1,09
    se registraba como R = 13,27, porque su stop trailado había quedado a diez céntimos de
    la entrada. Un múltiplo así no corresponde a ningún riesgo que nadie corriera, y con él
    la distribución de R —que es la cifra con la que se compara una variante con otra— deja
    de significar nada.
    """
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(Order("o1", "TSLA", Side.BUY, 10, 408.0, "s1", "base"))
    broker.on_bar(vela(410.0, 410.5, 409.5, 410.0))        # entra a 410, R = 2.00
    broker.modify_stop("TSLA", "base", 409.9)              # el trailing casi lo pega a la entrada

    ultima = vela(414.0, 414.5, 413.5, 414.0)
    cierre = broker.close_all({"TSLA": ultima}, "force_close")[0]

    # (414 - 410) / 2.00 = 2.00, no (414 - 410) / 0.10 = 40.
    assert cierre.r_multiple == pytest.approx(2.0)


# --- N1: bruto y costes, medidos y no reconstruidos -----------------------------------

CON_COSTES = ExecutionModel(slippage_pct=0.001, commission_per_share=0.01, min_commission=1.0)


def _cierra_una_operacion(broker):
    """Compra 10 a 400 y cierra por venta a 410, con la vela siguiente."""
    broker.submit(compra(shares=10, stop=None))
    broker.on_bar(bar(33, o=400.0, h=405.0, l=399.0, c=404.0))
    broker.submit(Order("o2", "TSLA", Side.SELL, 10, None, "sig1", "base"))
    eventos = broker.on_bar(bar(34, o=410.0, h=412.0, l=409.0, c=411.0))
    return next(e for e in eventos if isinstance(e, PositionClosed))


def test_el_pnl_resta_LAS_DOS_comisiones_y_no_solo_la_de_salida():
    """La de entrada salía de la caja pero no del P&L informado, así que cada operación se
    informaba mejor de lo que fue. Con 9.186 operaciones del barrido son ~9.186 de diferencia
    entre lo que dicen los registros y lo que hizo el dinero."""
    broker = SimulatedBroker(starting_cash=100_000.0, execution=CON_COSTES)
    caja_antes = broker.account().cash

    cierre = _cierra_una_operacion(broker)

    assert cierre.pnl == pytest.approx(broker.account().cash - caja_antes)


def test_el_cierre_informa_del_bruto_aparte_de_los_costes():
    """Sin esto, saber si la estrategia tiene ventaja exige reconstruir los costes desde la
    posición media, que es una estimación --y es el número que decide si vale la pena."""
    broker = SimulatedBroker(starting_cash=100_000.0, execution=CON_COSTES)

    cierre = _cierra_una_operacion(broker)

    # Bruto: (410 - 400) * 10, sin deslizamiento ni comisiones.
    assert cierre.gross_pnl == pytest.approx(100.0)
    # Comisiones: 10 x 0,01 = 0,10 por lado, pero el mínimo es 1,00. Dos lados.
    assert cierre.commission == pytest.approx(2.0)
    # Deslizamiento: 0,4 por acción al comprar y 0,41 al vender, por 10 acciones.
    assert cierre.slippage == pytest.approx(400.0 * 0.001 * 10 + 410.0 * 0.001 * 10)


def test_el_bruto_menos_los_costes_es_exactamente_el_neto():
    """Si no cuadran, una de las tres cifras miente y no se sabe cuál."""
    broker = SimulatedBroker(starting_cash=100_000.0, execution=CON_COSTES)

    c = _cierra_una_operacion(broker)

    assert c.pnl == pytest.approx(c.gross_pnl - c.commission - c.slippage)


def test_sin_costes_el_bruto_y_el_neto_coinciden():
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)

    c = _cierra_una_operacion(broker)

    assert c.gross_pnl == pytest.approx(c.pnl)
    assert (c.commission, c.slippage) == (0.0, 0.0)


# --- Deslizamiento por símbolo: 0,46 pb en AAPL y 6,44 en REGN no son el mismo coste ---


def test_sin_tabla_por_simbolo_todo_sigue_igual():
    """Lo de siempre: un solo número para todos. No se cambia nada por defecto."""
    modelo = ExecutionModel(slippage_pct=0.001)

    assert modelo.apply_slippage(400.0, Side.BUY, "REGN") == pytest.approx(400.4)


def test_un_simbolo_con_su_propio_deslizamiento_usa_el_suyo():
    """MEDIDO el 2026-09-01 sobre 235.851 cotizaciones: AAPL y NVDA tienen 0,46 pb por lado y
    REGN 6,44. Catorce veces. Cobrarles lo mismo abarata unas operaciones y encarece otras, y
    con una ventaja bruta de 3 pb eso decide cuáles salen rentables."""
    modelo = ExecutionModel(slippage_pct=0.0002,
                            slippage_pct_by_symbol={"REGN": 0.000644, "AAPL": 0.000046})

    assert modelo.apply_slippage(100.0, Side.BUY, "REGN") == pytest.approx(100.0644)
    assert modelo.apply_slippage(100.0, Side.BUY, "AAPL") == pytest.approx(100.0046)
    assert modelo.apply_slippage(100.0, Side.SELL, "REGN") == pytest.approx(99.9356)


def test_un_simbolo_que_no_esta_en_la_tabla_paga_el_general():
    """Una tabla incompleta no puede significar "este no paga deslizamiento"."""
    modelo = ExecutionModel(slippage_pct=0.001, slippage_pct_by_symbol={"AAPL": 0.0})

    assert modelo.apply_slippage(400.0, Side.BUY, "MU") == pytest.approx(400.4)


def test_el_broker_cobra_a_cada_simbolo_el_suyo():
    """De nada sirve el modelo si el bróker no le dice de qué símbolo habla."""
    modelo = ExecutionModel(slippage_pct=0.0, commission_per_share=0.0, min_commission=0.0,
                            slippage_pct_by_symbol={"TSLA": 0.001})
    broker = SimulatedBroker(starting_cash=100_000.0, execution=modelo)

    cierre = _cierra_una_operacion(broker)

    assert cierre.slippage == pytest.approx(400.0 * 0.001 * 10 + 410.0 * 0.001 * 10)


# --- Entrada con orden LIMITADA -------------------------------------------------------

def _limitada(shares=10, stop=390.0, limite=400.0):
    return Order("o1", "TSLA", Side.BUY, shares, stop, "sig1", "base", limit_price=limite)


def test_una_limitada_no_ejecuta_si_el_precio_no_baja_hasta_ella():
    """Es la diferencia entera con la orden a mercado: no se persigue al precio. MEDIDO el
    2026-09-02 sobre 9.186 operaciones: de los 6,43 de coste por operación, 3,97 son
    deslizamiento --el 62 %-- y vienen de comprar en la apertura de la vela siguiente, sea
    cual sea. La estrategia entra en una zona de soporte, que es un nivel conocido de
    antemano: es justo donde una limitada tiene sentido."""
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(_limitada(limite=400.0))

    eventos = broker.on_bar(bar(33, o=405.0, h=407.0, l=402.0, c=406.0))

    assert broker.positions() == []
    assert not [e for e in eventos if type(e).__name__ == "Fill"]


def test_una_limitada_ejecuta_a_su_precio_cuando_el_recorrido_la_toca():
    """Y nunca peor: si la vela abrió por debajo del límite, se ejecuta en la apertura, que
    es mejor. Comprar más caro que el límite sería no tener límite."""
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(_limitada(limite=400.0))

    (ejecucion,) = broker.on_bar(bar(33, o=402.0, h=403.0, l=399.0, c=401.0))

    assert ejecucion.price == pytest.approx(400.0)


def test_una_limitada_con_hueco_a_la_baja_ejecuta_en_la_apertura():
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(_limitada(limite=400.0))

    (ejecucion,) = broker.on_bar(bar(33, o=396.0, h=399.0, l=395.0, c=398.0))

    assert ejecucion.price == pytest.approx(396.0)


def test_una_limitada_espera_varias_velas_antes_de_rendirse():
    """Un solo intento la convertiría casi en una orden a mercado con mala suerte. Esperar
    para siempre es peor: la señal caduca y el nivel deja de significar lo que significaba."""
    modelo = ExecutionModel(slippage_pct=0.0, commission_per_share=0.0, min_commission=0.0,
                            limit_ttl_bars=3)
    broker = SimulatedBroker(starting_cash=100_000.0, execution=modelo)
    broker.submit(_limitada(limite=400.0))

    for minuto in (33, 34):
        assert not broker.on_bar(bar(minuto, o=405.0, h=406.0, l=402.0, c=405.0))

    (ejecucion,) = broker.on_bar(bar(35, o=402.0, h=403.0, l=399.0, c=401.0))

    assert ejecucion.price == pytest.approx(400.0)


def test_una_limitada_que_caduca_libera_su_clave():
    """Sin un final de ciclo, la clave anti-duplicado se queda cogida y el símbolo no puede
    volver a entrar en toda la sesión aunque la señal se repita."""
    modelo = ExecutionModel(slippage_pct=0.0, commission_per_share=0.0, min_commission=0.0,
                            limit_ttl_bars=2)
    broker = SimulatedBroker(starting_cash=100_000.0, execution=modelo)
    broker.submit(_limitada(limite=400.0))

    broker.on_bar(bar(33, o=405.0, h=406.0, l=402.0, c=405.0))
    eventos = broker.on_bar(bar(34, o=405.0, h=406.0, l=402.0, c=405.0))

    caducadas = [e for e in eventos if type(e).__name__ == "OrderCancelled"]
    assert caducadas and caducadas[0].order_id == "o1"
    assert "limit" in caducadas[0].reason.lower() or "limitada" in caducadas[0].reason.lower()


def test_sin_limite_todo_sigue_ejecutando_a_mercado():
    """El comportamiento de siempre: quien no pida limitada no se entera de que existe."""
    broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
    broker.submit(compra())

    (ejecucion,) = broker.on_bar(bar(33, o=405.0, h=407.0, l=404.0, c=406.0))

    assert ejecucion.price == pytest.approx(405.0)


def test_una_limitada_no_paga_deslizamiento():
    """Es su punto ENTERO: una orden limitada en reposo no cruza el diferencial. El
    deslizamiento modela el coste de cruzarlo con una orden a mercado, y cobrarselo a una
    limitada es cobrarle justo lo que evita --MEDIDO el 2026-09-04: con el deslizamiento
    aplicado igual, el ahorro medido fue de 3,98 a 2,92 cuando deberia haber sido la mitad
    entera del coste--."""
    modelo = ExecutionModel(slippage_pct=0.01, commission_per_share=0.0, min_commission=0.0)
    broker = SimulatedBroker(starting_cash=100_000.0, execution=modelo)
    broker.submit(_limitada(limite=400.0))

    (ejecucion,) = broker.on_bar(bar(33, o=402.0, h=403.0, l=399.0, c=401.0))

    assert ejecucion.price == pytest.approx(400.0), "pago mas que su limite"


def test_el_bruto_de_una_limitada_cuenta_contra_lo_que_de_verdad_se_pago():
    """Si el bruto se mide contra la apertura de la vela y la compra fue al limite, el
    desglose no cuadra: el bruto sale peor de lo que fue y el deslizamiento absorbe la
    diferencia. Los tres numeros tienen que seguir sumando."""
    modelo = ExecutionModel(slippage_pct=0.001, commission_per_share=0.0, min_commission=0.0)
    broker = SimulatedBroker(starting_cash=100_000.0, execution=modelo)
    broker.submit(_limitada(shares=10, stop=None, limite=400.0))
    broker.on_bar(bar(33, o=402.0, h=403.0, l=399.0, c=401.0))
    broker.submit(Order("o2", "TSLA", Side.SELL, 10, None, "sig1", "base"))

    cierre = next(e for e in broker.on_bar(bar(34, o=410.0, h=412.0, l=409.0, c=411.0))
                  if isinstance(e, PositionClosed))

    # Compra a 400 (su limite, sin deslizamiento), venta a 410 con deslizamiento de venta.
    assert cierre.gross_pnl == pytest.approx((410.0 - 400.0) * 10)
    # Solo la pata de VENTA desliza.
    assert cierre.slippage == pytest.approx(410.0 * 0.001 * 10)
    assert cierre.pnl == pytest.approx(cierre.gross_pnl - cierre.commission - cierre.slippage)


# NOTA DE LA VERSION PUBLICA
# Dos pruebas de este fichero comprueban que el PERFIL (YAML) sabe configurar el
# deslizamiento por simbolo y la entrada con orden limitada. Viven en la version
# completa porque atraviesan `bot.main`, el cableado que monta el bot entero desde un
# perfil, y ese cableado no forma parte de esta version publica.
