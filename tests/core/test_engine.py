"""Pruebas del motor: el ciclo completo con llamadas directas y eventos intercalados."""

import datetime as dt

import pytest

from bot.adapters.brokers.simulated import ExecutionModel, SimulatedBroker
from bot.adapters.data.csv_replay import CsvReplayFeed
from bot.contracts.sink import EventSink
from bot.contracts.strategy import IndicatorSpec, Strategy
from bot.core.bus import EventBus
from bot.core.clock import SimulatedClock
from bot.core.engine import Engine
from bot.core.context import Position
from bot.core.events import Fill, PositionClosed, SystemEvent
from bot.core.scoring import ConditionResult, LevelThresholds, score_signal
from bot.core.session import SessionSchedule
from bot.core.types import Bar, Intent, Order, Shares, Side
from bot.risk.filters import DuplicateOrderFilter, RiskState, TradingWindowFilter
from bot.risk.manager import RiskManager
from bot.risk.sizers import PercentEquitySizer

# Importado solo por su efecto de registro: sin esto, `get_indicator("rsi")` falla si
# este módulo se recoge antes que tests/indicators/test_rsi.py, ya que el registro no
# tiene todavía un arranque que importe todas las implementaciones conocidas.
import bot.indicators.rsi  # noqa: F401

TZ = dt.timezone.utc
NIVELES = LevelThresholds()
HORARIO = SessionSchedule.from_dict(
    {
        "timezone": "UTC",
        "trading_windows": [{"from": "09:30", "to": "15:00", "allow_entries": True}],
        "danger_zones": [],
        "force_close": "15:45",
    }
)


class Recorder(EventSink):
    def __init__(self):
        self.events = []

    def handle(self, event):
        self.events.append(event)

    def close(self):
        pass


def _idle_score():
    """Puntuación de prueba por debajo de cualquier umbral: se registra como IDLE.

    El brief no registraba evaluación en las estrategias de prueba, con lo que
    `_emit_signal` se quedaba siempre en `score is None` y nunca emitía nada. Una
    estrategia real (Moris) sí llama a `record_evaluation` en cada evaluación; se
    corrige el mismo contrato aquí para que `SignalEvaluated` e `IdleSummary` se puedan
    probar de verdad.

    return : SignalScore — nivel IDLE, con una única condición sin cumplir.
    """
    condicion = ConditionResult(
        name="dummy", raw_value=0.0, threshold=1.0, score=0.0, weight=1.0, passed=False
    )
    return score_signal([condicion], NIVELES)


def _armed_score():
    """Puntuación de prueba con la condición cumplida: se registra como ARMED.

    return : SignalScore — nivel ARMED, con una única condición cumplida.
    """
    condicion = ConditionResult(
        name="dummy", raw_value=1.0, threshold=1.0, score=1.0, weight=1.0, passed=True
    )
    return score_signal([condicion], NIVELES)


class CompraEnLaTercera(Strategy):
    """Estrategia de prueba: compra en su tercera evaluación y nada más."""

    name = "prueba"
    version = "0.0.1"

    def __init__(self):
        self.calls = 0
        self.last_signal_id = ""

    def required_indicators(self):
        return [IndicatorSpec("rsi", "1m", params={"period": 2})]

    def on_bar(self, ctx):
        self.calls += 1
        self.last_signal_id = f"sig{self.calls}"
        if self.calls != 3:
            self.record_evaluation(ctx.symbol, _idle_score(), self.last_signal_id)
            return []
        self.record_evaluation(ctx.symbol, _armed_score(), self.last_signal_id)
        return [
            Intent(self.last_signal_id, ctx.symbol, Side.BUY, Shares(10), ctx.bar.close - 5,
                   "prueba", "base")
        ]


class Inmovil(Strategy):
    """Estrategia de prueba que nunca opera."""

    name = "inmovil"
    version = "0.0.1"

    def __init__(self):
        self.last_signal_id = ""

    def required_indicators(self):
        return []

    def on_bar(self, ctx):
        self.record_evaluation(ctx.symbol, _idle_score(), self.last_signal_id)
        return []


def escribir_csv(tmp_path, minutos: int = 8, precio_base: float = 400.0):
    filas = ["timestamp,symbol,open,high,low,close,volume"]
    for i in range(minutos):
        ts = dt.datetime(2026, 3, 10, 10, i, tzinfo=TZ).isoformat()
        p = precio_base + i
        filas.append(f"{ts},TSLA,{p},{p + 1},{p - 1},{p + 0.5},1000")
    ruta = tmp_path / "datos.csv"
    ruta.write_text("\n".join(filas) + "\n", encoding="utf-8")
    return ruta


def montar(tmp_path, strategy, minutos: int = 8):
    bus = EventBus()
    recorder = Recorder()
    bus.subscribe(recorder)
    broker = SimulatedBroker(
        starting_cash=100_000.0,
        execution=ExecutionModel(slippage_pct=0.0, commission_per_share=0.0, min_commission=0.0),
    )
    engine = Engine(
        clock=SimulatedClock(dt.datetime(2026, 3, 10, 10, 0, tzinfo=TZ), "UTC"),
        bus=bus,
        feed=CsvReplayFeed(escribir_csv(tmp_path, minutos)),
        broker=broker,
        strategy=strategy,
        risk=RiskManager(
            filters=[TradingWindowFilter()],
            sizer=PercentEquitySizer(pct=0.2),
            state=RiskState(starting_equity=100_000.0),
        ),
        schedule=HORARIO,
        watchlist=["TSLA"],
        indicator_specs=strategy.required_indicators(),
        levels=NIVELES,
    )
    return engine, broker, recorder


def test_el_motor_recorre_las_velas_y_evalua_la_estrategia(tmp_path):
    estrategia = CompraEnLaTercera()
    engine, _, _ = montar(tmp_path, estrategia)

    engine.run()

    assert estrategia.calls == 8


def test_una_intencion_llega_a_ser_posicion(tmp_path):
    engine, broker, _ = montar(tmp_path, CompraEnLaTercera())

    engine.run()

    # Se compró en la tercera vela y se cerró al final de la sesión de datos.
    assert len(broker.closed_trades) == 1
    assert broker.closed_trades[0].shares == 10


def test_el_motor_emite_eventos_en_cada_etapa(tmp_path):
    engine, _, recorder = montar(tmp_path, CompraEnLaTercera())

    engine.run()

    tipos = {type(e).__name__ for e in recorder.events}
    assert {"BarClosed", "SignalEvaluated", "IntentRaised", "OrderSubmitted", "Fill"} <= tipos


def test_el_signal_id_hila_la_senal_con_la_orden_y_el_cierre(tmp_path):
    # Sin este hilo no existe la etiqueta con la que entrenar nada después.
    engine, broker, recorder = montar(tmp_path, CompraEnLaTercera())

    engine.run()

    orden = next(e for e in recorder.events if type(e).__name__ == "OrderSubmitted")
    cierre = broker.closed_trades[0]

    assert orden.signal_id == "sig3"
    assert cierre.signal_id == "sig3"


def test_las_velas_idle_se_agregan_en_vez_de_escribirse(tmp_path):
    engine, _, recorder = montar(tmp_path, Inmovil())

    engine.run()

    resumenes = [e for e in recorder.events if type(e).__name__ == "IdleSummary"]
    assert len(resumenes) >= 1
    assert resumenes[-1].bars > 0


def test_el_cierre_forzado_liquida_lo_que_quede(tmp_path):
    engine, broker, recorder = montar(tmp_path, CompraEnLaTercera())

    engine.run()

    assert broker.positions() == []
    assert broker.closed_trades[0].exit_reason in {"force_close", "end_of_data", "stop"}


