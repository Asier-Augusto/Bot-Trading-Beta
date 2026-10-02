"""Pruebas del bus: reparto, aislamiento de fallos y contrapresión."""

import datetime as dt
import threading

from bot.contracts.sink import EventSink
from bot.core.bus import EventBus
from bot.core.events import BarClosed, Fill, SystemEvent
from bot.core.types import Bar, Side

TS = dt.datetime(2026, 3, 10, 14, 32, tzinfo=dt.timezone.utc)
BAR = Bar("TSLA", "1m", TS, 410.0, 413.0, 409.0, 412.5, 1_000)


class Recorder(EventSink):
    """Sink de prueba que guarda todo lo que recibe."""

    def __init__(self) -> None:
        self.received: list = []

    def handle(self, event) -> None:
        self.received.append(event)

    def close(self) -> None:
        pass


class Exploding(EventSink):
    """Sink de prueba que siempre falla."""

    def __init__(self) -> None:
        self.calls = 0

    def handle(self, event) -> None:
        self.calls += 1
        raise RuntimeError("disco lleno")

    def close(self) -> None:
        pass


def _fill(n: int) -> Fill:
    return Fill(ts=TS, order_id=f"o{n}", symbol="TSLA", side=Side.BUY, shares=1, price=1.0)


def test_reparte_a_todos_los_sinks():
    bus = EventBus()
    a, b = Recorder(), Recorder()
    bus.subscribe(a)
    bus.subscribe(b)

    bus.emit(BarClosed(ts=TS, bar=BAR))
    bus.drain()

    assert len(a.received) == 1
    assert len(b.received) == 1


def test_un_sink_que_falla_no_detiene_a_los_demas():
    bus = EventBus()
    malo, bueno = Exploding(), Recorder()
    bus.subscribe(malo)
    bus.subscribe(bueno)

    bus.emit(BarClosed(ts=TS, bar=BAR))
    bus.drain()

    assert malo.calls == 1
    assert len(bueno.received) == 1
    assert bus.stats.sink_errors == 1


def test_emit_nunca_lanza_aunque_el_sink_falle():
    bus = EventBus()
    bus.subscribe(Exploding())

    bus.emit(BarClosed(ts=TS, bar=BAR))  # no debe lanzar
    bus.drain()


def test_la_contrapresion_descarta_velas_antes_que_ejecuciones():
    bus = EventBus(maxsize=3)
    # Se llena con eventos descartables sin drenar.
    for _ in range(3):
        bus.emit(BarClosed(ts=TS, bar=BAR))

    # Un evento crítico entra desalojando a un descartable.
    bus.emit(_fill(1))

    recorder = Recorder()
    bus.subscribe(recorder)
    bus.drain()

    tipos = [type(e).__name__ for e in recorder.received]
    assert "Fill" in tipos
    assert bus.stats.dropped_by_kind["BarClosed"] == 1


def test_nunca_se_descarta_un_evento_critico():
    bus = EventBus(maxsize=2)
    for n in range(5):
        bus.emit(_fill(n))

    recorder = Recorder()
    bus.subscribe(recorder)
    bus.drain()

    assert len(recorder.received) == 5
    assert bus.stats.dropped_by_kind.get("Fill", 0) == 0
    assert bus.stats.overflowed >= 1


def test_el_hilo_trabajador_entrega_y_para_limpiamente():
    bus = EventBus()
    recorder = Recorder()
    bus.subscribe(recorder)
    bus.start()

    bus.emit(SystemEvent(ts=TS, level="INFO", message="arranque"))
    bus.stop(timeout=5.0)

    assert len(recorder.received) == 1


def test_stop_no_finge_muerto_al_trabajador_si_un_sink_se_cuelga():
    # El sink entra en handle() y se queda esperando a que el test le libere.
    liberar = threading.Event()

    class Colgado(EventSink):
        """Sink de prueba que se cuelga dentro de handle() hasta que se le libera."""

        def __init__(self) -> None:
            self.entro = threading.Event()

        def handle(self, event) -> None:
            self.entro.set()
            liberar.wait()

        def close(self) -> None:
            pass

    bus = EventBus()
    colgado = Colgado()
    bus.subscribe(colgado)
    bus.start()

    bus.emit(SystemEvent(ts=TS, level="INFO", message="x"))
    colgado.entro.wait(timeout=2.0)  # confirma que el trabajador ya entró en handle()

    try:
        bus.stop(timeout=0.2)  # debe volver enseguida, sin fingir que el hilo murió

        assert bus.stats.stop_timed_out is True
    finally:
        liberar.set()  # libera al trabajador huérfano para no dejarlo colgado


