"""Una misma batería contra TODAS las implementaciones de `Broker`.

§15.3, capa 2: *"es lo que garantiza que el bróker simulado se comporta como el real, de lo
que depende que los resultados del backtest signifiquen algo"*.

Sin esto, el bróker simulado y el de IBKR pueden divergir sin que nada lo diga, y entonces
las cifras del backtest dejan de servir para decidir nada — que es el propósito entero de
tener un backtest.

**Lo que la batería fija y lo que no.** Fija el CONTRATO: qué hechos produce cada operación,
en qué estado queda el bróker después y qué garantías se mantienen. No fija los precios ni
los tiempos: el simulado ejecuta en la apertura de la vela siguiente con deslizamiento, y
IBKR ejecuta cuando el mercado cruza. Esa diferencia es real y deliberada, y meterla en la
batería obligaría a falsearla en uno de los dos lados.

`Mundo` es la costura: sabe hacer avanzar cada implementación hasta que sus hechos ocurren.
En el simulado eso es entregar una vela; en el de IBKR, inyectar la ejecución que IBKR
entregaría por su devolución de llamada.

**Nota de la version publica.** La bateria esta escrita para correr contra VARIAS
implementaciones de `Broker` a la vez (un "mundo" por cada una). Aqui solo viaja el
broker simulado, porque el adaptador de IBKR no forma parte de esta version; la
estructura parametrizada se conserva intacta, que es justo lo que permite anadir otro
broker sin tocar ni una prueba.
"""

from __future__ import annotations

import datetime as dt

import pytest

from bot.adapters.brokers.simulated import ExecutionModel, SimulatedBroker
from bot.core.clock import SimulatedClock
from bot.core.events import Fill, OrderRejected, PositionClosed, StopModified
from bot.core.types import Bar, Order, Side

TZ = dt.timezone.utc
T0 = dt.datetime(2026, 8, 26, 14, 0, tzinfo=TZ)
SIN_COSTES = ExecutionModel(slippage_pct=0.0, commission_per_share=0.0, min_commission=0.0)


def _orden(side=Side.BUY, shares=10, stop=405.0, tag="base", oid="o1", signal="s1"):
    return Order(oid, "TSLA", side, shares, stop, signal, tag)


# --- Los mundos -------------------------------------------------------------------


class _MundoSimulado:
    """Hace avanzar el bróker simulado entregándole velas."""

    nombre = "simulado"

    def __init__(self):
        self.broker = SimulatedBroker(starting_cash=100_000.0, execution=SIN_COSTES)
        self._minuto = 0

    def vela(self, precio=410.0, bajo=None, alto=None):
        self._minuto += 1
        ts = T0 + dt.timedelta(minutes=self._minuto)
        return Bar("TSLA", "1m", ts, precio, alto or precio + 1, bajo or precio - 1, precio, 1000)

    def avanzar(self, orden=None, precio=410.0, bajo=None):
        """Deja que ocurra lo que tenga que ocurrir tras enviar una orden.

        orden  : Order | None — la orden enviada, si la hubo. El simulado no la necesita.
        precio : float — precio al que el mercado ejecuta.
        bajo   : float | None — mínimo de la vela, para hacer saltar un stop.
        return : list[Event] — hechos producidos.
        """
        return list(self.broker.on_bar(self.vela(precio, bajo=bajo)))

    def cerrar_todo(self, motivo="force_close"):
        return list(self.broker.close_all({"TSLA": self.vela(410.0)}, motivo))


@pytest.fixture(params=[_MundoSimulado], ids=["simulado"])
def mundo(request):
    return request.param()


def _de_tipo(eventos, tipo):
    return [e for e in eventos if isinstance(e, tipo)]


# --- El contrato ----------------------------------------------------------------------


def test_una_compra_acaba_produciendo_un_fill_con_las_acciones_pedidas(mundo):
    mundo.broker.submit(_orden(shares=10))

    fills = _de_tipo(mundo.avanzar(precio=410.0), Fill)

    assert len(fills) == 1
    assert fills[0].shares == 10
    assert fills[0].side is Side.BUY


def test_todo_fill_lleva_el_identificador_de_la_orden(mundo):
    """Es lo que libera la clave anti-duplicado. Un `Fill` sin `order_id` dejaría esa clave
    viva para siempre, y con una venta eso es una posición que ya no se puede cerrar."""
    mundo.broker.submit(_orden(oid="o-abc"))

    fills = _de_tipo(mundo.avanzar(), Fill)

    assert fills[0].order_id == "o-abc"