def test_un_sink_que_revienta_no_detiene_la_operativa(tmp_path):
    class Roto(EventSink):
        def handle(self, event):
            raise RuntimeError("disco lleno")

        def close(self):
            pass

    estrategia = CompraEnLaTercera()
    engine, broker, _ = montar(tmp_path, estrategia)
    engine.bus.subscribe(Roto())

    engine.run()

    assert estrategia.calls == 8
    assert len(broker.closed_trades) == 1
    assert engine.bus.stats.sink_errors > 0


# --- CAMBIO 2: la clave anti-duplicado se libera por ciclo de vida de ORDEN, no de
# posición. Estas dos pruebas son las que distinguen el arreglo correcto (RiskState.
# release por clave concreta, vía RiskManager.release_order) del clear() indiscriminado
# que el brief traía y que hubo que quitar. ---


class DosVecesMismaClave(Strategy):
    """Estrategia de prueba: lanza la misma intención (mismo símbolo, lado y tag) dos
    veces en evaluaciones distintas, sin que ninguna posición llegue a cerrarse entre
    medias."""

    name = "dos_veces"
    version = "0.0.1"

    def __init__(self):
        self.calls = 0
        self.last_signal_id = ""

    def required_indicators(self):
        return []

    def on_bar(self, ctx):
        self.calls += 1
        self.last_signal_id = f"sig{self.calls}"
        if self.calls in (2, 5):
            self.record_evaluation(ctx.symbol, _armed_score(), self.last_signal_id)
            return [
                Intent(self.last_signal_id, ctx.symbol, Side.BUY, Shares(10),
                       ctx.bar.close - 5, "prueba", "base")
            ]
        self.record_evaluation(ctx.symbol, _idle_score(), self.last_signal_id)
        return []


def test_tras_un_fill_la_clave_de_la_orden_se_libera_y_una_intencion_equivalente_vuelve_a_pasar(
    tmp_path,
):
    """Con el clear() que se quitó, o si la liberación solo ocurriera al cerrar la
    posición, la segunda intención (misma clave: TSLA, BUY, "base") quedaría bloqueada
    como duplicada, porque en este escenario ninguna posición llega a cerrarse. Con la
    liberación atada al Fill de la orden, la clave queda libre mucho antes de la
    segunda intención y esta pasa con normalidad.
    """
    estrategia = DosVecesMismaClave()
    bus = EventBus()
    recorder = Recorder()
    bus.subscribe(recorder)
    broker = SimulatedBroker(
        starting_cash=100_000.0,
        execution=ExecutionModel(slippage_pct=0.0, commission_per_share=0.0, min_commission=0.0),
    )
    engine = Engine(
        clock=SimulatedClock(dt.datetime(2026, 3, 10, 10, 0, tzinfo=TZ), "UTC"),
        bus=bus,
        feed=CsvReplayFeed(escribir_csv(tmp_path, 8)),
        broker=broker,
        strategy=estrategia,
        risk=RiskManager(
            filters=[TradingWindowFilter(), DuplicateOrderFilter()],
            sizer=PercentEquitySizer(pct=0.2),
            state=RiskState(starting_equity=100_000.0),
        ),
        schedule=HORARIO,
        watchlist=["TSLA"],
        indicator_specs=[],
        levels=NIVELES,
    )

    engine.run()

    bloqueos_duplicado = [
        e for e in recorder.events
        if type(e).__name__ == "RiskBlocked" and e.rule == "duplicate_order"
    ]
    envios = [e for e in recorder.events if type(e).__name__ == "OrderSubmitted"]

    assert bloqueos_duplicado == []
    assert len(envios) == 2
    assert ("TSLA", "BUY", "base") not in engine.risk.state.pending_order_keys


def test_liberar_la_clave_de_una_orden_no_libera_las_de_otras_ordenes_vivas(tmp_path):
    """Es lo que distingue el arreglo correcto del clear() que se quitó: liberar una
    orden concreta no debe soltar las claves de otras órdenes que siguen vivas. Se
    fabrican dos órdenes pendientes con tags distintos (como los dos tramos de una
    variante mixta) y se resuelve solo una con un Fill fabricado a mano.
    """
    engine, broker, _ = montar(tmp_path, Inmovil())

    orden_a = Order("orden-a", "TSLA", Side.BUY, 10, 395.0, "sigA", "tramo1")
    orden_b = Order("orden-b", "TSLA", Side.BUY, 10, 395.0, "sigB", "tramo2")
    engine._orders.track(orden_a)
    engine._orders.track(orden_b)
    engine.risk.state.pending_order_keys.add(("TSLA", "BUY", "tramo1"))
    engine.risk.state.pending_order_keys.add(("TSLA", "BUY", "tramo2"))

    fill_a = Fill(
        ts=dt.datetime(2026, 3, 10, 10, 1, tzinfo=TZ),
        order_id="orden-a", symbol="TSLA", side=Side.BUY, shares=10, price=400.0,
    )
    bar = Bar("TSLA", "1m", dt.datetime(2026, 3, 10, 10, 1, tzinfo=TZ), 400, 401, 399, 400, 1000)
    broker.on_bar = lambda b: [fill_a]

    engine._process_broker_events(bar)

    assert ("TSLA", "BUY", "tramo1") not in engine.risk.state.pending_order_keys
    assert ("TSLA", "BUY", "tramo2") in engine.risk.state.pending_order_keys
    assert "orden-a" not in engine._orders
    assert "orden-b" in engine._orders


class PideIndicadorInexistente(Strategy):
    """Estrategia de prueba que declara un indicador que no está registrado."""

    name = "pide_inexistente"
    version = "0.0.1"

    def required_indicators(self):
        return [IndicatorSpec("no_existe_de_verdad", "1m")]

    def on_bar(self, ctx):
        return []


def test_el_motor_falla_al_arrancar_si_la_estrategia_pide_un_indicador_no_registrado(tmp_path):
    # Fallar al arrancar con un mensaje claro es justo la garantía que evita reventar
    # a media sesión con dinero puesto: se comprueba que el mensaje nombra el
    # indicador pedido y enumera los que sí están disponibles.
    estrategia = PideIndicadorInexistente()
    bus = EventBus()
    broker = SimulatedBroker(
        starting_cash=100_000.0,
        execution=ExecutionModel(slippage_pct=0.0, commission_per_share=0.0, min_commission=0.0),
    )

    with pytest.raises(KeyError) as excinfo:
        Engine(
            clock=SimulatedClock(dt.datetime(2026, 3, 10, 10, 0, tzinfo=TZ), "UTC"),
            bus=bus,
            feed=CsvReplayFeed(escribir_csv(tmp_path, 2)),
            broker=broker,
            strategy=estrategia,
            risk=RiskManager(
                filters=[TradingWindowFilter()],
                sizer=PercentEquitySizer(pct=0.2),
                state=RiskState(starting_equity=100_000.0),
            ),
            schedule=HORARIO,
            watchlist=["TSLA"],
            indicator_specs=estrategia.required_indicators(),
            levels=NIVELES,
        )

    mensaje = str(excinfo.value)
    assert "no_existe_de_verdad" in mensaje
    assert "Disponibles" in mensaje


def _escribir_csv_dos_simbolos(tmp_path, minutos: int = 5):
    """CSV con dos símbolos y precios muy separados, para detectar contaminación
    cruzada si el motor confundiera la vela de uno con la del otro al liquidar."""
    filas = ["timestamp,symbol,open,high,low,close,volume"]
    for i in range(minutos):
        ts = dt.datetime(2026, 3, 10, 10, i, tzinfo=TZ).isoformat()
        p_tsla = 400.0 + i
        p_nvda = 800.0 + i * 2
        filas.append(f"{ts},TSLA,{p_tsla},{p_tsla + 1},{p_tsla - 1},{p_tsla + 0.5},1000")
        filas.append(f"{ts},NVDA,{p_nvda},{p_nvda + 1},{p_nvda - 1},{p_nvda + 0.5},1000")
    ruta = tmp_path / "dos_simbolos.csv"
    ruta.write_text("\n".join(filas) + "\n", encoding="utf-8")
    return ruta


