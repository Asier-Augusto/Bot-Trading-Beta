"""Puntuación continua de señales.

Cada condición devuelve un valor en [0, 1] en vez de un booleano, y el total ponderado
determina con qué nivel de detalle se registra la evaluación.

REGLA CAPITAL: la puntuación decide QUÉ SE REGISTRA, nunca SI SE OPERA. `ARMED` exige
que todas las condiciones se cumplan literalmente. Si un 0.99 armara el gatillo,
habríamos relajado los criterios de entrada sin darnos cuenta, disfrazado de cambio de
registro.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Iterable, Literal

Direction = Literal["below", "above"]


class SignalLevel(enum.IntEnum):
    """Nivel de relevancia de una evaluación, de menos a más interesante."""

    IDLE = 0
    PARTIAL = 1
    NEAR_MISS = 2
    ARMED = 3


@dataclass(frozen=True, slots=True)
class LevelThresholds:
    """Umbrales que separan los niveles de relevancia.

    near_miss          : float — total mínimo para registrar como NEAR_MISS.
    partial            : float — total mínimo para registrar como PARTIAL.
    armed_requires_all : bool  — si ARMED exige que todas se cumplan literalmente.
                                 Ponerlo a False cambia la estrategia, no el registro.
    """

    near_miss: float = 0.80
    partial: float = 0.55
    armed_requires_all: bool = True


@dataclass(frozen=True, slots=True)
class ConditionSpec:
    """Cómo se puntúa una condición.

    name      : str           — nombre estable, el que aparece en los logs.
    weight    : float         — peso relativo; se normaliza contra la suma de todos.
    reach     : float         — a qué distancia del umbral la puntuación llega a cero.
    min_score : float | None  — suelo de veto: por debajo, la evaluación cae a IDLE.
    direction : Direction     — "below" para umbrales "<=", "above" para ">=".
    """

    name: str
    weight: float = 1.0
    reach: float = 1.0
    min_score: float | None = None
    direction: Direction = "below"


@dataclass(frozen=True, slots=True)
class ConditionResult:
    """Lo que una condición produjo en una evaluación concreta.

    name      : str          — nombre de la condición.
    raw_value : float        — valor crudo del indicador. Se guarda para poder
                               recalcular con otra función de puntuación sin repetir
                               el backtest.
    threshold : float        — umbral contra el que se comparó.
    score     : float        — puntuación en [0, 1].
    weight    : float        — peso sin normalizar.
    passed    : bool         — si la condición se cumple literalmente.
    min_score : float | None — suelo de veto aplicable.
    """

    name: str
    raw_value: float
    threshold: float
    score: float
    weight: float
    passed: bool
    min_score: float | None = None


@dataclass(frozen=True, slots=True)
class SignalScore:
    """Resultado completo de puntuar una evaluación.

    conditions : tuple[ConditionResult, ...] — desglose por condición.
    total      : float       — puntuación ponderada y normalizada, en [0, 1].
    level      : SignalLevel — nivel de relevancia para el registro.
    armed      : bool        — si la estrategia debe armar el gatillo.
    blocking   : tuple[str, ...] — condiciones que no se cumplen.
    """

    conditions: tuple[ConditionResult, ...]
    total: float
    level: SignalLevel
    armed: bool
    blocking: tuple[str, ...]

    def to_payload(self) -> tuple[dict[str, Any], ...]:
        """Serializa el desglose para meterlo en un evento.

        return : tuple[dict, ...] — una fila por condición, con valor crudo y puntuación.
        """
        return tuple(
            {
                "name": c.name,
                "raw_value": c.raw_value,
                "threshold": c.threshold,
                "score": c.score,
                "weight": c.weight,
                "passed": c.passed,
            }
            for c in self.conditions
        )


def ramp_score(
    value: float,
    threshold: float,
    reach: float,
    direction: Direction = "below",
) -> float:
    """Puntúa cuánto se acerca un valor a cumplir su umbral.

    Rampa lineal: cumplido vale 1.0, y la puntuación baja linealmente hasta 0.0 a una
    distancia `reach` del umbral.

    value     : float     — valor observado del indicador.
    threshold : float     — umbral de la condición.
    reach     : float     — distancia a la que la puntuación llega a cero. Positiva.
    direction : Direction — "below" si la condición es "<=", "above" si es ">=".
    return    : float     — puntuación en [0, 1].
    raises    : ValueError — si el alcance no es positivo.
    """
    if reach <= 0:
        raise ValueError("el alcance debe ser positivo")

    distance = (value - threshold) if direction == "below" else (threshold - value)
    if distance <= 0:
        return 1.0
    return max(0.0, 1.0 - distance / reach)


def score_signal(
    results: Iterable[ConditionResult],
    levels: LevelThresholds,
) -> SignalScore:
    """Combina las condiciones en una puntuación total y un nivel de relevancia.

    results : Iterable[ConditionResult] — resultado de cada condición.
    levels  : LevelThresholds           — umbrales de nivel.
    return  : SignalScore               — total normalizado, nivel y desglose.
    raises  : ValueError — si alguna condición trae un peso negativo.
    """
    conditions = tuple(results)
    if not conditions:
        return SignalScore((), 0.0, SignalLevel.IDLE, False, ())

    # La validación va AQUÍ y no en `ConditionSpec`: `ConditionSpec` es una descripción de
    # cómo se puntúa algo, y el invariante que importa no es suyo sino del total. Un peso
    # negativo sacaría el total fuera de [0, 1], y con eso se cae el argumento de
    # equivalencia que sostiene la garantía capital del módulo: que un total alto no puede
    # armar el gatillo porque `armed` sale de las condiciones cumplidas, no del total. Con
    # totales fuera de rango los umbrales de nivel dejan de significar nada.
    negativos = [c.name for c in conditions if c.weight < 0]
    if negativos:
        raise ValueError(
            f"peso negativo en {negativos}: sacaría el total fuera de [0, 1] y con ello "
            f"los umbrales de nivel dejarían de significar nada"
        )

    weight_sum = sum(c.weight for c in conditions)
    total = (
        sum(c.score * c.weight for c in conditions) / weight_sum if weight_sum > 0 else 0.0
    )

    blocking = tuple(c.name for c in conditions if not c.passed)
    armed = not blocking if levels.armed_requires_all else total >= 1.0

    # El veto manda sobre el total: una condición por debajo de su suelo tumba la
    # evaluación a IDLE por alta que sea la puntuación de las demás.
    vetoed = any(
        c.min_score is not None and c.score < c.min_score for c in conditions
    )

    if armed:
        level = SignalLevel.ARMED
    elif vetoed:
        level = SignalLevel.IDLE
    elif total >= levels.near_miss:
        level = SignalLevel.NEAR_MISS
    elif total >= levels.partial:
        level = SignalLevel.PARTIAL
    else:
        level = SignalLevel.IDLE

    return SignalScore(conditions, total, level, armed, blocking)
