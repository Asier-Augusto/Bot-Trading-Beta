"""Franjas horarias de la sesión.

Bloquear entradas y cerrar posiciones son dos cosas distintas y dos parámetros
independientes. Entre las 15:00 y las 15:45 las posiciones abiertas siguen vivas con su
stop; simplemente no se abren nuevas. A las 15:45 se cierra lo que quede.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo
from dataclasses import dataclass
from typing import Any


def _parse_time(text: str) -> dt.time:
    """Convierte "HH:MM" en una hora.

    text   : str  — hora en formato de 24 horas.
    return : time — la hora correspondiente.
    """
    hours, minutes = text.split(":")
    return dt.time(int(hours), int(minutes))


@dataclass(frozen=True, slots=True)
class TimeRange:
    """Un tramo horario con su etiqueta.

    start        : time — hora de inicio, incluida.
    end          : time — hora de fin, excluida.
    label        : str  — etiqueta que viaja en los eventos, para el informe.
    allow_entries: bool — si el tramo admite abrir posiciones.
    """

    start: dt.time
    end: dt.time
    label: str
    allow_entries: bool

    def contains(self, moment: dt.time) -> bool:
        """Indica si una hora cae en el tramo.

        moment : time — hora local de la bolsa.
        return : bool
        """
        return self.start <= moment < self.end


@dataclass(frozen=True, slots=True)
class SessionSchedule:
    """Calendario intradía: cuándo se puede entrar y cuándo se cierra todo.

    timezone      : str  — zona horaria de la bolsa.
    trading_windows: tuple[TimeRange, ...] — tramos que admiten entradas.
    danger_zones  : tuple[TimeRange, ...] — tramos etiquetados que las prohíben.
    force_close   : time — hora de cierre forzado de todas las posiciones.
    """

    timezone: str
    trading_windows: tuple[TimeRange, ...]
    danger_zones: tuple[TimeRange, ...]
    force_close: dt.time

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SessionSchedule":
        """Construye el calendario desde el bloque `session` del perfil.

        raw    : dict — contenido de `session` en el YAML.
        return : SessionSchedule
        """
        return cls(
            timezone=str(raw.get("timezone", "America/New_York")),
            trading_windows=tuple(
                TimeRange(
                    _parse_time(w["from"]), _parse_time(w["to"]),
                    w.get("label", "regular"), bool(w.get("allow_entries", True)),
                )
                for w in raw.get("trading_windows", [])
            ),
            danger_zones=tuple(
                TimeRange(
                    _parse_time(z["from"]), _parse_time(z["to"]),
                    z.get("label", "peligro"), bool(z.get("allow_entries", False)),
                )
                for z in raw.get("danger_zones", [])
            ),
            force_close=_parse_time(str(raw.get("force_close", "15:45"))),
        )

    def _local_time(self, ts: dt.datetime) -> dt.time:
        """Pasa un instante a hora local de la bolsa.

        ts     : datetime — instante consciente de zona.
        return : time     — hora local.
        """
        return ts.astimezone(zoneinfo.ZoneInfo(self.timezone)).time()

    def label_at(self, ts: dt.datetime) -> str:
        """Etiqueta de la franja en la que cae un instante.

        La etiqueta viaja en los eventos para que el informe pueda desglosar el
        rendimiento por franja horaria.

        ts     : datetime — instante a etiquetar.
        return : str      — etiqueta, o "regular" si no cae en ninguna zona marcada.
        """
        local = self._local_time(ts)
        for zone in self.danger_zones:
            if zone.contains(local):
                return zone.label
        for window in self.trading_windows:
            if window.contains(local):
                return window.label
        return "fuera_de_sesion"

    def entries_allowed_at(self, ts: dt.datetime) -> bool:
        """Indica si se pueden abrir posiciones en ese instante.

        Las zonas peligrosas mandan sobre las ventanas: si un instante cae en ambas,
        prohíbe la zona.

        ts     : datetime — instante a comprobar.
        return : bool
        """
        local = self._local_time(ts)
        for zone in self.danger_zones:
            if zone.contains(local):
                return zone.allow_entries
        for window in self.trading_windows:
            if window.contains(local):
                return window.allow_entries
        return False

    def is_force_close_time(self, ts: dt.datetime) -> bool:
        """Indica si toca cerrar todas las posiciones.

        ts     : datetime — instante a comprobar.
        return : bool     — True a la hora de cierre forzado y después.
        """
        return self._local_time(ts) >= self.force_close