class CompraUnaVezPorSimbolo(Strategy):
    """Estrategia de prueba: compra una vez por símbolo, en su primera evaluación, y
    no vuelve a tocarlo mientras la posición siga abierta."""

    name = "compra_una_vez"
    version = "0.0.1"

    def __init__(self):
        self.bought = set()
        self.last_signal_id = ""

    def required_indicators(self):
        return []

    def on_bar(self, ctx):
        self.last_signal_id = f"sig-{ctx.symbol}-{ctx.bar.ts.isoformat()}"
        if ctx.symbol in self.bought or ctx.position is not None:
            self.record_evaluation(ctx.symbol, _idle_score(), self.last_signal_id)
            return []
        self.bought.add(ctx.symbol)
        self.record_evaluation(ctx.symbol, _armed_score(), self.last_signal_id)
        return [
            Intent(self.last_signal_id, ctx.symbol, Side.BUY, Shares(10),
                   ctx.bar.close - 50, "prueba", "base")
        ]


def test_el_fin_de_datos_liquida_cada_simbolo_a_su_propio_precio_y_actualiza_el_riesgo(
    tmp_path,
):
    """Prueba de integración de la CRÍTICA (a través del motor completo, no solo del
    bróker) y del punto 3: el cierre de sesión debe dejar risk.state tan al día como lo deja
    _force_close, para que un runner que reutilice el mismo RiskState entre sesiones
    no arrastre posiciones fantasma ni un daily_pnl por debajo del real.
    """
    bus = EventBus()
    recorder = Recorder()
    bus.subscribe(recorder)
    broker = SimulatedBroker(
        starting_cash=200_000.0,
        execution=ExecutionModel(slippage_pct=0.0, commission_per_share=0.0, min_commission=0.0),
    )
    engine = Engine(
        clock=SimulatedClock(dt.datetime(2026, 3, 10, 10, 0, tzinfo=TZ), "UTC"),
        bus=bus,
        feed=CsvReplayFeed(_escribir_csv_dos_simbolos(tmp_path, 5)),
        broker=broker,
        strategy=CompraUnaVezPorSimbolo(),
        risk=RiskManager(
            filters=[TradingWindowFilter()],
            sizer=PercentEquitySizer(pct=0.1),
            state=RiskState(starting_equity=200_000.0),
        ),
        schedule=HORARIO,
        watchlist=["TSLA", "NVDA"],
        indicator_specs=[],
        levels=NIVELES,
    )

    engine.run()

    cierres = {c.symbol: c for c in broker.closed_trades}
    assert set(cierres) == {"TSLA", "NVDA"}
    # Precios muy separados a propósito: si una se cerrara con la vela de la otra,
    # estos números saldrían disparados.
    assert cierres["TSLA"].exit_price == pytest.approx(404.5)
    assert cierres["NVDA"].exit_price == pytest.approx(808.5)

    assert broker.positions() == []
    assert engine.risk.state.open_positions == {}
    assert engine.risk.state.daily_pnl == pytest.approx(
        cierres["TSLA"].pnl + cierres["NVDA"].pnl
    )


def test_el_cierre_forzado_con_varios_simbolos_no_duplica_ni_contamina_precios(tmp_path):
    """CAMBIO documentado en el punto 2 del encargo: con varios símbolos, la hora de
    cierre forzado se cruza una vez por símbolo, así que `_force_close` se llama
    varias veces seguidas dentro de la misma vuelta. La primera llamada liquida TODAS
    las posiciones (cada una a su propio precio); las siguientes no encuentran nada
    que cerrar y no producen ni eventos ni errores.
    """
    horario_estricto = SessionSchedule.from_dict(
        {
            "timezone": "UTC",
            "trading_windows": [{"from": "09:30", "to": "15:00", "allow_entries": True}],
            "danger_zones": [],
            "force_close": "10:04",
        }
    )
    bus = EventBus()
    recorder = Recorder()
    bus.subscribe(recorder)
    broker = SimulatedBroker(
        starting_cash=200_000.0,
        execution=ExecutionModel(slippage_pct=0.0, commission_per_share=0.0, min_commission=0.0),
    )
    engine = Engine(
        clock=SimulatedClock(dt.datetime(2026, 3, 10, 10, 0, tzinfo=TZ), "UTC"),
        bus=bus,
        feed=CsvReplayFeed(_escribir_csv_dos_simbolos(tmp_path, 5)),
        broker=broker,
        strategy=CompraUnaVezPorSimbolo(),
        risk=RiskManager(
            filters=[TradingWindowFilter()],
            sizer=PercentEquitySizer(pct=0.1),
            state=RiskState(starting_equity=200_000.0),
        ),
        schedule=horario_estricto,
        watchlist=["TSLA", "NVDA"],
        indicator_specs=[],
        levels=NIVELES,
    )

    engine.run()

    cierres_forzados = [
        e for e in recorder.events
        if type(e).__name__ == "PositionClosed" and e.exit_reason == "force_close"
    ]
    assert {c.symbol for c in cierres_forzados} == {"TSLA", "NVDA"}
    assert len(cierres_forzados) == 2  # ni una repetida, ni ninguna sin cerrar

    precios = {c.symbol: c.exit_price for c in cierres_forzados}
    assert precios["TSLA"] < 500.0   # nunca contaminado con el precio de NVDA (~800+)
    assert precios["NVDA"] > 500.0   # nunca contaminado con el precio de TSLA (~400+)

    assert broker.positions() == []
    assert engine.risk.state.open_positions == {}


def test_el_arranque_se_registra_con_la_fecha_real_de_la_primera_vela(tmp_path):
    # Antes, el arranque se emitía con el reloj todavía en la época Unix
    # (1970-01-01), previa al primer advance_to; debía caer en el día real de la
    # sesión, igual que la parada.
    engine, _, recorder = montar(tmp_path, Inmovil())

    engine.run()

    arranque = next(
        e for e in recorder.events
        if type(e).__name__ == "SystemEvent" and e.message == "arranque"
    )
    parada = next(
        e for e in recorder.events
        if type(e).__name__ == "SystemEvent" and e.message == "parada"
    )

    assert arranque.ts.date() == dt.date(2026, 3, 10)
    assert parada.ts.date() == dt.date(2026, 3, 10)


def test_con_el_feed_vacio_no_se_emite_arranque_pero_si_parada(tmp_path):
    # Sin ticks no hubo sesión que abrir: se omite "arranque". Sí se llega a
    # el cierre de sesión, así que "parada" se emite igual, con la sesión cerrada de forma
    # consistente aunque estuviera vacía.
    engine, _, recorder = montar(tmp_path, Inmovil(), minutos=0)

    engine.run()

    mensajes = [
        e.message for e in recorder.events if type(e).__name__ == "SystemEvent"
    ]
    assert "arranque" not in mensajes
    assert "parada" in mensajes


# --- B1: la liberación de la clave anti-duplicado tiene TRES vías, no una. El Fill ya
# estaba probado arriba; aquí se cubren el rechazo del bróker, la cancelación y el
# paracaídas del cierre (`SessionCloser`). ---


