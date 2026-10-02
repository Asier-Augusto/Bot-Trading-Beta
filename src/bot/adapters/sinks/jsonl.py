"""Registro en JSONL, un fichero por tema y por día.

JSONL y no CSV como formato base por dos motivos. Es a prueba de caídas: solo se añade
al final, nunca se reescribe, así que si el proceso muere a media escritura lo peor
posible es una última línea incompleta, y se detecta al leerla. Y admite campos nuevos
sin romper lo antiguo, mientras que un CSV de columnas fijas obligaría a decidir hoy
todo lo que se querrá guardar dentro de dos años.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, TextIO

from bot.contracts.sink import EventSink
from bot.core.events import Event
from bot.core.registry import register_sink
from bot.core.scoring import SignalLevel

#: A qué fichero temático va cada tipo de evento.
#:
#: SOLO eventos que el código emite. La versión anterior enrutaba cinco tipos que no
#: existían en ninguna parte (`KillSwitchTripped`, `OrderAccepted`, `PositionScaled`,
#: `IndicatorSnapshot`, `BrokerMessage`, `ShadowTrade*`) y se leía como una cobertura que
#: no estaba: quien buscara `shadow.jsonl` no encontraría el fichero y no sabría si es que
#: no hubo operaciones en la sombra o es que nadie las escribe. Una prueba fija el
#: invariante: toda clave de aquí tiene que ser una clase real de `bot.core.events`.
TOPIC_BY_KIND = {
    "SignalEvaluated": "signals",
    "IdleSummary": "idle",
    "IntentRaised": "signals",
    "TriggerArmed": "signals",
    "ArmExpired": "signals",
    "RiskBlocked": "risk",
    "SizeComputed": "risk",
    "OrderSubmitted": "orders",
    "OrderRejected": "orders",
    "OrderCancelled": "orders",
    "StopModified": "orders",
    "Fill": "orders",
    "PositionOpened": "trades",
    "PositionClosed": "trades",
    "ShadowTradeOpened": "shadow",
    "ShadowTradeClosed": "shadow",
    "BarClosed": "market",
    "SystemEvent": "system",
}


def read_jsonl(path: Path) -> Iterator[dict]:
    """Lee un fichero JSONL tolerando una última línea truncada.

    Una línea incompleta al final es el único daño que puede causar una caída a media
    escritura. Se descarta en silencio; cualquier otra línea corrupta se propaga,
    porque eso sí indicaría un problema real.

    path   : Path — fichero a leer.
    return : Iterator[dict] — un diccionario por registro completo.
    """
    with path.open(encoding="utf-8") as handle:
        lines = handle.readlines()

    for index, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                return  # última línea truncada: caída a media escritura
            raise


@register_sink("jsonl")
class JsonlSink(EventSink):
    """Escribe cada evento como una línea JSON en el fichero de su tema."""

    def __init__(self, root: Path | str = "logs", min_signal_level: str = "NEAR_MISS") -> None:
        """Crea el sink.

        root             : Path | str — carpeta raíz de los logs.
        min_signal_level : str        — nivel mínimo para registrar `SignalEvaluated`.
                                        Por debajo, la evaluación solo se cuenta en los
                                        resúmenes agregados de `IdleSummary`.
        """
        self.root = Path(root)
        self.min_signal_level = SignalLevel[min_signal_level]
        self._handles: dict[Path, TextIO] = {}

    def _handle_for(self, day: str, topic: str) -> TextIO:
        """Devuelve el descriptor abierto del fichero de un tema y un día.

        day    : str — fecha en formato AAAA-MM-DD.
        topic  : str — tema, p. ej. "signals".
        return : TextIO — descriptor en modo añadir.
        """
        path = self.root / day / f"{topic}.jsonl"
        handle = self._handles.get(path)
        if handle is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a", encoding="utf-8")
            self._handles[path] = handle
        return handle

    def _should_write(self, event: Event) -> bool:
        """Decide si un evento supera el filtro de nivel.

        El filtro solo se aplica a `SignalEvaluated`: los demás eventos se escriben
        siempre, porque su volumen es acotado y su valor no depende de un umbral.

        event  : Event — evento a evaluar.
        return : bool
        """
        if event.kind != "SignalEvaluated":
            return True
        return SignalLevel[event.level] >= self.min_signal_level

    def handle(self, event: Event) -> None:
        """Escribe el evento en el fichero de su tema y lo VACÍA al disco.

        El vaciado inmediato no es paranoia. Sin él, Python guarda las líneas en un búfer de
        varios kilobytes y el fichero no aparece hasta que algo lo cierra. En backtest da
        igual —dura milisegundos—, pero en vivo tiene dos consecuencias graves:

        - **El registro es invisible mientras la sesión corre.** Medido: un ensayo de doce
          minutos contra TWS no escribió una sola línea; todo seguía en el búfer. No se puede
          vigilar una sesión en vivo cuyo registro no existe hasta que termina.
        - **Una caída se lleva el búfer entero.** La promesa del módulo —"si el proceso muere
          a media escritura, lo peor posible es una última línea incompleta"— es falsa
          mientras haya búfer: lo que se pierde son los últimos kilobytes, y con eventos
          críticos esos kilobytes son las últimas órdenes y ejecuciones.

        El coste es una llamada al sistema por evento. El bus ya aísla el disco del camino de
        decisión con un hilo aparte, que es exactamente para lo que existe.

        event  : Event — evento recibido del bus.
        return : None
        """
        if not self._should_write(event):
            return
        topic = TOPIC_BY_KIND.get(event.kind, "system")
        day = event.ts.date().isoformat()
        handle = self._handle_for(day, topic)
        handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        handle.flush()

    def close(self) -> None:
        """Cierra todos los ficheros abiertos.

        return : None
        """
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()