def test_todo_fill_conserva_la_senal_y_la_etiqueta(mundo):
    mundo.broker.submit(_orden(tag="aggressive", signal="sig-42"))

    fill = _de_tipo(mundo.avanzar(), Fill)[0]

    assert fill.signal_id == "sig-42"
    assert fill.tag == "aggressive"


def test_una_compra_deja_una_posicion_recuperable_con_su_etiqueta(mundo):
    mundo.broker.submit(_orden(shares=10, tag="aggressive"))
    mundo.avanzar(precio=410.0)

    posiciones = mundo.broker.positions()

    assert len(posiciones) == 1
    assert posiciones[0].symbol == "TSLA"
    assert posiciones[0].shares == 10
    assert posiciones[0].tag == "aggressive"


def test_dos_tramos_del_mismo_simbolo_coexisten(mundo):
    """Es lo que hace posible la variante mixta de Moris: dos entradas del mismo símbolo con
    stops distintos, una por cada disparador."""
    mundo.broker.submit(_orden(oid="o1", tag="aggressive", shares=5, stop=405.0))
    mundo.broker.submit(_orden(oid="o2", tag="conservative", shares=5, stop=409.0))
    mundo.avanzar(precio=410.0)

    tramos = {p.tag for p in mundo.broker.positions()}

    assert tramos == {"aggressive", "conservative"}


def test_cada_tramo_conserva_su_propio_stop(mundo):
    """El agresivo es amplio y el conservador ajustado, y esa diferencia es todo el sentido de
    la variante mixta."""
    mundo.broker.submit(_orden(oid="o1", tag="aggressive", shares=5, stop=405.0))
    mundo.broker.submit(_orden(oid="o2", tag="conservative", shares=5, stop=409.0))
    mundo.avanzar(precio=410.0)

    stops = {p.tag: p.stop_price for p in mundo.broker.positions()}

    assert stops == {"aggressive": 405.0, "conservative": 409.0}


def test_una_venta_etiquetada_cierra_solo_su_tramo(mundo):
    mundo.broker.submit(_orden(oid="o1", tag="aggressive", shares=5))
    mundo.broker.submit(_orden(oid="o2", tag="conservative", shares=5))
    mundo.avanzar(precio=410.0)

    mundo.broker.submit(_orden(oid="o3", side=Side.SELL, shares=5, stop=None, tag="aggressive"))
    mundo.avanzar(precio=412.0)

    assert {p.tag for p in mundo.broker.positions()} == {"conservative"}


def test_una_venta_sin_posicion_no_crea_un_corto(mundo):
    """Ni el simulado ni el de IBKR deben acabar con acciones negativas: el sistema no modela
    posiciones cortas en ninguna parte."""
    mundo.broker.submit(_orden(side=Side.SELL, shares=10, stop=None))
    mundo.avanzar(precio=410.0)

    assert all(p.shares > 0 for p in mundo.broker.positions())


def test_modify_stop_sube_el_stop_y_lo_dice(mundo):
    mundo.broker.submit(_orden(shares=10, stop=405.0))
    mundo.avanzar(precio=410.0)

    eventos = mundo.broker.modify_stop("TSLA", "base", 408.0)

    assert len(_de_tipo(eventos, StopModified)) == 1
    assert mundo.broker.positions()[0].stop_price == 408.0


def test_modify_stop_nunca_baja_el_stop(mundo):
    """Un trailing que retrocede amplía el riesgo de una posición que ya va a favor. La regla
    se comprueba en el gestor, en el simulado y aquí: es la clase de error que solo se
    descubre con dinero puesto."""
    mundo.broker.submit(_orden(shares=10, stop=405.0))
    mundo.avanzar(precio=410.0)

    assert mundo.broker.modify_stop("TSLA", "base", 400.0) == []
    assert mundo.broker.positions()[0].stop_price == 405.0


def test_modify_stop_de_un_tramo_inexistente_no_revienta(mundo):
    assert mundo.broker.modify_stop("TSLA", "fantasma", 408.0) == []


def test_close_all_deja_el_broker_sin_posiciones(mundo):
    """Es la garantía del cierre forzado de las 15:45. Si falla, quedan posiciones abiertas de
    un día para otro."""
    mundo.broker.submit(_orden(oid="o1", tag="aggressive", shares=5))
    mundo.broker.submit(_orden(oid="o2", tag="conservative", shares=5))
    mundo.avanzar(precio=410.0)

    mundo.cerrar_todo("force_close")

    assert mundo.broker.positions() == []