def _escribir_csv_con_desplome(tmp_path):
    """CSV de 6 velas de TSLA con un desplome en la tercera.

    Está diseñado para provocar el camino exacto del fallo: la venta de salida se
    encola en la vela 1 y, cuando iba a ejecutarse en la vela 2, el stop ya ha cerrado
    la posición dentro de esa misma vela (`_check_stops` corre antes que
    `_execute_pending`). Esa venta se queda sin posición que cerrar.

    tmp_path : Path — carpeta temporal de la prueba.
    return   : Path — ruta del CSV escrito.
    """
    velas = [
        (400.0, 400.5, 399.5, 400.0),
        (400.0, 400.5, 399.5, 400.0),
        (399.0, 399.5, 390.0, 395.0),   # perfora el stop de 398
        (395.0, 395.5, 394.5, 395.0),
        (395.0, 395.5, 394.5, 395.0),
        (395.0, 395.5, 394.5, 395.0),
    ]
    filas = ["timestamp,symbol,open,high,low,close,volume"]
    for minuto, (o, h, l, c) in enumerate(velas):
        ts = dt.datetime(2026, 3, 10, 10, minuto, tzinfo=TZ).isoformat()
        filas.append(f"{ts},TSLA,{o},{h},{l},{c},1000")
    ruta = tmp_path / "desplome.csv"
    ruta.write_text("\n".join(filas) + "\n", encoding="utf-8")
    return ruta


class CompraYVende(Strategy):
    """Estrategia de prueba: compra cuando no tiene posición y vende cuando la tiene.

    Reproduce lo que hará Moris en el Plan 2 (salidas por estocástico y trailing), que
    es lo que vuelve alcanzable el camino de la venta rechazada.
    """

    name = "compra_y_vende"
    version = "0.0.1"

    def __init__(self):
        self.last_signal_id = ""
        self.calls = 0

    def required_indicators(self):
        return []

    def on_bar(self, ctx):
        self.calls += 1
        self.last_signal_id = f"sig{self.calls}"
        self.record_evaluation(ctx.symbol, _armed_score(), self.last_signal_id)
        if ctx.position is None:
            return [
                Intent(self.last_signal_id, ctx.symbol, Side.BUY, Shares(10),
                       ctx.bar.close - 2, "entrada", "base")
            ]
        return [
            Intent(self.last_signal_id, ctx.symbol, Side.SELL, Shares(10),
                   None, "salida", "base")
        ]


def _montar_con_duplicados(tmp_path, strategy, ruta_csv):
    """Motor con el filtro anti-duplicado activo, que es el que sufre el fallo.

    tmp_path  : Path     — carpeta temporal.
    strategy  : Strategy — estrategia a montar.
    ruta_csv  : Path     — CSV de datos.
    return    : tuple    — (motor, bróker, grabador).
    """
    bus = EventBus()
    recorder = Recorder()
    bus.subscribe(recorder)
    broker = SimulatedBroker(
        starting_cash=100_000.0,
        execution=ExecutionModel(slippage_pct=0.0, commission_per_share=0.0, min_commission=0.0),
    )
    engine = Engine(
        clock=SimulatedClock(dt.datetime(2026, 3, 10, 10, 0, tzinfo=TZ), "UTC"),
        bus=bus,
        feed=CsvReplayFeed(ruta_csv),
        broker=broker,
        strategy=strategy,
        risk=RiskManager(
            filters=[TradingWindowFilter(), DuplicateOrderFilter()],
            sizer=PercentEquitySizer(pct=0.2),
            state=RiskState(starting_equity=100_000.0),
        ),
        schedule=HORARIO,
        watchlist=["TSLA"],
        indicator_specs=[],
        levels=NIVELES,
    )
    return engine, broker, recorder


def test_una_venta_rechazada_libera_su_clave_y_la_siguiente_salida_no_se_bloquea(tmp_path):
    """B1: sin el rechazo del bróker, la clave (TSLA, SELL, "base") se quedaba viva el
    resto de la sesión y DuplicateOrderFilter bloqueaba TODAS las salidas posteriores
    de ese símbolo: la siguiente posición ya no se podía cerrar por estrategia.
    """
    engine, _, recorder = _montar_con_duplicados(
        tmp_path, CompraYVende(), _escribir_csv_con_desplome(tmp_path)
    )

    engine.run()

    rechazos = [e for e in recorder.events if type(e).__name__ == "OrderRejected"]
    duplicados = [
        e for e in recorder.events
        if type(e).__name__ == "RiskBlocked" and e.rule == "duplicate_order"
    ]
    ventas = [
        e for e in recorder.events
        if type(e).__name__ == "OrderSubmitted" and e.side is Side.SELL
    ]

    assert len(rechazos) == 1
    assert rechazos[0].symbol == "TSLA"
    assert "sin tramo abierto" in rechazos[0].reason
    assert duplicados == []
    assert len(ventas) >= 2          # la del stop y, al menos, la de la posición siguiente
    assert ("TSLA", "SELL", "base") not in engine.risk.state.pending_order_keys


class CompraEnLaUltima(Strategy):
    """Estrategia de prueba: compra en su octava y última evaluación.

    Esa orden nace en la última vela y ya no queda vela siguiente en la que ejecutarse:
    ni Fill, ni rechazo, ni cancelación. Es el caso que solo cubre el paracaídas.
    """

    name = "compra_en_la_ultima"
    version = "0.0.1"

    def __init__(self):
        self.calls = 0
        self.last_signal_id = ""

    def required_indicators(self):
        return []

    def on_bar(self, ctx):
        self.calls += 1
        self.last_signal_id = f"sig{self.calls}"
        if self.calls != 8:
            self.record_evaluation(ctx.symbol, _idle_score(), self.last_signal_id)
            return []
        self.record_evaluation(ctx.symbol, _armed_score(), self.last_signal_id)
        return [
            Intent(self.last_signal_id, ctx.symbol, Side.BUY, Shares(10),
                   ctx.bar.close - 5, "prueba", "base")
        ]


def test_al_terminar_no_queda_ninguna_clave_colgada_de_una_orden_sin_resolver(tmp_path):
    """B1, paracaídas: pase lo que pase durante la sesión, el cierre de sesión libera las claves
    de las órdenes que sigan sin resolver. Sin esto, un runner que reutilice el mismo
    RiskState entre sesiones arrancaría el día siguiente con claves de ayer.
    """
    estrategia = CompraEnLaUltima()
    engine, _, _ = _montar_con_duplicados(
        tmp_path, estrategia, escribir_csv(tmp_path, 8)
    )

    engine.run()

    assert estrategia.calls == 8            # la orden nació en la última evaluación
    assert len(engine._orders) == 0
    assert engine.risk.state.pending_order_keys == set()


def test_cancelar_una_orden_publica_el_evento_y_libera_su_clave(tmp_path):
    """B1, tercera vía: la cancelación. El bróker no ve el bus, así que `cancel`
    devuelve el evento y el motor lo publica y lo resuelve, igual que hace con lo que
    devuelve `on_bar`.
    """
    engine, broker, recorder = _montar_con_duplicados(
        tmp_path, Inmovil(), escribir_csv(tmp_path, 3)
    )
    orden = Order("orden-a", "TSLA", Side.BUY, 10, 395.0, "sigA", "base")
    broker.submit(orden)
    engine._orders.track(orden)
    engine.risk.state.pending_order_keys.add(("TSLA", "BUY", "base"))

    engine.cancel_order("orden-a")
    engine.bus.drain()

    cancelaciones = [e for e in recorder.events if type(e).__name__ == "OrderCancelled"]
    assert len(cancelaciones) == 1
    assert cancelaciones[0].order_id == "orden-a"
    assert ("TSLA", "BUY", "base") not in engine.risk.state.pending_order_keys
    assert "orden-a" not in engine._orders


# --- B2: la watchlist es el conjunto OPERABLE; lo que se suscribe es la union de la
# watchlist con los simbolos explicitos de los indicadores requeridos. ---


