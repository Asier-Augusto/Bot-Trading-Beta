"""Bus de observación.

Aísla el disco y la red del camino de decisión. `emit` encola y vuelve en cientos de
nanosegundos; un hilo aparte reparte a los sinks. Un hipo de disco de 200 ms no para
el motor.
"""

from __future__ import annotations

import collections
import threading
from dataclasses import dataclass, field

from bot.contracts.sink import EventSink
from bot.core.events import Event, Priority


@dataclass
class BusStats:
    """Contadores del bus, para que un registro incompleto sea siempre visible.

    emitted         : int  — eventos aceptados por `emit`.
    delivered       : int  — entregas a sinks (un evento a tres sinks cuenta tres).
    dropped_by_kind : dict — descartes por contrapresión, por tipo de evento.
    sink_errors     : int  — excepciones capturadas de sinks.
    overflowed      : int  — veces que la cola creció por encima del máximo para no
                             perder un evento crítico.
    stop_timed_out  : bool — `stop()` agotó su timeout con el trabajador aún vivo
                             (un sink colgado dentro de `handle()`). El trabajador
                             sigue corriendo en segundo plano; no se creó un segundo
                             despachador para no competir por `stats` sin cerrojo.
    """

    emitted: int = 0
    delivered: int = 0
    dropped_by_kind: dict[str, int] = field(default_factory=dict)
    sink_errors: int = 0
    overflowed: int = 0
    stop_timed_out: bool = False


class EventBus:
    """Cola acotada en memoria con reparto a N sinks.

    La política de contrapresión es explícita: cuando la cola está llena se desaloja
    el evento descartable más antiguo. Si no hay ninguno descartable y el entrante es
    crítico, se acepta igualmente y se cuenta como desbordamiento — **jamás** se pierde
    una orden, una ejecución ni un evento de riesgo.
    """

    def __init__(self, maxsize: int = 10_000) -> None:
        """Crea el bus.

        maxsize : int — capacidad de la cola antes de aplicar contrapresión.
        """
        self._maxsize = maxsize
        self._queue: collections.deque[Event] = collections.deque()
        self._sinks: list[EventSink] = []
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._worker: threading.Thread | None = None
        self._running = False
        #: True mientras el trabajador está DENTRO de un `handle()`. Sin esto, `drain()` no
        #: puede distinguir "no queda nada" de "queda el que se está entregando ahora mismo".
        self._dispatching = False
        self.stats = BusStats()

    def subscribe(self, sink: EventSink) -> None:
        """Añade un observador.

        sink   : EventSink — el observador a suscribir.
        return : None
        """
        with self._lock:
            self._sinks.append(sink)

    def emit(self, event: Event) -> None:
        """Encola un evento. Nunca bloquea y nunca lanza.

        event  : Event — el hecho consumado a repartir.
        return : None
        """
        with self._not_empty:
            if len(self._queue) >= self._maxsize:
                self._make_room(event)
            self._queue.append(event)
            self.stats.emitted += 1
            self._not_empty.notify()

    def _make_room(self, incoming: Event) -> None:
        """Aplica la política de contrapresión. Se llama con el cerrojo tomado.

        incoming : Event — el evento que quiere entrar.
        return   : None
        """
        for index, queued in enumerate(self._queue):
            if queued.priority is Priority.DROPPABLE:
                del self._queue[index]
                kind = queued.kind
                self.stats.dropped_by_kind[kind] = self.stats.dropped_by_kind.get(kind, 0) + 1
                return

        if incoming.priority is Priority.DROPPABLE:
            # Nada que desalojar y el entrante es prescindible: se descarta él.
            kind = incoming.kind
            self.stats.dropped_by_kind[kind] = self.stats.dropped_by_kind.get(kind, 0) + 1
            return

        # Cola llena de eventos que no se pueden perder. Se acepta y se hace visible.
        self.stats.overflowed += 1

    def _dispatch(self, event: Event) -> None:
        """Entrega un evento a todos los sinks, aislando sus fallos.

        event  : Event — evento a repartir.
        return : None
        """
        with self._lock:
            sinks = list(self._sinks)
        for sink in sinks:
            try:
                sink.handle(event)
                self.stats.delivered += 1
            except Exception:  # noqa: BLE001 — un sink jamás tumba el camino de decisión
                self.stats.sink_errors += 1

    def drain(self, timeout: float = 5.0) -> None:
        """Deja la cola vacía antes de volver.

        Hace dos cosas distintas según haya trabajador o no, y la diferencia importa:

        - **Sin trabajador** (el backtest y las pruebas), reparte aquí mismo: es el único
          repartidor que hay.
        - **Con el trabajador vivo**, ESPERA a que él termine. Repartir aquí sería meter un
          segundo repartidor en paralelo, y entonces dos hilos entran a la vez en el mismo
          sink. MEDIDO el 2026-08-27 en el barrido: `idle.jsonl` de una sesión salió con una
          línea partida por la mitad, porque dos hilos escribían en el mismo fichero. El
          invariante es que solo reparte un hilo; un sink no tiene por qué ser seguro entre
          hilos para poder escribir en un fichero.

        timeout : float — segundos máximos de espera al trabajador. Agotarlo no lanza: deja
                  eventos sin entregar, que es mejor que colgar el cierre de sesión.
        return  : None
        """
        if self._worker is None:
            while True:
                with self._lock:
                    if not self._queue:
                        return
                    event = self._queue.popleft()
                self._dispatch(event)

        restante = timeout
        with self._not_empty:
            while (self._queue or self._dispatching) and restante > 0:
                paso = min(0.05, restante)
                self._not_empty.wait(timeout=paso)
                restante -= paso

    def _run(self) -> None:
        """Bucle del hilo trabajador.

        return : None
        """
        while True:
            with self._not_empty:
                while self._running and not self._queue:
                    self._not_empty.wait(timeout=0.1)
                if not self._queue:
                    if not self._running:
                        return
                    continue
                event = self._queue.popleft()
                self._dispatching = True
            try:
                self._dispatch(event)
            finally:
                with self._not_empty:
                    self._dispatching = False
                    self._not_empty.notify_all()

    def start(self) -> None:
        """Arranca el hilo trabajador que reparte en segundo plano.

        return : None
        """
        if self._worker is not None:
            return
        self._running = True
        self._worker = threading.Thread(target=self._run, name="event-bus", daemon=True)
        self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Vacía lo pendiente, para el hilo y cierra los sinks.

        Si el trabajador sigue vivo tras agotar `timeout` (un sink colgado dentro de
        `handle()`), no se le da por muerto: crear un segundo despachador en paralelo
        competiría sin cerrojo por `stats.delivered` y `stats.sink_errors`, que es
        justo la carrera que este bus existe para evitar. En ese caso se marca
        `stats.stop_timed_out` y se sale sin drenar ni cerrar los sinks.

        timeout : float — segundos máximos de espera al hilo.
        return  : None
        """
        with self._not_empty:
            self._running = False
            self._not_empty.notify_all()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=timeout)
            if worker.is_alive():
                # Un sink sigue colgado dentro de handle(). Crear un segundo
                # despachador en paralelo es peor que dejar eventos pendientes:
                # se hace visible y se sale.
                self.stats.stop_timed_out = True
                return
            self._worker = None
        self.drain()
        with self._lock:
            sinks = list(self._sinks)
        for sink in sinks:
            try:
                sink.close()
            except Exception:  # noqa: BLE001
                self.stats.sink_errors += 1
