"""Zonas de soporte: el tipo de dato público.

En la versión completa del bot este módulo contiene además la detección y puntuación
de zonas (pivotes, agrupación con tolerancia en ATR, ponderación por recencia y volumen,
y fusión por confluencia entre marcos temporales).

**Ese algoritmo no forma parte de esta versión pública.** Lo que sí está aquí es el
contrato: qué es una zona y cómo la consume el motor, que es lo que hace falta para
entender la arquitectura y para escribir una implementación propia.

`SupportZone` vive en `bot.core.context` porque es parte del contexto de solo lectura que
el motor entrega a la estrategia; se reexporta aquí por comodidad.
"""

from __future__ import annotations

from bot.core.context import SupportZone

__all__ = ["SupportZone"]