def _escribir_csv_con_referencia(tmp_path, minutos: int = 6):
    """CSV con el símbolo operable (TSLA) y una referencia observada (ONEQ).

    tmp_path : Path — carpeta temporal de la prueba.
    minutos  : int  — velas por símbolo.
    return   : Path — ruta del CSV escrito.
    """
    filas = ["timestamp,symbol,open,high,low,close,volume"]
    for i in range(minutos):
        ts = dt.datetime(2026, 3, 10, 10, i, tzinfo=TZ).isoformat()
        p_tsla = 400.0 + i
        p_oneq = 60.0 + i * 0.5
        filas.append(f"{ts},TSLA,{p_tsla},{p_tsla + 1},{p_tsla - 1},{p_tsla + 0.5},1000")
        filas.append(f"{ts},ONEQ,{p_oneq},{p_oneq + 1},{p_oneq - 1},{p_oneq + 0.5},1000")
    ruta = tmp_path / "con_referencia.csv"
    ruta.write_text("\n".join(filas) + "\n", encoding="utf-8")
    return ruta


class MiraLaReferencia(Strategy):
    """Estrategia de prueba que pide un indicador de un símbolo que NO opera."""

    name = "mira_referencia"
    version = "0.0.1"

    def __init__(self):
        self.simbolos_evaluados = []
        self.valores_referencia = []
        self.last_signal_id = ""

    def required_indicators(self):
        return [
            IndicatorSpec("rsi", "1m", params={"period": 2}),
            IndicatorSpec("rsi", "1m", symbol="ONEQ", params={"period": 2}),
        ]

    def on_bar(self, ctx):
        self.simbolos_evaluados.append(ctx.symbol)
        self.valores_referencia.append(ctx.indicator("rsi", "1m", symbol="ONEQ"))
        self.record_evaluation(ctx.symbol, _idle_score(), self.last_signal_id)
        return []


def test_los_indicadores_de_una_referencia_no_operable_si_reciben_datos(tmp_path):
    """B2: el motor suscribía solo la watchlist, así que el feed filtraba los ticks de
    ONEQ y su indicador se quedaba sin valor para siempre. Con el perfil que se
    entrega eso hacía que `rsi_ndx` no cumpliera JAMÁS y armar fuera imposible, con
    independencia de las zonas de soporte.
    """
    estrategia = MiraLaReferencia()
    bus = EventBus()
    broker = SimulatedBroker(
        starting_cash=100_000.0,
        execution=ExecutionModel(slippage_pct=0.0, commission_per_share=0.0, min_commission=0.0),
    )
    engine = Engine(
        clock=SimulatedClock(dt.datetime(2026, 3, 10, 10, 0, tzinfo=TZ), "UTC"),
        bus=bus,
        feed=CsvReplayFeed(_escribir_csv_con_referencia(tmp_path, 6)),
        broker=broker,
        strategy=estrategia,
        risk=RiskManager(
            filters=[TradingWindowFilter()],
            sizer=PercentEquitySizer(pct=0.2),
            state=RiskState(starting_equity=100_000.0),
        ),
        schedule=HORARIO,
        watchlist=["TSLA"],
        indicator_specs=estrategia.required_indicators(),
        levels=NIVELES,
    )

    engine.run()

    # La referencia recibe datos: su indicador llega a tener valor.
    assert any(v is not None for v in estrategia.valores_referencia)


def test_la_referencia_observada_no_se_evalua_como_simbolo_operable(tmp_path):
    """La otra mitad de B2: meter ONEQ en la watchlist NO es la solución, porque
    entonces el motor lo consideraría operable y la estrategia podría abrir posición en
    el ETF de referencia. Suscrito sí; operable no.
    """
    estrategia = MiraLaReferencia()
    bus = EventBus()
    broker = SimulatedBroker(
        starting_cash=100_000.0,
        execution=ExecutionModel(slippage_pct=0.0, commission_per_share=0.0, min_commission=0.0),
    )
    engine = Engine(
        clock=SimulatedClock(dt.datetime(2026, 3, 10, 10, 0, tzinfo=TZ), "UTC"),
        bus=bus,
        feed=CsvReplayFeed(_escribir_csv_con_referencia(tmp_path, 6)),
        broker=broker,
        strategy=estrategia,
        risk=RiskManager(
            filters=[TradingWindowFilter()],
            sizer=PercentEquitySizer(pct=0.2),
            state=RiskState(starting_equity=100_000.0),
        ),
        schedule=HORARIO,
        watchlist=["TSLA"],
        indicator_specs=estrategia.required_indicators(),
        levels=NIVELES,
    )

    engine.run()

    assert set(estrategia.simbolos_evaluados) == {"TSLA"}
    assert estrategia.simbolos_evaluados  # y se evaluó de verdad, no cero veces


class SoloEvaluaTsla(Strategy):
    """Estrategia de prueba que registra evaluación de TSLA y de nadie más.

    Reproduce el caso que describe I1: una estrategia que retorna antes de rellenar la
    puntuación. Con una sola ranura compartida, el motor atribuía al símbolo en curso
    la puntuación del símbolo anterior.
    """

    name = "solo_tsla"
    version = "0.0.1"

    def required_indicators(self):
        return []

    def on_bar(self, ctx):
        if ctx.symbol != "TSLA":
            return []
        self.record_evaluation(ctx.symbol, _armed_score(), f"sig-{ctx.symbol}")
        return []


def test_el_motor_no_atribuye_a_un_simbolo_la_puntuacion_de_otro(tmp_path):
    """I1, lado del motor: la evaluación se lee POR SÍMBOLO. Un símbolo que la
    estrategia no evaluó no produce `SignalEvaluated` ni entra en los contadores de
    IDLE con datos ajenos.
    """
    bus = EventBus()
    recorder = Recorder()
    bus.subscribe(recorder)
    broker = SimulatedBroker(
        starting_cash=100_000.0,
        execution=ExecutionModel(slippage_pct=0.0, commission_per_share=0.0, min_commission=0.0),
    )
    engine = Engine(
        clock=SimulatedClock(dt.datetime(2026, 3, 10, 10, 0, tzinfo=TZ), "UTC"),
        bus=bus,
        feed=CsvReplayFeed(_escribir_csv_dos_simbolos(tmp_path, 5)),
        broker=broker,
        strategy=SoloEvaluaTsla(),
        risk=RiskManager(
            filters=[TradingWindowFilter()],
            sizer=PercentEquitySizer(pct=0.1),
            state=RiskState(starting_equity=100_000.0),
        ),
        schedule=HORARIO,
        watchlist=["TSLA", "NVDA"],
        indicator_specs=[],
        levels=NIVELES,
    )

    engine.run()

    evaluaciones = [e for e in recorder.events if type(e).__name__ == "SignalEvaluated"]
    assert evaluaciones                                    # de TSLA sí hay
    assert {e.symbol for e in evaluaciones} == {"TSLA"}
    assert all(e.signal_id == "sig-TSLA" for e in evaluaciones)
    assert "NVDA" not in engine._idle.symbols()


def test_una_posicion_que_no_se_pudo_cerrar_se_registra_como_error(tmp_path):
    """Barrera de posición huérfana: si tras el cierre de la sesión sigue quedando algo
    abierto, el único aviso era un `warnings.warn` del bróker, que puede perderse. Es
    el paracaídas de toda la familia "posición que no se puede cerrar", así que tiene
    que quedar en el log de sistema como ERROR.
    """
    engine, broker, recorder = _montar_con_duplicados(
        tmp_path, Inmovil(), escribir_csv(tmp_path, 3)
    )
    broker.submit(Order("o1", "TSLA", Side.BUY, 10, 390.0, "sig1", "base"))
    broker.on_bar(Bar("TSLA", "1m", dt.datetime(2026, 3, 10, 10, 1, tzinfo=TZ),
                      400.0, 401.0, 399.0, 400.0, 1_000))
    assert broker.positions()  # hay algo que cerrar
    # Un bróker que no consigue liquidar: es justo el caso que la barrera vigila.
    broker.close_all = lambda bars, reason: []

    engine._closer.finish()
    engine.bus.drain()

    errores = [
        e for e in recorder.events
        if type(e).__name__ == "SystemEvent" and e.level == "ERROR"
    ]
    assert len(errores) == 1
    assert "TSLA" in str(errores[0].detail)


