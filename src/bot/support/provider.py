"""Quién responde a la pregunta "¿dónde están las zonas de soporte de este símbolo?".

El motor no detecta zonas: se las pide a un proveedor que recibe por inyección. Así la
detección se puede cambiar entera —otro algoritmo, otra fuente de datos, un fichero
precalculado— sin tocar el motor, y el motor se puede probar sin ninguna detección.

**En esta versión pública solo está el contrato.** La implementación real (recálculo al
cierre de vela, caché por símbolo y marco, fusión entre marcos temporales) no se publica.
`NullSupportProvider` es la implementación trivial que permite arrancar el motor sin
zonas: devuelve siempre vacío, con lo que cualquier condición que dependa del soporte
simplemente no se cumple.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod

from bot.core.context import SupportZone
from bot.core.events import Event


class SupportProvider(ABC):
    """Contrato de un proveedor de zonas de soporte.

    Dos obligaciones, y ninguna más: saber decir qué zonas hay para un símbolo en un
    instante dado, y poder entregar los eventos que haya generado al hacerlo (un recálculo,
    un aviso de histórico insuficiente) para que el motor los emita al bus.
    """

    @abstractmethod
    def zones_for(self, symbol: str, now: dt.datetime) -> tuple[SupportZone, ...]:
        """Zonas vigentes para un símbolo en un instante.

        symbol : str      — ticker consultado.
        now    : datetime — instante de la consulta, SIEMPRE del reloj inyectado: un
                            proveedor que mire la hora del sistema rompe el backtest.
        return : tuple[SupportZone, ...] — ordenadas por suelo ascendente, para que
                 preguntar "¿está el precio en soporte?" sea una búsqueda binaria.
        """

    @abstractmethod
    def events(self) -> list[Event]:
        """Eventos acumulados desde la última llamada, y los vacía.

        return : list[Event] — hechos que el motor emitirá al bus; lista vacía si no hay.
        """


class NullSupportProvider(SupportProvider):
    """Proveedor que no detecta nada. Útil para arrancar el motor sin zonas y en pruebas."""

    def zones_for(self, symbol: str, now: dt.datetime) -> tuple[SupportZone, ...]:
        """Siempre vacío.

        symbol : str      — ignorado.
        now    : datetime — ignorado.
        return : tuple[SupportZone, ...] — siempre la tupla vacía.
        """
        return ()

    def events(self) -> list[Event]:
        """Nunca produce eventos.

        return : list[Event] — siempre la lista vacía.
        """
        return []
