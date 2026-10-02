"""Contrato del dimensionador de posiciones."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from bot.core.types import Intent

if TYPE_CHECKING:
    from bot.core.context import MarketContext


class PositionSizer(ABC):
    """Convierte una intención en un número concreto de acciones."""

    @abstractmethod
    def size(self, intent: Intent, ctx: "MarketContext", entry_price: float) -> int:
        """Calcula cuántas acciones corresponden a una intención.

        intent      : Intent        — la intención, con su directiva de tamaño opcional.
        ctx         : MarketContext — contexto, para leer la foto de cuenta.
        entry_price : float         — precio de referencia de la entrada.
        return      : int           — número de acciones, nunca negativo.
        """