# --- D4 y D5: la sesión se cierra pase lo que pase, y la orden se registra antes -----


class _FeedQueRevienta(CsvReplayFeed):
    """Feed que entrega dos ticks y luego se rompe, como un histórico corrupto."""

    def __init__(self, path):
        super().__init__(path)
        self.desconectado = False

    def stream(self):
        for indice, tick in enumerate(super().stream()):
            if indice >= 2:
                raise RuntimeError("el feed se rompió a media sesión")
            yield tick

    def disconnect(self):
        self.desconectado = True
        super().disconnect()


class _CompraSiempre(Strategy):
    """Estrategia de prueba que quiere comprar en cada vela."""

    name = "compra_siempre"
    version = "0.0.1"

    def required_indicators(self):
        return []

    def on_bar(self, ctx):
        self.record_evaluation(ctx.symbol, _armed_score(), "sig")
        return [Intent("sig", ctx.symbol, Side.BUY, Shares(10), ctx.bar.close - 5, "x", "base")]


class _BrokerQueRevientaAlEnviar(SimulatedBroker):
    """Bróker que rechaza de forma síncrona lanzando desde `submit`."""

    def submit(self, order):
        raise RuntimeError("el bróker rechazó la orden lanzando")


def test_una_excepcion_a_media_sesion_sigue_cerrando_la_sesion(tmp_path):
    """Un feed que revienta no puede dejar el feed conectado ni la sesión sin cerrar.

    Sin el `try/finally`, una excepción a media sesión se saltaba el cierre entero: no
    se liberaba ninguna clave anti-duplicado, no se emitía "parada" y no se desconectaba
    el feed. Es justo el escenario en el que un `RiskState` reutilizado entre sesiones
    arrastra claves de la anterior.
    """
    engine, _broker, recorder = montar(tmp_path, Inmovil())
    feed = _FeedQueRevienta(escribir_csv(tmp_path, 8))
    engine.feed = feed

    with pytest.raises(RuntimeError, match="se rompió"):
        engine.run()

    assert feed.desconectado is True
    mensajes = {getattr(e, "message", "") for e in recorder.events}
    assert "parada" in mensajes


def test_una_orden_rechazada_al_enviar_no_deja_su_clave_colgada(tmp_path):
    """Un bróker que lanza desde `submit()` no puede dejar la clave anti-duplicado viva.

    `submit` iba antes de `self._orders[order.order_id] = order`: la clave quedaba puesta
    por el gestor de riesgo y la orden sin registrar, invisible incluso para el
    paracaídas del cierre. La consecuencia era que ninguna orden equivalente volvía a
    pasar el filtro en toda la sesión.
    """
    engine, _broker, _recorder = montar(tmp_path, _CompraSiempre())
    engine.broker = _BrokerQueRevientaAlEnviar(
        starting_cash=100_000.0,
        execution=ExecutionModel(slippage_pct=0.0, commission_per_share=0.0, min_commission=0.0),
    )
    engine.risk.filters = [TradingWindowFilter(), DuplicateOrderFilter()]

    with pytest.raises(RuntimeError, match="rechazó"):
        engine.run()

    assert engine.risk.state.pending_order_keys == set()


# --- D11: la contabilidad de cierre vive en un solo sitio ---------------------------


@pytest.mark.parametrize("motivo", ["stop", "force_close", "end_of_data"])
def test_todo_cierre_actualiza_posiciones_y_resultado_del_dia(tmp_path, motivo):
    """Las tres vías de cierre actualizan igual el estado de riesgo.

    Es la prueba que impide que una vía de cierre nueva se olvide de descontar la
    posición o de acumular su resultado. Si falta lo primero, el símbolo queda bloqueado
    por `OnePositionPerSymbolFilter` sin tener posición; si falta lo segundo, el kill
    switch deja de ver esas pérdidas. Las tres se comprueban con el mismo aserto porque
    pasan las tres por `_apply_close`.
    """
    engine, broker, _recorder = montar(tmp_path, Inmovil())
    engine.risk.state.open_positions["TSLA"] = Position("TSLA", 10, 400.0, 395.0, "base", "s1")

    cierre = PositionClosed(
        ts=dt.datetime(2026, 3, 10, 10, 5, tzinfo=TZ),
        signal_id="s1",
        symbol="TSLA",
        shares=10,
        entry_price=400.0,
        exit_price=405.0,
        pnl=50.0,
        exit_reason=motivo,
    )
    engine._apply_close(cierre)

    assert "TSLA" not in engine.risk.state.open_positions
    assert engine.risk.state.daily_pnl == pytest.approx(50.0)


# --- D9: el motor cierra por reloj los agregadores de los demás símbolos -------------


def test_el_motor_produce_velas_de_un_simbolo_que_no_cotiza(tmp_path):
    """Un tick de TSLA hace avanzar también el agregador de ONEQ.

    Es el caso real por el que ONEQ se eligió como referencia del Nasdaq sabiendo que no
    cotiza en la mitad de los minutos (§7.2): si sus velas solo se cerraran cuando llega
    un tick suyo, su serie de 5' saldría comprimida y su RSI no sería el que se midió.
    """
    filas = ["timestamp,symbol,open,high,low,close,volume"]
    for i in range(8):
        ts = dt.datetime(2026, 3, 10, 10, i, tzinfo=TZ).isoformat()
        filas.append(f"{ts},TSLA,{400 + i},{401 + i},{399 + i},{400.5 + i},1000")
    # ONEQ solo cotiza en el minuto 0: los siete siguientes son huecos.
    ts0 = dt.datetime(2026, 3, 10, 10, 0, tzinfo=TZ).isoformat()
    filas.append(f"{ts0},ONEQ,50.0,50.2,49.8,50.0,300")
    ruta = tmp_path / "dos_simbolos.csv"
    ruta.write_text("\n".join(filas) + "\n", encoding="utf-8")

    class _PideONEQ(Inmovil):
        name = "pide_oneq"

        def required_indicators(self):
            return [IndicatorSpec("rsi", "1m", symbol="ONEQ", params={"period": 2})]

    engine, _broker, recorder = montar(tmp_path, _PideONEQ())
    engine.feed = CsvReplayFeed(ruta)

    engine.run()

    velas_oneq = [
        e.bar for e in recorder.events
        if type(e).__name__ == "BarClosed" and e.bar.symbol == "ONEQ"
    ]
    assert len(velas_oneq) >= 7, "ONEQ tiene que producir vela en los minutos sin operaciones"
    # Las de relleno arrastran el cierre anterior y llevan volumen cero.
    rellenos = [v for v in velas_oneq if v.volume == 0]
    assert rellenos
    assert all(v.close == 50.0 for v in rellenos)


# --- D7: el motor sella la cancelación con su propio reloj ---------------------------


def test_la_cancelacion_se_fecha_con_el_reloj_del_motor(tmp_path):
    """Antes se fechaba con el último `bar.ts` visto por el bróker, que arranca en 1970.

    Una cancelación anterior a la primera vela de la sesión escribía por tanto en
    `logs/1970-01-01/`, mezclada con las de todas las demás corridas.
    """
    engine, broker, recorder = montar(tmp_path, Inmovil())
    orden = Order("ord1", "TSLA", Side.BUY, 10, 395.0, "sig1", "base")
    broker.submit(orden)
    engine._orders.track(orden)

    engine.cancel_order("ord1")
    engine.bus.drain()

    cancelaciones = [e for e in recorder.events if type(e).__name__ == "OrderCancelled"]
    assert len(cancelaciones) == 1
    assert cancelaciones[0].ts == engine.clock.now()
    assert cancelaciones[0].ts.year == 2026