def test_cancelar_una_orden_desconocida_no_revienta(mundo):
    """Cancelar algo que ya se ejecutó o que nunca existió es un caso normal, no un error."""
    assert mundo.broker.cancel("no-existe") == []


def test_la_cuenta_devuelve_cifras_no_negativas(mundo):
    cuenta = mundo.broker.account()

    assert cuenta.cash >= 0
    assert cuenta.equity >= 0
    assert cuenta.buying_power >= 0


def test_los_hechos_se_entregan_una_sola_vez(mundo):
    """Entregar dos veces la misma ejecución duplicaría la posición en el estado de riesgo."""
    mundo.broker.submit(_orden())
    primera = mundo.avanzar(precio=410.0)
    segunda = mundo.avanzar(precio=411.0)

    assert _de_tipo(primera, Fill)
    assert not _de_tipo(segunda, Fill)


def test_un_stop_perforado_cierra_la_posicion(mundo):
    """El stop vive en el bróker en las dos implementaciones. Que salte tiene que dejar el
    símbolo sin posición, o los filtros de riesgo creerían que sigue abierta."""
    mundo.broker.submit(_orden(shares=10, stop=405.0))
    mundo.avanzar(precio=410.0)

    mundo.avanzar(precio=406.0, bajo=404.0)

    assert mundo.broker.positions() == []


def test_todo_evento_de_resolucion_lleva_order_id(mundo):
    """Fill, rechazo y cancelación son las tres vías que liberan la clave anti-duplicado, y
    las tres la identifican por `order_id`."""
    mundo.broker.submit(_orden(oid="o-xyz"))
    eventos = mundo.avanzar()

    resolutorios = _de_tipo(eventos, (Fill, OrderRejected))
    assert resolutorios
    for evento in resolutorios:
        assert evento.order_id, f"{type(evento).__name__} sin order_id"


def test_un_cierre_no_deja_la_posicion_a_medias(mundo):
    """Tras cerrar, o la posición existe entera o no existe: nunca con cero acciones, que
    haría que `OnePositionPerSymbolFilter` bloqueara un símbolo sin nada abierto."""
    mundo.broker.submit(_orden(shares=10))
    mundo.avanzar(precio=410.0)
    mundo.cerrar_todo()

    assert all(p.shares > 0 for p in mundo.broker.positions())


def test_cerrar_una_posicion_deja_la_operacion_registrada(mundo):
    """Ni el simulado ni el de IBKR pueden cerrar una posicion en silencio. El de IBKR lo
    hacia hasta el 2026-09-02 --borraba el tramo al llegar a cero-- y por eso una sesion en
    vivo informaba SIEMPRE de 0 operaciones y 0 P&L: el informe y SQLite no tenian de donde
    salir. Esta prueba esta aqui, en la bateria, porque una diferencia entre los dos brokers
    en esto invalida la comparacion entre backtest y vivo."""
    mundo.broker.submit(_orden(oid="o1", shares=10, stop=390.0))
    mundo.avanzar(precio=400.0)

    mundo.broker.submit(_orden(oid="o2", side=Side.SELL, shares=10, stop=None))
    mundo.avanzar(precio=410.0)

    cierres = list(mundo.broker.closed_trades)
    assert len(cierres) == 1
    c = cierres[0]
    assert (c.symbol, c.shares, c.tag) == ("TSLA", 10, "base")
    assert c.entry_price > 0 and c.exit_price > 0


def test_el_cierre_registrado_cuadra_bruto_menos_costes(mundo):
    """El neto tiene que ser el bruto menos lo que costo, en los dos brokers. Si no cuadra,
    una de las tres cifras miente y no se sabe cual --que es exactamente lo que pasaba con la
    comision de entrada, que salia de la caja pero no del P&L informado--."""
    mundo.broker.submit(_orden(oid="o1", shares=10, stop=390.0))
    mundo.avanzar(precio=400.0)
    mundo.broker.submit(_orden(oid="o2", side=Side.SELL, shares=10, stop=None))
    mundo.avanzar(precio=410.0)

    c = mundo.broker.closed_trades[0]

    assert c.pnl == pytest.approx(c.gross_pnl - c.commission - c.slippage)