def test_suscribir_durante_el_cierre_no_revienta():
    """El bucle de cierre de stop() debe iterar sobre una instantánea, no sobre la
    lista viva: el sitio donde vivía I1 es ese bucle, no el reparto de _dispatch()
    (que ya iteraba sobre list(self._sinks) antes del arreglo).

    Nota: mutar una lista con append() mientras un `for` la recorre NO lanza
    RuntimeError en CPython (ese error es propio de dict/set); el for simplemente
    visita también el elemento añadido. El síntoma real y verificable de I1 es que,
    sin instantánea, ese sink recién suscrito durante el cierre acaba viéndose
    `close()`-ado en el mismo `stop()` que lo trajo — antes de que nadie lo haya
    usado nunca. Con la instantánea, queda fuera del recorrido en curso.
    """
    bus = EventBus()

    class CierraSePonEnBandera(EventSink):
        """Sink de prueba que solo registra si se le ha llamado close()."""

        def __init__(self) -> None:
            self.cerrado = False

        def handle(self, event) -> None:
            pass

        def close(self) -> None:
            self.cerrado = True

    nuevo = CierraSePonEnBandera()

    class SuscribeAlCerrar(EventSink):
        """Sink de prueba cuyo close() suscribe a `nuevo` mientras stop() recorre
        la lista, para mutarla realmente DURANTE la iteración de cierre."""

        def handle(self, event) -> None:
            pass

        def close(self) -> None:
            bus.subscribe(nuevo)

    # Se suscribe primero para que su close() se ejecute mientras el recorrido de
    # stop() sigue en curso (con al menos un sink más detrás en la lista).
    bus.subscribe(SuscribeAlCerrar())
    bus.subscribe(Recorder())
    bus.start()

    bus.emit(SystemEvent(ts=TS, level="INFO", message="x"))
    bus.stop(timeout=5.0)

    # Sin instantánea, el recorrido de cierre crece sobre la marcha y `nuevo` acaba
    # viéndose close()-ado en este mismo stop(); con instantánea, queda fuera.
    assert nuevo.cerrado is False


# --- drain() con el trabajador vivo --------------------------------------------------


class _SinkLento:
    """Sink que anota en qué hilo se le llamó y tarda un poco dentro de handle()."""

    def __init__(self, retardo=0.002):
        self.hilos = set()
        self.eventos = []
        self.solapes = 0
        self._dentro = 0
        self._retardo = retardo
        self._lock = threading.Lock()

    def handle(self, event):
        import time

        with self._lock:
            self._dentro += 1
            if self._dentro > 1:
                self.solapes += 1
        self.hilos.add(threading.current_thread().name)
        time.sleep(self._retardo)
        self.eventos.append(event)
        with self._lock:
            self._dentro -= 1

    def close(self):
        pass


def test_drenar_con_el_trabajador_vivo_no_mete_un_segundo_repartidor():
    """MEDIDO el 2026-08-27 en el barrido: `idle.jsonl` de una sesión salió con una línea
    partida por la mitad. Con el trabajador vivo, `drain()` repartía en el hilo que llama,
    así que dos hilos entraban a la vez en el MISMO sink y se pisaban al escribir.

    El invariante es que solo reparte un hilo. Un sink no tiene por qué ser seguro entre
    hilos para poder escribir en un fichero.
    """
    bus = EventBus()
    sink = _SinkLento()
    bus.subscribe(sink)
    bus.start()
    try:
        for _ in range(40):
            bus.emit(BarClosed(ts=TS, bar=BAR))
        bus.drain()
    finally:
        bus.stop()

    assert sink.solapes == 0
    assert len(sink.hilos) == 1


def test_drenar_con_el_trabajador_vivo_espera_a_que_no_quede_nada():
    """Si volviera antes de tiempo, el cierre de sesión cerraría las sombras al precio de una
    vela que el trabajador todavía no ha entregado."""
    bus = EventBus()
    sink = _SinkLento()
    bus.subscribe(sink)
    bus.start()
    try:
        for _ in range(20):
            bus.emit(BarClosed(ts=TS, bar=BAR))
        bus.drain()
        entregados = len(sink.eventos)
    finally:
        bus.stop()

    assert entregados == 20


def test_drenar_sin_trabajador_sigue_repartiendo_en_el_acto():
    """Es como corre el backtest: sin hilo, `drain()` es el único repartidor que hay."""
    bus = EventBus()
    sink = _SinkLento(retardo=0)
    bus.subscribe(sink)

    bus.emit(BarClosed(ts=TS, bar=BAR))
    bus.drain()

    assert len(sink.eventos) == 1