def test_la_cancelacion_sigue_liberando_su_clave_anti_duplicado(tmp_path):
    """El sellado no puede romper la resolución del ciclo de vida de la orden."""
    engine, broker, _recorder = montar(tmp_path, Inmovil())
    orden = Order("ord1", "TSLA", Side.BUY, 10, 395.0, "sig1", "base")
    broker.submit(orden)
    engine._orders.track(orden)
    engine.risk.state.pending_order_keys.add(("TSLA", "BUY", "base"))

    engine.cancel_order("ord1")

    assert engine.risk.state.pending_order_keys == set()


# --- D13: "parada" es el último evento también en el tiempo --------------------------


def test_parada_es_el_ultimo_evento_tambien_en_el_tiempo(tmp_path):
    """Ningún evento puede llevar un `ts` posterior al de "parada".

    La última vela se sella con su `period_end`, posterior al último tick, mientras que
    "parada" usa `clock.now()`, que es el último tick. Si la sesión acaba a las 23:59:30
    y la última vela de 1' se sella a las 00:00, esa vela cae en la carpeta del día
    siguiente y "parada" se queda en la de hoy. Los Planes 4 y 5 leen esas carpetas.
    """
    engine, _broker, recorder = montar(tmp_path, Inmovil())
    engine.run()

    parada = next(e for e in recorder.events if getattr(e, "message", "") == "parada")
    posteriores = [e for e in recorder.events if e.ts > parada.ts]

    assert posteriores == [], f"eventos con ts posterior a 'parada': {posteriores}"


def test_todos_los_registros_de_una_sesion_caen_en_la_misma_carpeta_de_dia(tmp_path):
    """La consecuencia práctica: una sesión, una carpeta."""
    engine, _broker, recorder = montar(tmp_path, Inmovil())
    engine.run()

    dias = {e.ts.date() for e in recorder.events}
    assert len(dias) == 1


# --- Los soportes llegan al contexto de la estrategia --------------------------------


class _ProveedorDePrueba:
    """Proveedor de zonas que cuenta cuántas veces se le pregunta."""

    def __init__(self, zonas):
        self.zonas = zonas
        self.llamadas = 0

    def zones_for(self, symbol, now):
        self.llamadas += 1
        return self.zonas

    def events(self):
        return []


class _MiraLasZonas(Strategy):
    """Estrategia de prueba que guarda las zonas que recibe en cada vela."""

    name = "mira_zonas"
    version = "0.0.1"

    def __init__(self):
        self.vistas = []

    def required_indicators(self):
        return []

    def on_bar(self, ctx):
        self.vistas.append(ctx.support_zones)
        self.record_evaluation(ctx.symbol, _idle_score(), "sig")
        return []


def test_el_contexto_recibe_las_zonas_del_proveedor(tmp_path):
    from bot.core.context import SupportZone

    zonas = (SupportZone(410.0, 412.5, 16.3, 14, ("1d", "1h")),)
    estrategia = _MiraLasZonas()
    engine, _broker, _rec = montar(tmp_path, estrategia)
    engine.supports = _ProveedorDePrueba(zonas)

    engine.run()

    assert estrategia.vistas
    assert all(v == zonas for v in estrategia.vistas)


def test_se_pregunta_al_proveedor_una_vez_por_vela_evaluada(tmp_path):
    """El proveedor es quien decide si recalcula o reutiliza; el motor solo pregunta. Si el
    motor cacheara por su cuenta, el refresco intradía del §6.2 no llegaría nunca."""
    estrategia = _MiraLasZonas()
    engine, _broker, _rec = montar(tmp_path, estrategia, minutos=8)
    proveedor = _ProveedorDePrueba(())
    engine.supports = proveedor

    engine.run()

    assert proveedor.llamadas == len(estrategia.vistas)


def test_sin_proveedor_el_contexto_llega_con_las_zonas_vacias(tmp_path):
    """Es lo correcto: no se falsean zonas para que el backtest "funcione". La condición
    `support` de Moris puntuará cero y su veto dejará toda evaluación en IDLE."""
    estrategia = _MiraLasZonas()
    engine, _broker, _rec = montar(tmp_path, estrategia)

    engine.run()

    assert all(v == () for v in estrategia.vistas)


def test_los_hechos_del_proveedor_se_publican(tmp_path):
    """El proveedor no conoce el bus: devuelve los hechos y el motor los publica, igual que
    el bróker."""
    from bot.core.events import SystemEvent

    class _ConEventos(_ProveedorDePrueba):
        def __init__(self):
            super().__init__(())
            self.entregado = False

        def events(self):
            if self.entregado:
                return []
            self.entregado = True
            return [SystemEvent(ts=dt.datetime(2026, 3, 10, 10, 1, tzinfo=TZ),
                                level="INFO", message="zonas de soporte recalculadas")]

    engine, _broker, recorder = montar(tmp_path, _MiraLasZonas())
    engine.supports = _ConEventos()

    engine.run()

    mensajes = [getattr(e, "message", "") for e in recorder.events]
    assert "zonas de soporte recalculadas" in mensajes


# --- §7.3: el motor revisa el trailing antes de evaluar la estrategia ----------------


def test_el_motor_sube_el_stop_cuando_el_trailing_lo_pide(tmp_path):
    """El motor es quien habla con el bróker; el gestor solo decide. Esa separación deja el
    camino listo para que en IBKR esto sea una modificación de una orden real."""
    from bot.risk.trailing import TrailingParams, TrailingStopManager

    class _CompraEnLaPrimera(Strategy):
        name = "compra_primera"
        version = "0.0.1"

        def __init__(self):
            self.veces = 0

        def required_indicators(self):
            return []

        def on_bar(self, ctx):
            self.veces += 1
            self.record_evaluation(ctx.symbol, _idle_score(), "sig")
            if self.veces != 1:
                return []
            return [
                Intent("sig", ctx.symbol, Side.BUY, Shares(10), ctx.bar.close - 4.0,
                       "prueba", "base")
            ]

    engine, broker, recorder = montar(tmp_path, _CompraEnLaPrimera(), minutos=8)
    engine.trailing = TrailingStopManager(
        TrailingParams(enabled=True, activate_at_r=1.0, distance_atr=1.0)
    )
    # El ATR de 5' no existe en esta prueba, así que se inyecta uno fijo por el conjunto de
    # indicadores: lo que se comprueba es el enganche, no el valor del ATR.
    engine._indicators.values_for = lambda symbol: {("atr", "5m", symbol): 1.0}

    engine.run()

    modificaciones = [e for e in recorder.events if type(e).__name__ == "StopModified"]
    assert modificaciones, "el trailing no llegó a mover ningún stop"
    assert all(m.new_stop > m.old_stop for m in modificaciones)
    assert all(m.ts.year == 2026 for m in modificaciones)


def test_sin_trailing_el_motor_no_toca_ningun_stop(tmp_path):
    engine, broker, recorder = montar(tmp_path, Inmovil())

    engine.run()

    assert [e for e in recorder.events if type(e).__name__ == "StopModified"] == []


# --- El latido: una vela cierra aunque no llegue ningún tick -------------------------


class _FeedConLatidos(CsvReplayFeed):
    """Feed que intercala latidos entre los ticks, como hace el de IBKR en vivo."""

    def stream(self):
        ultimo = None
        for tick in super().stream():
            yield tick
            ultimo = tick[1]
        # Cinco latidos de un minuto tras agotarse los datos: es lo que haría un feed en
        # vivo con el mercado abierto y ningún símbolo cotizando.
        for i in range(1, 6):
            yield ("", ultimo + dt.timedelta(minutes=i), None, 0)


