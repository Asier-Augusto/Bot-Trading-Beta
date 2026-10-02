"""Motor: orquesta el ciclo de decisión.

El camino de decisión son llamadas directas y síncronas; en paralelo se emiten eventos
al bus para los observadores. La distinción es mecánica: la llamada devuelve algo que
se usa en la línea siguiente, el `emit` no devuelve nada y nadie lo espera.

Si se borraran todas las líneas `emit`, el bot operaría exactamente igual. Solo se
quedaría ciego.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from bot.contracts.broker import BarDrivenBroker, Broker
from bot.contracts.feed import MarketDataFeed
from bot.contracts.history import HistoricalSource
from bot.contracts.strategy import IndicatorSpec, Strategy
from bot.core.bus import EventBus
from bot.core.clock import SimulatedClock
from bot.core.context import MarketContext, Position, aggregate_positions
from bot.core.idle import IdleAccumulator
from bot.core.order_book import OrderBook
from bot.core.indicator_set import IndicatorSet
from bot.core.events import (
    BarClosed,
    Event,
    IntentRaised,
    OrderSubmitted,
    PositionOpened,
    RiskBlocked,
    SignalEvaluated,
    SizeComputed,
    SystemEvent,
)
from bot.core.scoring import LevelThresholds, SignalLevel
from bot.core.session_closer import SessionCloser
from bot.core.session import SessionSchedule
from bot.indicators.vwap import VWAP
from bot.support.provider import SupportProvider
from bot.core.types import Bar, Order
from bot.indicators.aggregator import BarAggregator
from bot.risk.manager import RiskManager
from bot.risk.trailing import TrailingStopManager

class Engine:
    """El único componente que conoce a todos los demás."""

    def __init__(
        self,
        clock,
        bus: EventBus,
        feed: MarketDataFeed,
        broker: Broker,
        strategy: Strategy,
        risk: RiskManager,
        schedule: SessionSchedule,
        watchlist: list[str],
        indicator_specs: list[IndicatorSpec],
        levels: LevelThresholds,
        idle_summary_minutes: int = 60,
        strategy_params: dict | None = None,
        config_hash: str = "",
        history: "HistoricalSource | None" = None,
        supports: "SupportProvider | None" = None,
        resistances: "SupportProvider | None" = None,
        trailing: TrailingStopManager | None = None,
        shadow=None,
    ) -> None:
        """Crea el motor.

        clock                : Clock — reloj inyectado.
        bus                  : EventBus — bus de observación.
        feed                 : MarketDataFeed — fuente de ticks.
        broker               : Broker — ejecutor de órdenes.
        strategy             : Strategy — la estrategia activa.
        risk                 : RiskManager — cadena de filtros y dimensionamiento.
        schedule             : SessionSchedule — franjas horarias.
        watchlist            : list[str] — símbolos a vigilar.
        indicator_specs      : list[IndicatorSpec] — lo que pide la estrategia.
        levels               : LevelThresholds — umbrales de nivel de registro.
        idle_summary_minutes : int — cada cuánto se vuelca el resumen de IDLE.
        strategy_params      : dict | None — parámetros que viajan en el contexto.
        config_hash          : str — resumen de la configuración, para los eventos.
        history              : HistoricalSource | None — fuente de históricos con la que
                               calentar los indicadores antes del primer tick. None
                               arranca en frío, y el motor lo registra como aviso: sin
                               calentar, un RSI-14 de 5' no vale nada hasta 75 minutos
                               después de abrir, que es la franja de más volatilidad.
        supports             : SupportProvider | None — quien responde "¿dónde están las
                               zonas de soporte de este símbolo?". None deja
                               `ctx.support_zones` vacío, con lo que la condición `support`
                               de Moris puntúa cero y su veto tumba toda evaluación a IDLE.
                               Es lo correcto —no se falsean zonas para que el backtest
                               "funcione"— y es lo que permite que las pruebas de motor que
                               no van de soportes no tengan que montar uno.
        trailing             : TrailingStopManager | None — sube el stop de las posiciones
                               que ya van a favor. None lo desactiva y los stops se quedan
                               donde la entrada los puso.
        shadow               : ShadowTracker | None — rastreador contrafactual. Es un
                               OBSERVADOR: el motor solo le pasa los indicadores, que no
                               viajan por el bus, y publica lo que produzca. Si es None no
                               pasa nada; el bot opera igual.
        """
        self.clock = clock
        self.bus = bus
        self.feed = feed
        self.broker = broker
        self.strategy = strategy
        self.risk = risk
        self.schedule = schedule
        self.watchlist = watchlist
        self.levels = levels
        self.history = history
        self.supports = supports
        #: Techos estructurales. Solo se OBSERVAN: no entran en la puntuación ni en el veto.
        self.resistances = resistances
        #: VWAP de la sesión por símbolo. Vive aquí y no en el conjunto de indicadores a
        #: propósito: así no hay que declararlo en el perfil, el `config_hash` no cambia y
        #: esto no cuenta como una iteración de ajuste. No se decide nada con él.
        self._vwap: dict[str, VWAP] = {}
        self.trailing = trailing
        self.shadow = shadow
        self.idle_summary_minutes = idle_summary_minutes
        self.config_hash = config_hash
        # El resumen de configuración viaja con los parámetros para que la estrategia
        # pueda derivar identificadores deterministas sin conocer al motor. Va con doble
        # subrayado porque NO es un parámetro de la estrategia: es contexto del sistema
        # que se cuela por el único canal que la estrategia ya recibe.
        self.strategy_params = {**(strategy_params or {}), "__config_hash__": config_hash}

        self._specs = indicator_specs
        self._indicators = IndicatorSet(watchlist, indicator_specs)
        self._aggregators: dict[tuple[str, str], BarAggregator] = {}
        self._last_bar: dict[str, Bar] = {}
        #: Instante más alto que ha llevado un evento de vela. La última vela se sella con
        #: su `period_end`, POSTERIOR al último tick, así que sin esto "parada" —que usa
        #: `clock.now()`— llevaría un `ts` anterior al de la última vela.
        self._last_event_ts: dt.datetime | None = None
        #: Órdenes enviadas y aún no resueltas. Ver `bot.core.order_book`.
        self._orders = OrderBook(release=self.risk.release_order)

        self._idle = IdleAccumulator(idle_summary_minutes)
        #: El cierre de sesión, en su propio módulo: es lo más delicado que hay aquí y se
        #: lee, se prueba y se cambia mejor solo. Ver `bot.core.session_closer`.
        self._closer = SessionCloser(self)

        self._build_aggregators()

    def _build_aggregators(self) -> None:
        """Crea un agregador por cada par (símbolo, marco) que haya que construir.

        Los pares salen del conjunto de indicadores, más el marco de 1' de cada símbolo
        de la watchlist: ese siempre hace falta porque es el que dispara la evaluación de
        la estrategia, aunque ningún indicador lo use.

        return : None
        """
        for target, timeframe in self._indicators.aggregator_keys():
            self._aggregators.setdefault((target, timeframe), BarAggregator(target, timeframe))
        for symbol in self.watchlist:
            self._aggregators.setdefault((symbol, "1m"), BarAggregator(symbol, "1m"))

    def subscribed_symbols(self) -> list[str]:
        """Símbolos cuyos ticks hay que pedirle al feed.

        No coincide con la watchlist, y confundirlos era un fallo silencioso: la
        watchlist es el conjunto OPERABLE (lo que `_on_bar_closed` deja llegar a la
        estrategia), mientras que aquí se devuelve el conjunto OBSERVADO, que es la
        unión de la watchlist con los símbolos explícitos de los indicadores
        requeridos. Sin la unión, el motor construía el indicador y el agregador de la
        referencia del Nasdaq (ONEQ) pero el feed filtraba sus ticks, así que
        `ctx.indicator("rsi", "5m", symbol="ONEQ")` era None siempre y la condición
        `rsi_ndx` no podía cumplirse jamás.

        Meter la referencia en la watchlist no vale como arreglo: el motor la
        consideraría operable y la estrategia podría abrir posición en el ETF de
        referencia. Cada referencia observada consume una línea de mercado (§19 del
        diseño), no un hueco de operativa.

        return : list[str] — watchlist primero, en su orden, y después las referencias
                             en el orden en que la estrategia las declara. Sin
                             repetidos y determinista, para que dos corridas iguales
                             pidan lo mismo.
        """
        symbols = list(dict.fromkeys(self.watchlist))
        for spec in self._specs:
            if spec.symbol and spec.symbol not in symbols:
                symbols.append(spec.symbol)
        return symbols

    def run(self) -> None:
        """Ejecuta la sesión completa: consume el feed hasta agotarlo.

        El evento de "arranque" se emite en el primer tick recibido, no antes de
        empezar a leer el feed: con un reloj simulado, emitirlo antes haría que
        cayera en la época Unix (1970-01-01), la fecha de arranque de
        `SimulatedClock` antes del primer `advance_to`, en vez de en el día real de
        la sesión que se está reproduciendo. Eso mezclaba los "arranque" de todas
        las corridas en una misma carpeta `logs/1970-01-01/` y dejaba el
        `system.jsonl` del día simulado con solo la mitad del par arranque/parada.

        Si el feed no produce ningún tick, no se emite "arranque": sin datos no hubo
        sesión que abrir. Sí se llega a el cierre de sesión, así que "parada" se emite
        igual — sesión vacía, pero cerrada de forma consistente.

        return : None
        """
        # El bus reparte en un hilo aparte, y hay que ARRANCARLO. Sin esto, `emit` encola y
        # nadie recoge: en backtest se disimulaba porque el cierre hace un `drain()` síncrono
        # al terminar, pero en vivo significaba que no se escribía una sola línea en toda la
        # sesión y que la cola crecía hasta que la contrapresión empezaba a descartar velas.
        # Medido: un ensayo contra TWS de doce minutos no produjo ningún registro.
        #
        # Es idempotente: si alguien lo arrancó antes, no hace nada.
        self.bus.start()

        self.feed.connect()
        self.feed.subscribe(self.subscribed_symbols())
        self._drain_feed()

        try:
            arrancado = False
            for symbol, ts, price, volume in self.feed.stream():
                if isinstance(self.clock, SimulatedClock):
                    self.clock.advance_to(ts)
                if not arrancado:
                    self.bus.emit(
                        SystemEvent(ts=self.clock.now(), level="INFO", message="arranque")
                    )
                    self._warmup(ts)
                    arrancado = True
                self._on_tick(symbol, ts, price, volume)
                self._drain_feed()
        finally:
            self._drain_feed()
            # `SessionCloser` es el paracaídas de TODA la familia "la sesión murió a media
            # faena": libera claves anti-duplicado, avisa de posiciones huérfanas, emite
            # "parada" y desconecta el feed. Fuera del `finally`, una excepción de la
            # estrategia o un histórico corrupto se lo saltaban entero y dejaban el
            # `RiskState` contaminado para la sesión siguiente — un runner que lo
            # reutilice arrancaría el día siguiente bloqueando órdenes por claves de ayer.
            self._closer.finish()

    def _drain_feed(self) -> None:
        """Publica los hechos que el feed haya acumulado.

        Un feed de fichero no produce ninguno. El de IBKR sí, y son de los más importantes
        del registro: que la conexión se cayó, que un símbolo no tiene suscripción, y sobre
        todo que los datos son DIFERIDOS. Ese aviso es el que dice que las cifras de la
        sesión no son un resultado de trading, y sin publicarlo aquí solo se vería en la
        consola de quien la lanzó — que es justo quien ya lo sabe.

        return : None
        """
        if not hasattr(self.feed, "events"):
            return
        for hecho in self.feed.events():
            self.bus.emit(hecho)

    def _warmup(self, first_tick: dt.datetime) -> None:
        """Calienta los indicadores con histórico antes de procesar el primer tick.

        Se hace AQUÍ y no al construir el motor porque con reloj simulado el instante de
        arranque no se conoce hasta que llega el primer dato. El corte es ese primer tick:
        la fuente devuelve velas que cierran en él o antes, así que ninguna del futuro
        entra en el calentamiento.

        Si algún indicador sigue sin valor después, el aviso es de nivel WARNING y dice
        cuáles: operar con el RSI de 5' apagado es distinto de operar con él listo, y esa
        diferencia tiene que verse en el registro y no deducirse.

        first_tick : datetime — instante del primer tick de la sesión.
        return     : None
        """
        if self.history is None:
            self.bus.emit(
                SystemEvent(
                    ts=self.clock.now(),
                    level="WARNING",
                    message="sesión sin calentamiento: no hay fuente de históricos",
                    detail={
                        "consecuencia": (
                            "los indicadores arrancan en frío; un RSI-14 sobre velas de "
                            "5' no tendrá valor hasta 75 minutos después de abrir"
                        )
                    },
                    config_hash=self.config_hash,
                )
            )
            return

        resumen = self._indicators.warmup(self.history, first_tick)
        if hasattr(self.history, "events"):
            for hecho in self.history.events():
                self.bus.emit(hecho)
        self.bus.emit(
            SystemEvent(
                ts=self.clock.now(),
                level="WARNING" if resumen["not_ready"] else "INFO",
                message="calentamiento de indicadores",
                detail=resumen,
                config_hash=self.config_hash,
            )
        )

    def _on_tick(
        self, symbol: str, ts: dt.datetime, price: float | None, volume: int
    ) -> None:
        """Procesa un tick: agrega, actualiza indicadores y evalúa si cerró vela.

        Un tick con `price` a None es un LATIDO: solo avanza el reloj de los agregadores. Ver
        `MarketDataFeed.stream`.

        symbol : str          — ticker del tick; se ignora en un latido.
        ts     : datetime     — instante.
        price  : float | None — precio, o None si es un latido.
        volume : int          — acciones negociadas; se ignora en un latido.
        return : None
        """
        if price is None:
            self._heartbeat(ts)
            return

        # Dos pasadas, y el orden importa.
        #
        # Primero se avanzan por RELOJ los agregadores de los demás símbolos: un símbolo
        # que no cotiza en este minuto tiene que producir igualmente su vela (§5.1), o sus
        # indicadores se congelan. Es el caso concreto de ONEQ, que no cotiza en la mitad
        # de los minutos de la sesión y es la referencia del Nasdaq de Moris. Sin esto, la
        # serie de 5' sobre la que se calcula su RSI no es la que se midió en el §7.2.
        #
        # Después se incorpora el tick a los agregadores propios. Al revés, la vela de
        # relleno de ONEQ del minuto en curso llegaría DESPUÉS de la vela de TSLA de ese
        # mismo minuto, y la estrategia evaluaría TSLA con la referencia de un minuto
        # antes.
        for (target, timeframe), aggregator in self._aggregators.items():
            if target == symbol:
                continue
            for bar in aggregator.on_clock(ts):
                self._on_bar_closed(bar)

        for (target, timeframe), aggregator in self._aggregators.items():
            if target != symbol:
                continue
            closed = aggregator.on_tick(ts, price, volume)
            if closed is not None:
                self._on_bar_closed(closed)

    def _heartbeat(self, ts: dt.datetime) -> None:
        """Avanza por reloj TODOS los agregadores, sin tocar ninguna vela.

        Es lo que hace que un símbolo que no cotiza siga produciendo velas en vivo. La vela
        que sale de aquí es de relleno —plana al cierre anterior y con volumen cero—, así que
        el latido no puede alterar precios ni volumen de nada: dice qué hora es y nada más.

        ts     : datetime — instante del latido.
        return : None
        """
        for aggregator in self._aggregators.values():
            for bar in aggregator.on_clock(ts):
                self._on_bar_closed(bar)

    def _update_vwap(self, bar: Bar) -> None:
        """Alimenta el VWAP de la sesión del símbolo. Observación pura.

        bar    : Bar — vela recién cerrada.
        return : None
        """
        if bar.timeframe != "1m":
            return
        self._vwap.setdefault(bar.symbol, VWAP()).update(bar)

    def _available_r(self, symbol: str, entry: float, stop: float, ts) -> tuple:
        """Cuánto recorrido tenía por delante la operación al entrar, en múltiplos de R.

        No decide nada: se apunta y ya. Ver el docstring de `PositionOpened`.

        symbol : str — ticker.
        entry  : float — precio de entrada.
        stop   : float — stop con el que se abrió.
        ts     : datetime — instante de la apertura.
        return : tuple[float | None, float | None] — (hasta el VWAP, hasta la resistencia).
                 None donde no se pudo medir, que no es lo mismo que cero.
        """
        riesgo = entry - stop
        if riesgo <= 0:
            return (None, None)

        vwap = self._vwap.get(symbol)
        hasta_vwap = (vwap.value - entry) / riesgo if vwap and vwap.value else None

        hasta_techo = None
        if self.resistances is not None:
            encima = [z for z in self.resistances.zones_for(symbol, ts) if z.low > entry]
            for _evento in self.resistances.events():
                pass
            if encima:
                hasta_techo = (min(z.low for z in encima) - entry) / riesgo
        return (hasta_vwap, hasta_techo)

    def _on_bar_closed(self, bar: Bar) -> None:
        """Ciclo completo sobre una vela cerrada: llamadas directas más eventos.

        bar    : Bar — vela recién cerrada.
        return : None
        """
        # --- llamadas directas: actualizar indicadores de este marco y símbolo ---
        self._indicators.update(bar)
        self._update_vwap(bar)
        if self._last_event_ts is None or bar.ts > self._last_event_ts:
            self._last_event_ts = bar.ts

        self.bus.emit(BarClosed(ts=bar.ts, bar=bar, config_hash=self.config_hash))

        if bar.symbol not in self.watchlist or bar.timeframe != "1m":
            return

        self._last_bar[bar.symbol] = bar
        self._process_broker_events(bar)

        if self.schedule.is_force_close_time(bar.ts):
            self._force_close()
            return

        # El stop tiene que estar donde toca ANTES de decidir nada nuevo: si la estrategia
        # abriera o cerrara en esta vela, lo haría sobre una posición cuya protección ya
        # está actualizada.
        self._review_trailing(bar)

        ctx = self._build_context(bar)
        intents = self.strategy.on_bar(ctx)             # llamada — devuelve
        self._emit_signal(bar, ctx)                     # evento  — nadie espera
        for hecho in self.strategy.drain_events():      # evento  — ídem
            self.bus.emit(hecho)

        for intent in intents:
            self.bus.emit(
                IntentRaised(
                    ts=bar.ts, signal_id=intent.signal_id, symbol=intent.symbol,
                    side=intent.side, reason=intent.reason, tag=intent.tag,
                    strategy_version=self.strategy.version, config_hash=self.config_hash,
                )
            )
            decision = self.risk.evaluate(intent, ctx)  # llamada — devuelve
            if not decision.approved:
                self.bus.emit(
                    RiskBlocked(
                        ts=bar.ts, signal_id=intent.signal_id, symbol=intent.symbol,
                        rule=decision.blocked_by, reason=decision.reason,
                        config_hash=self.config_hash,
                    )
                )
                continue

            self.bus.emit(
                SizeComputed(
                    ts=bar.ts, signal_id=intent.signal_id, symbol=intent.symbol,
                    requested=decision.requested_shares, final=decision.final_shares,
                    clipped_by=decision.clipped_by, reason=decision.reason,
                    config_hash=self.config_hash,
                )
            )
            order = decision.order
            # Se registra ANTES de enviarla, y por order_id, para poder liberar SU clave
            # anti-duplicado —y solo la suya— cuando esta orden concreta se resuelva.
            # El orden importa: un bróker que rechace de forma síncrona lanzando desde
            # `submit` dejaba si no la clave puesta por el gestor de riesgo y la orden sin
            # registrar, invisible incluso para el paracaídas del cierre. Consecuencia:
            # ninguna orden equivalente volvía a pasar el filtro en toda la sesión.
            self._orders.track(order)
            self.broker.submit(order)                   # llamada — devuelve
            self.bus.emit(
                OrderSubmitted(
                    ts=bar.ts, signal_id=order.signal_id, order_id=order.order_id,
                    symbol=order.symbol, side=order.side, shares=order.shares,
                    stop_price=order.stop_price, tag=order.tag,
                    config_hash=self.config_hash,
                )
            )

        # Y lo que se resuelve DENTRO de esta vela —el rebote comprado a su cierre, la
        # ruptura que salta en el máximo de la vela anterior, la salida por sobrecompra— se
        # ejecuta ahora, no en la apertura de la siguiente. Ver `Timing`.
        if isinstance(self.broker, BarDrivenBroker):
            self._publish_broker_events(self.broker.settle(bar))

    def _review_trailing(self, bar: Bar) -> None:
        """Sube los stops que el trailing considere que deben subir.

        El motor es quien habla con el bróker; el gestor solo decide. Esa separación es lo
        que permite probar la regla del trailing —que nunca baja un stop— sin montar un
        bróker, y lo que deja el camino listo para que en IBKR esto sea una modificación de
        una orden real y no una variable en memoria.

        bar    : Bar — vela recién cerrada.
        return : None
        """
        if self.trailing is None:
            return

        tramos = [p for p in self.broker.positions() if p.symbol == bar.symbol]
        if not tramos:
            return

        atr = self._indicators.values_for(bar.symbol).get(("atr", "5m", bar.symbol))
        for symbol, tag, nuevo in self.trailing.review(tramos, bar, atr):
            for evento in self.broker.modify_stop(symbol, tag, nuevo):
                self.bus.emit(
                    dataclasses.replace(evento, ts=bar.ts, config_hash=self.config_hash)
                )

    def _process_broker_events(self, bar: Bar) -> None:
        """Avanza el bróker una vela, drena lo asíncrono y publica todo lo que produzca.

        Son las dos vías del contrato y se consultan las dos. `on_bar` es la del
        backtest: el bróker avanza cuando avanza la vela. `drain_events` es la de un
        bróker asíncrono como el de IBKR, cuyas ejecuciones llegan por devolución de
        llamada en otro hilo y se recogen aquí, en el del motor. Un bróker que solo
        implemente una de las dos funciona igual: la otra no aporta nada.

        bar    : Bar — vela recién cerrada.
        return : None
        """
        producidos = []
        if isinstance(self.broker, BarDrivenBroker):
            producidos.extend(self.broker.on_bar(bar))
        producidos.extend(self.broker.drain_events())
        self._publish_broker_events(producidos)

    def _publish_broker_events(self, producidos: list) -> None:
        """Publica hechos del bróker y aplica sus consecuencias al estado de riesgo.

        Está separado de `_process_broker_events` porque hay dos momentos en los que el
        bróker produce hechos dentro de la misma vela: al empezar —stops y órdenes que
        esperaban a esta apertura— y al terminar, cuando `settle` ejecuta lo que la
        estrategia acaba de decidir. Las consecuencias son las mismas en los dos, y
        repetirlas era la forma segura de que una se olvidara de la otra.

        producidos : list[Event] — hechos devueltos por el bróker.
        return     : None
        """
        for event in producidos:
            self.bus.emit(event)
            kind = type(event).__name__
            resuelta = (
                self._orders.resolve(event.order_id) if self._orders.resolves(kind) else None
            )
            if kind == "PositionClosed":
                self._apply_close(event)
            elif kind == "Fill" and event.side.value == "BUY":
                self._emit_position_opened(event, resuelta)
                # Vista AGREGADA por símbolo: los tramos de una misma entrada cuentan como
                # una sola posición a efectos del límite de una por símbolo (§7.1). Guardar
                # aquí el último tramo, como se hacía, haría que el segundo tramo de una
                # mixta borrara al primero del estado de riesgo.
                por_simbolo: dict[str, list] = {}
                for position in self.broker.positions():
                    por_simbolo.setdefault(position.symbol, []).append(position)
                for symbol, tramos in por_simbolo.items():
                    self.risk.state.open_positions[symbol] = aggregate_positions(tramos)

    def _apply_close(self, event) -> None:
        """Aplica al estado de riesgo el cierre de una posición.

        Único punto donde se descuenta una posición y se acumula su resultado. Toda vía
        de cierre —stop, venta enviada por la estrategia, cierre forzado de la sesión,
        fin de datos, y las que traiga el futuro— pasa por aquí. Tenerlo repetido en tres
        sitios era la forma más fácil de que una vía nueva se olvidara de una de las dos
        líneas: si falta la primera, el símbolo se queda bloqueado por
        `OnePositionPerSymbolFilter` sin tener posición; si falta la segunda, el kill
        switch deja de ver esas pérdidas.

        Con tramos, el símbolo NO se borra sin más: cerrar el tramo agresivo mientras el
        conservador sigue vivo tiene que dejar el símbolo en `open_positions`, o los filtros
        de riesgo creerían que no hay nada abierto y dejarían abrir una entrada nueva sobre
        una posición que sigue en el mercado. Se reconstruye la vista agregada desde el
        bróker, que es la única fuente que sabe qué queda vivo.

        event  : PositionClosed — el cierre que acaba de producirse.
        return : None
        """
        self.risk.state.daily_pnl += event.pnl
        vivos = [p for p in self.broker.positions() if p.symbol == event.symbol]
        agregada = aggregate_positions(vivos)
        if agregada is None:
            self.risk.state.open_positions.pop(event.symbol, None)
        else:
            self.risk.state.open_positions[event.symbol] = agregada

    def _emit_position_opened(self, fill, order=None) -> None:
        """Publica la apertura de una posición al ejecutarse su compra.

        El evento existía desde el Plan 1 y nadie lo producía. Con tramos deja de ser
        cosmético: es el único registro de que una entrada tuvo dos patas, con el precio y
        el stop de cada una. Sin él, la única forma de reconstruirlo sería cruzar los
        `Fill` con los `PositionClosed`, y una posición que siga abierta al leer los logs no
        aparecería por ninguna parte.

        fill   : Fill — ejecución de compra recién recibida.
        order  : Order | None — la orden que la produjo, si el motor la tenía registrada.
                 Es el respaldo para una posición que ya no está viva: desde que una
                 entrada intravela puede saltar por su stop en la MISMA vela, preguntarle
                 al bróker por ella devolvía nada y la apertura no se registraba. Una
                 operación completa desaparecía así de los informes y del visor, que la
                 dibuja a partir de este evento.
        return : None
        """
        abierta = next(
            (
                p
                for p in self.broker.positions()
                if p.symbol == fill.symbol and p.tag == fill.tag
            ),
            None,
        )
        if abierta is None:
            if order is None:
                return
            abierta = Position(
                symbol=fill.symbol,
                shares=fill.shares,
                entry_price=fill.price,
                stop_price=order.stop_price or 0.0,
                tag=fill.tag,
                signal_id=fill.signal_id,
            )
        hasta_vwap, hasta_techo = self._available_r(
            abierta.symbol, abierta.entry_price, abierta.stop_price, fill.ts
        )
        self.bus.emit(
            PositionOpened(
                ts=fill.ts,
                signal_id=fill.signal_id,
                symbol=abierta.symbol,
                shares=abierta.shares,
                entry_price=abierta.entry_price,
                stop_price=abierta.stop_price,
                tag=abierta.tag,
                r_to_vwap=hasta_vwap,
                r_to_resistance=hasta_techo,
                strategy_version=self.strategy.version,
                config_hash=self.config_hash,
            )
        )

    def cancel_order(self, order_id: str) -> None:
        """Retira una orden viva y cierra su ciclo de vida.

        Es la única de las tres vías de resolución que arranca en el motor y no en el
        bróker. `Broker.cancel` no publica nada (no conoce el bus): devuelve los
        eventos, y es aquí donde se publican y donde se libera la clave anti-duplicado
        de la orden retirada.

        El `ts` lo pone el MOTOR, no el bróker. El bróker no tiene reloj —y no debe
        fingir que lo tiene—: el simulado fechaba la cancelación con la última vela que
        había visto, cuyo valor inicial es la época Unix, así que una cancelación anterior
        a la primera vela de la sesión escribía en `logs/1970-01-01/`. Sellar aquí también
        deja consistente el caso asíncrono: en IBKR la confirmación llega minutos después
        y el instante que interesa registrar es aquel en el que el bot se enteró.

        order_id : str  — identificador devuelto por `Broker.submit`.
        return   : None
        """
        for event in self.broker.cancel(order_id):
            sellado = dataclasses.replace(event, ts=self.clock.now())
            self.bus.emit(sellado)
            if self._orders.resolves(type(sellado).__name__):
                self._orders.resolve(sellado.order_id)

    def _build_context(self, bar: Bar) -> MarketContext:
        """Construye el contexto de solo lectura para la estrategia.

        bar    : Bar — vela recién cerrada.
        return : MarketContext
        """
        # Los tramos y su vista agregada salen de la MISMA fuente, el bróker, para que no
        # puedan discrepar: un contexto con posición agregada pero sin tramos haría que una
        # venta etiquetada dimensionara a cero y la posición no se pudiera cerrar.
        tramos = tuple(p for p in self.broker.positions() if p.symbol == bar.symbol)

        valores = self._indicators.values_for(bar.symbol)
        if self.shadow is not None:
            # El rastreador es un observador y los indicadores no viajan por el bus: sin esto
            # no podría poner un stop imaginario ni aplicar la salida real, que son las dos
            # cosas que hacen que su análisis signifique algo.
            self.shadow.observe_indicators(bar.symbol, valores)

        return MarketContext(
            symbol=bar.symbol,
            bar=bar,
            now=bar.ts,
            indicators=valores,
            account=self.broker.account(),
            position=aggregate_positions(tramos),
            positions=tramos,
            support_zones=self._support_zones(bar),
            session_label=self.schedule.label_at(bar.ts),
            entries_allowed=self.schedule.entries_allowed_at(bar.ts),
            params=self.strategy_params,
        )

    def _support_zones(self, bar: Bar) -> tuple:
        """Zonas de soporte vigentes del símbolo de esta vela.

        El proveedor devuelve además los hechos que haya producido —un recálculo, un
        acierto de caché— y es aquí donde se publican: el proveedor no conoce el bus, igual
        que el bróker.

        bar    : Bar — vela que se está evaluando.
        return : tuple[SupportZone, ...] — vacía si no hay proveedor configurado.
        """
        if self.supports is None:
            return ()
        zonas = self.supports.zones_for(bar.symbol, bar.ts)
        for evento in self.supports.events():
            self.bus.emit(evento)
        return zonas

    def _emit_signal(self, bar: Bar, ctx: MarketContext) -> None:
        """Publica la evaluación, o la acumula si es IDLE.

        bar    : Bar           — vela evaluada.
        ctx    : MarketContext — contexto usado.
        return : None
        """
        # Por SÍMBOLO, nunca por instancia: una sola estrategia atiende a toda la
        # watchlist, así que leer una ranura compartida atribuiría a esta vela la
        # puntuación del símbolo evaluado justo antes.
        score = self.strategy.score_for(bar.symbol)
        if score is None:
            return

        if score.level is SignalLevel.IDLE:
            self._accumulate_idle(bar, score)
            return

        self.bus.emit(
            SignalEvaluated(
                ts=bar.ts,
                signal_id=self.strategy.signal_id_for(bar.symbol),
                symbol=bar.symbol,
                strategy=self.strategy.name,
                level=score.level.name,
                total=score.total,
                armed=score.armed,
                conditions=score.to_payload(),
                blocking=score.blocking,
                strategy_version=self.strategy.version,
                config_hash=self.config_hash,
            )
        )

    def _accumulate_idle(self, bar: Bar, score) -> None:
        """Suma una vela IDLE al acumulador y vuelca el resumen cuando toca.

        bar    : Bar         — vela evaluada.
        score  : SignalScore — resultado de la puntuación.
        return : None
        """
        self._idle.add(bar.symbol, bar.ts, score)
        if self._idle.due(bar.ts):
            self._flush_idle(bar.ts)

    def _flush_idle(self, ts: dt.datetime) -> None:
        """Publica los resúmenes acumulados y reinicia la ventana.

        ts     : datetime — instante del volcado.
        return : None
        """
        for resumen in self._idle.flush(ts, self.config_hash):
            self.bus.emit(resumen)

    def _force_close(self) -> None:
        """Cierra todas las posiciones por hora de cierre forzado.

        Usa `self._last_bar`: la última vela conocida de cada símbolo, para que el
        bróker liquide cada posición a SU propio precio y no al de la vela que
        disparó esta llamada (que solo puede ser de un símbolo). Con varios símbolos
        en la watchlist, la hora de cierre se cruza una vez por símbolo y por tanto
        esta función se llama varias veces seguidas; a partir de la primera ya no
        queda ninguna posición abierta y las llamadas siguientes no producen nada,
        lo cual es inocuo.

        return : None
        """
        for event in self.broker.close_all(self._last_bar, "force_close"):
            self.bus.emit(event)
            self._apply_close(event)

    def _flush_aggregators(self) -> None:
        """Cierra la vela en curso de cada agregador y la evalúa por el camino normal.

        Un agregador solo cierra una vela cuando llega un tick del periodo siguiente.
        Con N velas de datos eso deja la última en el aire: nunca se evalúa, ni en
        backtest ni en vivo al sonar la campana. Se fuerza aquí su cierre con un
        instante posterior al del periodo en curso, y la vela resultante pasa por
        `_on_bar_closed` igual que cualquier otra — si generase una orden, morirá sin
        ejecutarse por no haber vela siguiente, que es honesto y no se maquilla.

        return : None
        """
        for aggregator in self._aggregators.values():
            if aggregator.current is None:
                continue
            after_period = aggregator.current.ts + dt.timedelta(microseconds=1)
            for bar in aggregator.on_clock(after_period):
                self._on_bar_closed(bar)

    def _report_orphan_positions(self) -> None:
        """Registra como ERROR cualquier posición que siga abierta al cerrar la sesión.

        Es la barrera de la familia entera de fallos "posición que no se puede cerrar".
        Cerrar es responsabilidad de `close_all`; si aun así queda algo vivo, el único
        aviso sería un `warnings.warn` del bróker, que se pierde con facilidad y que
        nadie lee al día siguiente. Un SystemEvent de nivel ERROR queda en el log de
        sistema, con el símbolo y las acciones, y es lo primero que se ve al revisar
        la sesión.

        return : None
        """
        orphans = self.broker.positions()
        if not orphans:
            return

        self.bus.emit(
            SystemEvent(
                ts=self.clock.now(),
                level="ERROR",
                message="posiciones abiertas al cerrar la sesión",
                detail={
                    "positions": [
                        {"symbol": p.symbol, "shares": p.shares, "entry_price": p.entry_price}
                        for p in orphans
                    ]
                },
                config_hash=self.config_hash,
            )
        )

    def _release_unresolved_orders(self) -> None:
        """Libera las claves de las órdenes que terminan la sesión sin resolverse y avisa.

        El registro se encarga de soltarlas (ver `OrderBook.release_all`); lo que se hace
        aquí es dejar constancia, porque una orden que nunca se resolvió es un hecho que
        alguien tiene que poder ver al día siguiente.

        return : None
        """
        pendientes = self._orders.release_all()
        if not pendientes:
            return

        self.bus.emit(
            SystemEvent(
                ts=self.clock.now(),
                level="WARNING",
                message="órdenes sin resolver al cerrar la sesión",
                detail={
                    "orders": [
                        {"order_id": o.order_id, "symbol": o.symbol, "side": o.side.value,
                         "tag": o.tag}
                        for o in pendientes
                    ]
                },
                config_hash=self.config_hash,
            )
        )

    def _close_remaining_positions(self) -> None:
        """Liquida al fin de los datos lo que siga abierto.

        Una sola llamada con TODAS las últimas velas conocidas, para que cada posición se
        liquide a la SUYA (ver la crítica de `close_all`) y para que el estado de riesgo
        se actualice igual que en `_force_close`: sin esto, `open_positions` y `daily_pnl`
        quedarían desfasados justo al final de la sesión, que es cuando más importa para
        un kill switch entre sesiones.

        return : None
        """
        if not self.broker.positions():
            return
        for event in self.broker.close_all(self._last_bar, "end_of_data"):
            self.bus.emit(event)
            self._apply_close(event)

    def _close_shadows(self) -> None:
        """Cierra las operaciones sombra que sigan vivas al terminar la sesión.

        §10.3 lo exige: todas cerradas en el cierre forzado. Una sombra que sobreviviera a la
        sesión seguiría abierta al día siguiente con un precio de entrada de ayer.

        Se vacía el bus ANTES de cerrarlas. El rastreador vive en el hilo trabajador y va por
        detrás del motor: sin esto, las sombras se cerrarían al precio de una vela que el
        trabajador aún no ha entregado, y las velas que llegaran después las reabrirían.

        return : None
        """
        if self.shadow is None:
            return
        self.bus.drain()
        self.shadow.close_all("force_close")
        for hecho in self.shadow.events():
            self.bus.emit(hecho)

