"""Contrato de los observadores que cuelgan del bus."""

from __future__ import annotations

from abc import ABC, abstractmethod

from bot.core.events import Event


class EventSink(ABC):
    """Consumidor de eventos.

    Un sink escribe, pinta o notifica. **Nunca** decide ni opera: no tiene referencia
    al bróker, así que no puede aunque quiera.
    """

    @abstractmethod
    def handle(self, event: Event) -> None:
        """Procesa un evento.

        Puede lanzar: el bus captura la excepción y sigue con los demás sinks.

        event  : Event — el evento a procesar.
        return : None
        """

    @abstractmethod
    def close(self) -> None:
        """Libera recursos: cierra ficheros, conexiones, etc.

        return : None
        """
