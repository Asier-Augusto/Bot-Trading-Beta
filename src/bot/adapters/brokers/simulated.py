"""Bróker simulado para backtest, con modelo de ejecución deliberadamente pesimista.

Tres decisiones que hacen que las cifras no mientan:

1. Las órdenes a mercado entran en la APERTURA DE LA VELA SIGUIENTE, nunca al precio
   que disparó la señal. En vivo no existe forma de comprar al precio que acabas de ver.
2. Los stops se evalúan por el RECORRIDO COMPLETO de la vela, no por su cierre.
3. Si la vela abre por debajo del stop, se ejecuta en la apertura: un hueco no respeta
   el precio prometido.

Preferible que el backtest dé cifras peores que la realidad y no al revés.
"""

from __future__ import annotations

import datetime as dt
import warnings
from dataclasses import dataclass

from bot.contracts.broker import BarDrivenBroker
from bot.core.context import AccountSnapshot, Position
from bot.core.events import (
    Fill,
    OrderCancelled,
    OrderRejected,
    PositionClosed,
    StopModified,
)
from bot.core.registry import register_broker
from bot.core.types import Bar, Order, Side, Timing


@dataclass(frozen=True, slots=True)
class ExecutionModel:
    """Costes de ejecución del backtest.

    slippage_pct         : float — deslizamiento por lado, en tanto por uno. Es el que se
                           aplica a los símbolos que no tengan el suyo.
    slippage_pct_by_symbol : dict[str, float] | None — deslizamiento propio de cada símbolo.
                           MEDIDO el 2026-09-01 sobre 235.851 cotizaciones de los 33 valores
                           del §11.1: AAPL y NVDA tienen 0,46 puntos básicos por lado y REGN
                           6,44. **Catorce veces.** Cobrarles a todos el mismo 2,00 abarata
                           unas operaciones y encarece otras, y con una ventaja bruta de
                           unos 3 puntos básicos eso decide cuáles salen rentables.

                           Un símbolo que no esté en la tabla paga el general: una tabla
                           incompleta no puede significar "este no paga deslizamiento".
    commission_per_share : float — comisión por acción.
    min_commission       : float — comisión mínima por operación.
    limit_ttl_bars       : int — velas que una orden limitada espera antes de rendirse. Un
                           solo intento la convertiría casi en una orden a mercado con mala
                           suerte; esperar para siempre es peor, porque la señal caduca y el
                           nivel deja de significar lo que significaba cuando se vio.
    """

    slippage_pct: float = 0.0002
    commission_per_share: float = 0.005
    min_commission: float = 1.0
    slippage_pct_by_symbol: dict[str, float] | None = None
    limit_ttl_bars: int = 5

    def slippage_for(self, symbol: str | None) -> float:
        """Deslizamiento por lado que le toca a un símbolo.

        symbol : str | None — ticker; None usa el general.
        return : float — en tanto por uno.
        """
        if symbol is None or not self.slippage_pct_by_symbol:
            return self.slippage_pct
        return self.slippage_pct_by_symbol.get(symbol, self.slippage_pct)

    def apply_slippage(self, price: float, side: Side, symbol: str | None = None) -> float:
        """Empeora el precio en el sentido que perjudica.

        price  : float — precio teórico.
        side   : Side  — sentido de la operación.
        symbol : str | None — ticker, para cobrarle el suyo si lo tiene.
        return : float — precio efectivo, ya con el deslizamiento aplicado.
        """
        pct = self.slippage_for(symbol)
        return price * (1 + pct if side is Side.BUY else 1 - pct)

    def commission(self, shares: int) -> float:
        """Comisión de una ejecución.

        shares : int   — acciones negociadas.
        return : float — comisión en divisa, respetando el mínimo por operación.
        """
        return max(shares * self.commission_per_share, self.min_commission)