def test_un_latido_cierra_las_velas_sin_meter_precio(tmp_path):
    """Un símbolo que no cotiza —ONEQ, el 46,6 % de los minutos, medido contra IBKR— no
    produce ticks. Sin latido sus agregadores no avanzan, sus indicadores se congelan toda la
    sesión y la condición `rsi_ndx` no puede cumplirse jamás."""
    engine, _broker, recorder = montar(tmp_path, Inmovil(), minutos=8)
    engine.feed = _FeedConLatidos(escribir_csv(tmp_path, 8))

    engine.run()

    velas = [e.bar for e in recorder.events if type(e).__name__ == "BarClosed"]
    rellenos = [v for v in velas if v.volume == 0]
    assert rellenos, "los latidos tienen que producir velas de relleno"


def test_un_latido_no_altera_los_precios_ni_el_volumen(tmp_path):
    """Es la mitad que hace que el latido sea seguro: dice qué hora es y nada más."""
    engine, _broker, recorder = montar(tmp_path, Inmovil(), minutos=8)
    engine.feed = _FeedConLatidos(escribir_csv(tmp_path, 8))

    engine.run()

    rellenos = [
        e.bar for e in recorder.events
        if type(e).__name__ == "BarClosed" and e.bar.volume == 0
    ]
    for vela in rellenos:
        assert vela.open == vela.high == vela.low == vela.close


def test_el_feed_de_fichero_no_produce_latidos(tmp_path):
    """El backtest se agota y el cierre de sesión vacía lo que quede: no los necesita, y
    meterlos cambiaría las cifras doradas."""
    feed = CsvReplayFeed(escribir_csv(tmp_path, 8))
    feed.connect()
    feed.subscribe(["TSLA"])

    assert all(precio is not None for _s, _ts, precio, _v in feed.stream())


# --- Los hechos del feed llegan al registro -----------------------------------------


class _FeedQueHabla(CsvReplayFeed):
    """Feed que acumula hechos, como el de IBKR."""

    def __init__(self, path):
        super().__init__(path)
        self._pendientes = [
            SystemEvent(
                ts=dt.datetime(2026, 3, 10, 10, 0, tzinfo=TZ),
                level="WARNING",
                message="datos de mercado en diferido",
            )
        ]

    def events(self):
        pendientes, self._pendientes = self._pendientes, []
        return pendientes


def test_los_hechos_del_feed_se_publican(tmp_path):
    """Los del feed de IBKR son de los más importantes del registro: que la conexión se cayó,
    que un símbolo no tiene suscripción, y sobre todo que los datos son DIFERIDOS. Ese último
    es el que dice que las cifras de la sesión no son un resultado de trading."""
    engine, _broker, recorder = montar(tmp_path, Inmovil())
    engine.feed = _FeedQueHabla(escribir_csv(tmp_path, 8))

    engine.run()

    mensajes = [getattr(e, "message", "") for e in recorder.events]
    assert "datos de mercado en diferido" in mensajes


def test_un_feed_que_no_habla_no_rompe_nada(tmp_path):
    """El de fichero no produce hechos y no tiene por qué implementar el método."""
    engine, _broker, _rec = montar(tmp_path, Inmovil())

    engine.run()   # no debe reventar


# --- El bus tiene que estar repartiendo, no solo encolando ---------------------------


def test_el_motor_arranca_el_hilo_del_bus(tmp_path):
    """`EventBus.start()` no se llamaba en ninguna parte: el hilo trabajador nunca corría.

    En backtest se disimulaba porque el cierre de sesión hace un `drain()` síncrono al
    terminar. En vivo significaba que no se escribía una sola línea en toda la sesión —medido
    contra TWS: doce minutos sin un registro— y que la cola crecía hasta que la contrapresión
    empezaba a descartar velas. El §2.1 del diseño promete "un hilo trabajador aparte" y "un
    hipo de disco de 200 ms no para el motor": sin arrancarlo, esa promesa era falsa.
    """
    engine, _broker, _rec = montar(tmp_path, Inmovil())

    engine.run()

    assert engine.bus._worker is not None, "el hilo del bus no llegó a arrancar"


def test_los_eventos_llegan_a_los_sinks_antes_de_terminar(tmp_path):
    """La consecuencia observable: un sink recibe eventos MIENTRAS la sesión corre, no solo
    al cerrarla."""

    import threading

    class _SinkQueBloqueaHastaRecibir(EventSink):
        def __init__(self):
            self.recibidos = threading.Event()

        def handle(self, event):
            self.recibidos.set()

        def close(self):
            pass

    engine, _broker, _rec = montar(tmp_path, Inmovil(), minutos=30)
    espia = _SinkQueBloqueaHastaRecibir()
    engine.bus.subscribe(espia)

    engine.run()

    assert espia.recibidos.is_set()


def test_parada_cierra_la_sesion_tambien_con_reloj_real(tmp_path):
    """El arreglo anterior adelantaba `SimulatedClock` y en vivo no hacía nada.

    `RealClock` no se puede adelantar, y una vela de 5' en curso al parar se sella hasta cinco
    minutos por delante del reloj. Medido en el ensayo contra TWS del 2026-08-26: "parada" a
    las 11:21:26 y las últimas velas a las 11:22 y las 11:25. Tomar el máximo no toca el reloj
    de nadie y deja el invariante cierto con los dos.
    """
    from bot.core.clock import RealClock

    engine, _broker, recorder = montar(tmp_path, Inmovil())
    engine.clock = RealClock("UTC")

    engine.run()

    parada = next(e for e in recorder.events if getattr(e, "message", "") == "parada")
    posteriores = [e for e in recorder.events if e.ts > parada.ts]
    assert posteriores == [], f"eventos con ts posterior a 'parada': {posteriores}"


# --- Cuanto recorrido tenia por delante la operacion, en el instante de entrar ---------


class _ResistenciasFalsas:
    """Doble del proveedor de zonas, mirando hacia arriba."""

    def __init__(self, suelo):
        self.suelo = suelo

    def zones_for(self, symbol, now):
        from bot.support.zones import SupportZone
        return (SupportZone(low=self.suelo, high=self.suelo + 1.0, score=9.0,
                            touches=3, timeframes=("1h",)),)

    def events(self):
        return []


def test_la_apertura_apunta_cuanto_R_habia_hasta_el_VWAP(tmp_path):
    """MEDIDO el 2026-09-02: a las operaciones que llegaron al objetivo el mercado solo les
    llego a ofrecer 0,717R y hacian falta 0,74R, asi que NINGUNA mejora de la salida arregla
    la estrategia. Lo que falta es poder decidir al ENTRAR si la operacion merece la pena, y
    para eso hace falta un objetivo legible en ese instante. Esto solo lo APUNTA: no cambia
    ni una decision."""
    engine, _broker, recorder = montar(tmp_path, CompraEnLaTercera())

    engine.run()

    aperturas = [e for e in recorder.events if type(e).__name__ == "PositionOpened"]
    assert aperturas, "la estrategia no llego a abrir"
    assert aperturas[0].r_to_vwap is not None


def test_la_apertura_apunta_cuanto_R_habia_hasta_la_resistencia(tmp_path):
    """El techo estructural es el otro objetivo legible al entrar, y a diferencia del VWAP
    no se mueve durante la sesion."""
    engine, _broker, recorder = montar(tmp_path, CompraEnLaTercera())
    apertura = None
    engine.resistances = _ResistenciasFalsas(suelo=1000.0)

    engine.run()

    apertura = [e for e in recorder.events if type(e).__name__ == "PositionOpened"][0]
    riesgo = apertura.entry_price - apertura.stop_price
    assert apertura.r_to_resistance == pytest.approx((1000.0 - apertura.entry_price) / riesgo)


def test_sin_resistencias_declaradas_no_se_inventa_un_numero(tmp_path):
    """Un cero se leeria como "no habia recorrido", que es una conclusion, no un dato que
    falta."""
    engine, _broker, recorder = montar(tmp_path, CompraEnLaTercera())

    engine.run()

    apertura = [e for e in recorder.events if type(e).__name__ == "PositionOpened"][0]
    assert apertura.r_to_resistance is None