@dataclass
class _OpenPosition:
    """Posición viva con el seguimiento de sus excursiones.

    symbol      : str   — ticker.
    shares      : int   — acciones, positivo en largo.
    entry_price : float — precio medio de entrada, ya con deslizamiento.
    stop_price  : float — precio del stop protector VIGENTE. El trailing lo mueve.
    initial_stop: float — precio del stop con el que se abrió la posición. No se toca nunca.
                  Es el que mide R: el riesgo de una operación es el que se asumió al
                  entrar, y calcular el múltiplo contra un stop ya subido por el trailing
                  infla la cifra hasta hacerla no significar nada. Medido en el backtest
                  dorado antes de separarlos: un tramo con R real de 1,09 se registraba
                  como R = 13,27, porque su stop trailado había quedado a diez céntimos de
                  la entrada.
    signal_id   : str   — identificador de correlación de la señal que la abrió.
    tag         : str   — etiqueta del tramo.
    high_water  : float — máximo alcanzado por el precio desde la entrada.
    low_water   : float — mínimo alcanzado por el precio desde la entrada.
    trailing    : bool  — si el stop actual lo puso el trailing y no la entrada. Distinguir
                  los dos cierres es lo que permite responder después "¿cuánto aportó el
                  trailing?": sin esta marca, un cierre por trailing y uno por el stop
                  original serían indistinguibles en los registros.
    """

    symbol: str
    shares: int
    entry_price: float
    stop_price: float
    initial_stop: float
    signal_id: str
    tag: str
    high_water: float
    low_water: float
    trailing: bool = False
    #: Precio de entrada ANTES del deslizamiento, y lo que costaron entrada y comisión. Se
    #: guardan para poder informar del bruto por separado al cerrar: reconstruirlo después
    #: desde el precio con deslizamiento es una estimación, y es el número que decide si la
    #: estrategia tiene ventaja propia.
    raw_entry_price: float = 0.0
    entry_commission: float = 0.0

    def track(self, bar: Bar) -> None:
        """Actualiza las excursiones máximas con una vela nueva.

        bar    : Bar — vela recién cerrada.
        return : None
        """
        self.high_water = max(self.high_water, bar.high)
        self.low_water = min(self.low_water, bar.low)


#: Prefijo con el que las estrategias marcan sus motivos. Se recorta para que
#: `exit_reason` lleve el motivo pelado ("stochastic") y no el nombre de quien lo produjo.
_STRATEGY_PREFIX = "moris:"


def _exit_reason(order_reason: str) -> str:
    """Traduce el motivo de una orden de venta al motivo de cierre de la posición.

    order_reason : str — `Order.reason`, heredado de la intención.
    return       : str — el motivo pelado si la orden traía uno; "sell_order" si no, que es
                   lo que significa "una venta de la estrategia sin más detalle".
    """
    if not order_reason:
        return "sell_order"
    if order_reason.startswith(_STRATEGY_PREFIX):
        return order_reason[len(_STRATEGY_PREFIX):]
    return order_reason


@register_broker("simulated")
class SimulatedBroker(BarDrivenBroker):
    """Bróker de mentira con contabilidad de verdad."""

    def __init__(
        self,
        starting_cash: float = 100_000.0,
        execution: ExecutionModel | None = None,
    ) -> None:
        """Crea el bróker.

        starting_cash : float               — efectivo inicial.
        execution     : ExecutionModel|None — costes de ejecución; por defecto los del diseño.
        return        : None
        """
        self.execution = execution or ExecutionModel()
        self._cash = starting_cash
        self._pending: list[Order] = []
        #: Posiciones vivas indexadas por (SÍMBOLO, ETIQUETA), no solo por símbolo.
        #: La variante mixta de Moris abre DOS posiciones en el mismo símbolo —una por el
        #: disparador agresivo y otra por el conservador, cada una con su stop— y con el
        #: índice por símbolo la segunda pisaba a la primera en silencio: el primer tramo
        #: dejaba de existir sin haberse cerrado, sin evento, sin contabilidad y sin stop.
        self._positions: dict[tuple[str, str], _OpenPosition] = {}
        self.closed_trades: list[PositionClosed] = []
        #: order_id -> velas que lleva esperando una orden limitada.
        self._limit_age: dict[str, int] = {}
        #: Instante de la última vela procesada. NO sirve para fechar hechos que no
        #: nacen de una vela: arranca en la época Unix, así que una cancelación anterior
        #: a la primera vela llevaría un `ts` de 1970 y su registro acabaría en
        #: `logs/1970-01-01/`. El bróker no tiene reloj y no debe fingir que lo tiene: es
        #: el motor quien sella esos eventos con el suyo (ver `Engine.cancel_order`).
        self._last_ts: dt.datetime = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)

    def submit(self, order: Order) -> str:
        """Encola una orden para la apertura de la vela siguiente.

        order  : Order — orden aprobada por el riesgo.
        return : str   — identificador de la orden.
        """
        self._pending.append(order)
        return order.order_id

    def cancel(self, order_id: str) -> list[OrderCancelled]:
        """Retira una orden que aún no se ha ejecutado.

        Devuelve el evento en vez de publicarlo porque el bróker no conoce el bus: es
        el mismo patrón que `on_bar`, y es lo que permite al motor enterarse de que el
        ciclo de vida de esta orden ha terminado y liberar su clave anti-duplicado. Un
        `cancel` que no devolviera nada dejaría esa clave viva para siempre.

        order_id : str — identificador devuelto por `submit`.
        return   : list[OrderCancelled] — un evento si la orden estaba viva; vacía si
                                          el bróker no la tenía pendiente.
        """
        cancelled = [o for o in self._pending if o.order_id == order_id]
        self._pending = [o for o in self._pending if o.order_id != order_id]
        return [
            OrderCancelled(
                ts=self._last_ts,
                signal_id=order.signal_id,
                order_id=order.order_id,
                symbol=order.symbol,
                reason="orden retirada antes de ejecutarse",
            )
            for order in cancelled
        ]

    def positions(self) -> list[Position]:
        """Posiciones abiertas.

        return : list[Position] — una entrada por TRAMO vivo. Con la variante mixta puede
                 haber dos del mismo símbolo, distinguidas por su etiqueta.
        """
        return [
            Position(p.symbol, p.shares, p.entry_price, p.stop_price, p.tag, p.signal_id)
            for p in self._positions.values()
        ]

    def account(self) -> AccountSnapshot:
        """Foto de la cuenta.

        En el simulado el patrimonio se valora al precio de entrada, no al de mercado:
        el bróker no ve el precio actual entre velas.

        return : AccountSnapshot — efectivo, patrimonio y poder de compra.
        """
        invested = sum(p.shares * p.entry_price for p in self._positions.values())
        equity = self._cash + invested
        return AccountSnapshot(cash=self._cash, equity=equity, buying_power=self._cash)

    def on_bar(self, bar: Bar) -> list[Fill | PositionClosed | OrderRejected]:
        """Avanza una vela: ejecuta pendientes y comprueba stops.

        El orden importa: primero se comprueban los stops de lo que ya estaba abierto,
        y solo después se ejecutan las órdenes nuevas en la apertura. Al revés, una
        posición abierta en esta misma vela podría saltar por su stop en la misma vela
        en que entró, que en vivo no es lo que pasa.

        bar    : Bar  — vela recién cerrada.
        return : list — ejecuciones (Fill), cierres (PositionClosed) y rechazos
                        (OrderRejected) producidos.
        """
        self._last_ts = bar.ts
        events: list[Fill | PositionClosed | OrderRejected] = []
        events.extend(self._check_stops(bar))
        events.extend(self._execute_pending(bar))
        # Solo se siguen los tramos del símbolo de esta vela: una vela de TSLA no puede
        # pisar el high_water/low_water de una posición abierta en NVDA. Y cada tramo lleva
        # los suyos, porque entró a un precio distinto: compartirlos falsearía el MAE y el
        # MFE del que entró más abajo.
        for position in self._tranches_of(bar.symbol):
            position.track(bar)
        return events

    def _tranches_of(self, symbol: str) -> list[_OpenPosition]:
        """Tramos vivos de un símbolo.

        symbol : str — ticker.
        return : list[_OpenPosition] — vacía si no hay ninguno.
        """
        return [p for (s, _tag), p in self._positions.items() if s == symbol]

    def settle(self, bar: Bar) -> list[Fill | PositionClosed | OrderRejected]:
        """Ejecuta las órdenes que se resuelven DENTRO de esta vela.

        La llama el motor al final de la vela, después de que la estrategia haya decidido.
        Hasta el 2026-09-10 no existía: todo se ejecutaba en la apertura de la vela
        siguiente, y eso metía un minuto de retraso en cada entrada y en cada salida de una
        estrategia cuyo movimiento cabe en dos o tres minutos. Ver `Timing`.

        Ninguna de las dos formas mira el futuro. `THIS_CLOSE` compra o vende al cierre de
        la vela que acaba de cerrar, que es lo que hace en vivo un bot cuyos indicadores
        incluyen la vela en curso. `THIS_STOP` es una orden stop **ya puesta** en el
        mercado: se ejecuta en su precio porque el precio pasó por ahí dentro de esta vela,
        y si la vela abrió por encima se ejecuta en la apertura, que es peor y es lo que
        habría pasado de verdad.

        bar    : Bar  — vela recién cerrada.
        return : list — ejecuciones, cierres y rechazos producidos.
        """
        events: list[Fill | PositionClosed | OrderRejected] = []
        pendientes, self._pending = self._pending, []
        for order in pendientes:
            if order.symbol != bar.symbol or order.timing is Timing.NEXT_OPEN:
                self._pending.append(order)
                continue
            if order.timing is Timing.THIS_STOP:
                # Con hueco de apertura por encima del disparo, la stop se convierte en una
                # compra a mercado en la apertura: no se puede ejecutar por debajo de lo que
                # ya cotiza. Darse el precio del disparo aquí sería regalarse el hueco.
                referencia = max(order.trigger_price or bar.open, bar.open)
            else:
                referencia = bar.close
            hechos = self._apply(order, bar, referencia)
            events.extend(hechos)
            if order.timing is Timing.THIS_STOP:
                events.extend(self._stop_en_la_vela_de_entrada(order, bar))
        return events

    def _stop_en_la_vela_de_entrada(self, order: Order, bar: Bar) -> list[PositionClosed]:
        """Cierra en ESTA vela una posición que ha entrado intravela y ya tenía el stop roto.

        Solo aplica a las entradas `THIS_STOP`. Una compra `THIS_CLOSE` se ejecuta al cierre
        de la vela, así que el recorrido de esa vela ya había pasado cuando la posición
        existe: mirarle el mínimo sería cerrarla por algo anterior a su nacimiento.

        Con la ruptura es al revés. La orden salta en el máximo de la vela anterior, en
        mitad del minuto, y el minuto puede seguir bajando después: en el fixture dorado, la
        vela que rompe a 412,508 se va luego a 412,2011 con el stop en 412,437. No se sabe
        en qué orden ocurrieron el máximo y el mínimo dentro del minuto, y ante esa duda se
        elige la lectura desfavorable —entró y saltó—, que es la única que no puede inflar
        el resultado. Dejarla viva sería regalarle a la estrategia las velas que la habrían
        tumbado.

        order  : Order — la orden que acaba de ejecutarse.
        bar    : Bar   — vela en la que ha entrado.
        return : list[PositionClosed] — el cierre, o vacía si el stop aguantó.
        """
        position = self._positions.get((order.symbol, order.tag))
        if position is None or position.stop_price <= 0 or bar.low > position.stop_price:
            return []
        return [self._close(position, position.stop_price, bar, "stop")]

    def _execute_pending(self, bar: Bar) -> list[Fill | PositionClosed | OrderRejected]:
        """Ejecuta en la apertura de esta vela las órdenes que quedaron esperando.

        Aquí llegan solo las de `Timing.NEXT_OPEN`: las de esta misma vela ya las resolvió
        `settle` cuando su vela cerró.

        bar    : Bar  — vela en cuya apertura se ejecuta.
        return : list — un `Fill` por orden ejecutada de este símbolo, más el
                        `PositionClosed` correspondiente cuando la orden es una venta
                        que efectivamente cierra una posición, y un `OrderRejected`
                        por cada venta que no se puede ejecutar.
        """
        events: list[Fill | PositionClosed | OrderRejected] = []
        pending, self._pending = self._pending, []

        for order in pending:
            if order.symbol != bar.symbol:
                self._pending.append(order)
                continue

            referencia = bar.open
            sin_diferencial = False
            if order.limit_price is not None and order.side is Side.BUY:
                que_hacer, caducada = self._try_limit(order, bar)
                if que_hacer == "esperar":
                    continue
                if que_hacer == "caducar":
                    events.append(caducada)
                    continue
                # "ejecutar": sigue por el camino normal, pero al precio del límite.
                #
                # Limitada: **no se cruza el diferencial**, que es su punto entero. Se
                # ejecuta a su precio, o mejor si la vela abrió por debajo. Cobrarle
                # deslizamiento sería cobrarle justo lo que evita.
                referencia = min(bar.open, order.limit_price)
                sin_diferencial = True

            events.extend(
                self._apply(order, bar, referencia, cruza_diferencial=not sin_diferencial)
            )
        return events

    def _apply(
        self,
        order: Order,
        bar: Bar,
        referencia: float,
        cruza_diferencial: bool = True,
    ) -> list[Fill | PositionClosed | OrderRejected]:
        """Abre o cierra una posición con esta orden, a este precio bruto.

        Es la contabilidad, y es la misma se ejecute la orden en la apertura de la vela
        siguiente o dentro de la vela que la produjo: quien decide el precio es quien llama.

        order  : Order — orden a ejecutar.
        bar    : Bar   — vela en la que se ejecuta; de ella salen el sello de tiempo y las
                 excursiones iniciales de la posición.
        referencia : float — precio BRUTO, antes de deslizamiento y comisión.
        cruza_diferencial : bool — si se cobra deslizamiento. Falso en las limitadas, que
                 existen justamente para no cruzarlo.
        return : list — hechos producidos.
        """
        events: list[Fill | PositionClosed | OrderRejected] = []

        if order.side is Side.BUY:
            price = (
                self.execution.apply_slippage(referencia, order.side, bar.symbol)
                if cruza_diferencial
                else referencia
            )
            commission = self.execution.commission(order.shares)
            self._cash -= order.shares * price + commission
            self._positions[(order.symbol, order.tag)] = _OpenPosition(
                # El bruto se cuenta contra lo que DE VERDAD se pagó antes de costes.
                # Medirlo contra la apertura de la vela cuando la compra fue al límite
                # descuadra el desglose: el bruto sale peor de lo que fue y el
                # deslizamiento absorbe la diferencia.
                raw_entry_price=referencia,
                entry_commission=commission,
                symbol=order.symbol,
                shares=order.shares,
                entry_price=price,
                stop_price=order.stop_price or 0.0,
                initial_stop=order.stop_price or 0.0,
                signal_id=order.signal_id,
                tag=order.tag,
                high_water=bar.high,
                low_water=bar.low,
            )
            events.append(
                Fill(
                    ts=bar.ts,
                    signal_id=order.signal_id,
                    order_id=order.order_id,
                    symbol=order.symbol,
                    side=order.side,
                    shares=order.shares,
                    price=price,
                    tag=order.tag,
                )
            )
            return events

        # Venta enviada por submit(): no se modelan cortos ni cierres parciales, así
        # que aquí se decide de forma explícita qué pasa en los dos casos que el diseño
        # no contempla. La venta busca SU tramo por etiqueta: cerrar el tramo agresivo
        # no puede liquidar el conservador, que tiene otro stop y otra entrada.
        position = self._positions.get((order.symbol, order.tag))
        if position is None:
            # No fabricar un corto que nada en el bróker sabe seguir (ni MAE/MFE
            # ni stop). Antes se avisaba con `warnings.warn` y se descartaba la
            # orden sin más: el aviso no llega al motor, así que la clave
            # anti-duplicado de esta venta se quedaba viva para siempre y la
            # siguiente posición del símbolo ya no se podía cerrar por estrategia.
            # Es un camino alcanzable: basta con que el stop salte en la misma
            # vela en la que iba a ejecutarse la venta de salida.
            events.append(
                OrderRejected(
                    ts=bar.ts,
                    signal_id=order.signal_id,
                    order_id=order.order_id,
                    symbol=order.symbol,
                    reason=(
                        f"venta sin tramo abierto con etiqueta '{order.tag}' en "
                        f"{order.symbol}: no se modelan posiciones cortas"
                    ),
                )
            )
            return events

        if order.shares != position.shares:
            # Ídem para un cierre parcial: en vez de inventar una posición
            # remanente cuyo seguimiento de excursiones no está modelado, se
            # liquida la posición completa y se avisa del descuadre.
            warnings.warn(
                f"venta parcial de {order.symbol}: la orden pedía "
                f"{order.shares} acciones y la posición tiene {position.shares}; "
                "se cierra la posición completa porque el Plan 1 no modela "
                "cierres parciales",
                stacklevel=2,
            )

        # `_close` aplica el deslizamiento y la comisión por su cuenta: se le pasa
        # el precio bruto para no contarlos dos veces.
        #
        # El motivo sale de la propia orden cuando lo trae. Así, una venta por
        # sobrecompra del estocástico se registra como "stochastic" y no como un
        # genérico "sell_order", y el informe puede separar esa salida del resto sin
        # tener que adivinarlo por el instante en que ocurrió.
        motivo = _exit_reason(order.reason)
        closed = self._close(position, referencia, bar, motivo)
        events.append(
            Fill(
                ts=bar.ts,
                signal_id=order.signal_id,
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                shares=closed.shares,
                price=closed.exit_price,
                tag=order.tag,
            )
        )
        events.append(closed)
        return events

    def _try_limit(self, order: Order, bar: Bar) -> tuple[str, "OrderCancelled | None"]:
        """Decide qué pasa con una orden limitada en esta vela.

        Una limitada **no persigue al precio**: solo se ejecuta si el recorrido de la vela
        baja hasta ella. MEDIDO el 2026-09-02 sobre 9.186 operaciones: de los 6,43 de coste
        por operación, 3,97 son deslizamiento —el 62 %— y salen de comprar en la apertura de
        la vela siguiente sea cual sea. La estrategia entra en una zona de soporte, un nivel
        conocido de antemano: es justo donde una limitada tiene sentido.

        order  : Order — la orden con `limit_price`.
        bar    : Bar — vela a evaluar.
        return : tuple[str, OrderCancelled | None] — ("ejecutar", None) si la vela la toca,
                 ("esperar", None) si sigue viva, ("caducar", evento) si se rinde.
        """
        if bar.low <= order.limit_price:
            self._limit_age.pop(order.order_id, None)
            return ("ejecutar", None)

        edad = self._limit_age.get(order.order_id, 0) + 1
        if edad < max(1, self.execution.limit_ttl_bars):
            self._limit_age[order.order_id] = edad
            self._pending.append(order)
            return ("esperar", None)

        self._limit_age.pop(order.order_id, None)
        return ("caducar", OrderCancelled(
            ts=bar.ts,
            signal_id=order.signal_id,
            order_id=order.order_id,
            symbol=order.symbol,
            reason=(
                f"orden limitada caducada: el precio no bajó hasta {order.limit_price:.2f} "
                f"en {self.execution.limit_ttl_bars} velas"
            ),
        ))

    def _check_stops(self, bar: Bar) -> list[PositionClosed]:
        """Comprueba si el recorrido de la vela ha perforado el stop de algún tramo.

        Se recorren TODOS los tramos del símbolo, cada uno contra el suyo: en la variante
        mixta el stop agresivo es amplio y el conservador ajustado, y esa diferencia es
        justo lo que la variante existe para medir. Un solo stop por símbolo cerraría los
        dos a la vez y la comparación no diría nada.

        bar    : Bar — vela a evaluar.
        return : list[PositionClosed] — un cierre por tramo cuyo stop saltó.
        """
        cierres = []
        for position in self._tranches_of(bar.symbol):
            if position.stop_price <= 0 or bar.low > position.stop_price:
                continue
            # Un hueco a la baja no respeta el precio del stop: se sale en la apertura.
            exit_price = min(bar.open, position.stop_price)
            motivo = "trailing_stop" if position.trailing else "stop"
            cierres.append(self._close(position, exit_price, bar, motivo))
        return cierres

    def modify_stop(self, symbol: str, tag: str, new_stop: float) -> list[StopModified]:
        """Mueve el stop protector de un tramo vivo.

        Segunda barrera contra el trailing que retrocede: el gestor no lo propone y este
        método tampoco lo aceptaría. Es la clase de error que solo se descubre con dinero
        puesto —ampliar el riesgo de una posición que ya va a favor, sin que nadie lo haya
        decidido—, así que se comprueba en los dos sitios.

        symbol   : str   — ticker.
        tag      : str   — etiqueta del tramo.
        new_stop : float — precio nuevo del stop.
        return   : list[StopModified] — un evento si el stop se movió; vacía si no había
                   tramo con esa etiqueta o si el stop nuevo no mejoraba el actual.
        """
        position = self._positions.get((symbol, tag))
        if position is None or new_stop <= position.stop_price:
            return []

        anterior = position.stop_price
        position.stop_price = new_stop
        position.trailing = True
        return [
            StopModified(
                ts=self._last_ts,
                signal_id=position.signal_id,
                symbol=symbol,
                tag=tag,
                old_stop=anterior,
                new_stop=new_stop,
                reason="trailing",
            )
        ]

    def close_all(self, bars: dict[str, Bar], reason: str) -> list[PositionClosed]:
        """Cierra todas las posiciones abiertas, cada una a SU PROPIA vela.

        Antes se recibía una única vela y se aplicaba a todas las posiciones abiertas,
        aunque esa vela solo pudiera ser de un símbolo: con TSLA y NVDA abiertas a la
        vez, el cierre forzado liquidaba una al precio de cierre de la otra, falseando
        exit_price, pnl y r_multiple. Es la misma causa raíz que ya se corrigió en
        `on_bar` para el seguimiento de excursiones, pero aquí era más grave porque la
        vela ajena se usaba como PRECIO DE SALIDA, no solo para MAE/MFE.

        Si una posición no tiene vela en el mapa —no ha llegado ninguna vela de ese
        símbolo en esta vuelta—, se deja abierta y se avisa: inventar un precio de
        salida es peor que no cerrar. En la práctica no debería ocurrir mientras quien
        llama (el motor) mantenga la última vela vista de cada símbolo de la
        watchlist, pero el bróker no debe asumirlo silenciosamente.

        Lo usa el cierre forzado y el fin de datos.

        bars   : dict[str, Bar]      — última vela conocida de cada símbolo, por ticker.
        reason : str                 — motivo, que viaja tal cual a
                                        `PositionClosed.exit_reason`. Hoy quien llama
                                        pasa "force_close" o "end_of_data"; el bróker no
                                        valida la lista porque no le corresponde saber
                                        qué vías de cierre existen.
        return : list[PositionClosed] — un cierre por cada posición que se pudo liquidar;
                                        las que no tenían vela quedan abiertas.
        """
        closed = []
        for position in list(self._positions.values()):
            bar = bars.get(position.symbol)
            if bar is None:
                warnings.warn(
                    f"cierre de {position.symbol} (tramo '{position.tag}') omitido: no hay "
                    f"vela de este símbolo para liquidarlo (motivo: {reason}); la posición "
                    f"queda abierta",
                    stacklevel=2,
                )
                continue
            position.track(bar)
            closed.append(self._close(position, bar.close, bar, reason))
        return closed

    def _close(
        self,
        position: _OpenPosition,
        raw_exit: float,
        bar: Bar,
        reason: str,
    ) -> PositionClosed:
        """Liquida una posición y calcula su resultado completo.

        position : _OpenPosition — posición a cerrar.
        raw_exit : float         — precio de salida antes de deslizamiento.
        bar      : Bar           — vela en la que se cierra.
        reason   : str           — motivo del cierre.
        return   : PositionClosed — evento con pnl, r_multiple, mae y mfe ya calculados.
        """
        exit_price = self.execution.apply_slippage(raw_exit, Side.SELL, position.symbol)
        commission = self.execution.commission(position.shares)
        self._cash += position.shares * exit_price - commission
        del self._positions[(position.symbol, position.tag)]

        # Bruto, comisiones y deslizamiento por separado. El neto es la resta de los tres, y
        # coincide con lo que se movió la caja --incluida la comisión de ENTRADA, que antes
        # salía del efectivo pero no del P&L informado y hacía que cada operación se contara
        # mejor de lo que fue.
        gross_pnl = (raw_exit - position.raw_entry_price) * position.shares
        comisiones = position.entry_commission + commission
        deslizamiento = (
            (position.entry_price - position.raw_entry_price) + (raw_exit - exit_price)
        ) * position.shares
        pnl = gross_pnl - comisiones - deslizamiento
        # R se mide contra el stop INICIAL, no contra el vigente: el riesgo de una
        # operación es el que se asumió al entrar. Con el stop trailado, una posición cuyo
        # stop ha subido casi hasta la entrada saldría con un múltiplo enorme que no
        # corresponde a ningún riesgo real.
        risk_per_share = position.entry_price - position.initial_stop
        r_multiple = (
            (exit_price - position.entry_price) / risk_per_share if risk_per_share > 0 else 0.0
        )

        event = PositionClosed(
            ts=bar.ts,
            signal_id=position.signal_id,
            symbol=position.symbol,
            shares=position.shares,
            entry_price=position.entry_price,
            exit_price=exit_price,
            pnl=pnl,
            gross_pnl=gross_pnl,
            commission=comisiones,
            slippage=deslizamiento,
            r_multiple=r_multiple,
            exit_reason=reason,
            mae=max(0.0, position.entry_price - position.low_water),
            mfe=max(0.0, position.high_water - position.entry_price),
            tag=position.tag,
        )
        self.closed_trades.append(event)
        return event
