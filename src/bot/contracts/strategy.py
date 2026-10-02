"""Contrato de las estrategias.

Una estrategia no habla con el bróker, no escribe en disco y no consulta la hora del
sistema. Recibe un contexto de solo lectura y devuelve intenciones. Esa restricción es
lo que permite probarla sin red y que sea idéntica en vivo y en backtest.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bot.core.types import Intent

if TYPE_CHECKING:
    from bot.core.context import MarketContext
    from bot.core.events import Event
    from bot.core.scoring import SignalScore


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    """Un indicador que una estrategia necesita en su contexto.

    name      : str        — nombre registrado, p. ej. "rsi".
    timeframe : str        — marco temporal, p. ej. "5m".
    symbol    : str | None — ticker; None significa "el símbolo que se está evaluando".
                             Se usa un ticker explícito para referencias externas como
                             ONEQ o QQQ.
    params    : dict       — parámetros del indicador, p. ej. {"period": 14}.
    """

    name: str
    timeframe: str
    symbol: str | None = None
    params: dict[str, Any] = field(default_factory=dict, compare=False)

    def __hash__(self) -> int:
        # Los parámetros entran en la identidad: un RSI-14 y un RSI-7 sobre el mismo
        # marco y símbolo son dos indicadores distintos, y con `compare=False` en el
        # campo salían iguales. `params` es un dict y no es hashable, así que se congela.
        return hash((self.name, self.timeframe, self.symbol, tuple(sorted(self.params.items()))))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, IndicatorSpec):
            return NotImplemented
        return (
            self.name == other.name
            and self.timeframe == other.timeframe
            and self.symbol == other.symbol
            and self.params == other.params
        )

    @property
    def key(self) -> tuple[str, str, str | None]:
        """Clave con la que el indicador se guarda en el contexto.

        return : tuple — (nombre, marco, símbolo).
        """
        return (self.name, self.timeframe, self.symbol)


class Strategy(ABC):
    """Decide qué comprar y cuándo, sin efectos secundarios.

    Hay UNA sola instancia de estrategia para toda la watchlist. De ahí que el
    resultado de la última evaluación no pueda vivir en una ranura de la instancia: se
    guarda indexado por símbolo, y el motor lo lee por símbolo. Con una sola ranura,
    una estrategia que retornara antes de rellenarla haría que el motor atribuyera a
    este símbolo la puntuación del anterior.

    **El mismo patrón vale para TODO estado por símbolo** que una estrategia necesite: la
    ventana de armado, el mínimo del tramo, qué tramos han disparado. Un solo valor
    compartido hace que armar en TSLA silencie la vela del mismo minuto de NVDA, y muerde
    justo cuando la estrategia dispara — un RSI del Nasdaq bajo significa que cae el
    mercado entero y todos los símbolos arman a la vez.

    **`Strategy` no puede declarar `__slots__`.** `_evaluations` se materializa de forma
    perezosa en la primera llamada a `record_evaluation`, para no obligar a cada
    estrategia a acordarse de llamar a `super().__init__()`. Esa asignación necesita un
    `__dict__` de instancia, y quien lo garantiza es esta clase base: mientras no declare
    ranuras, toda instancia de toda subclase tiene `__dict__`.

    Lo comprobado, para que nadie lo "arregle" en la dirección equivocada: una subclase
    que declare `__slots__ = ()` o que use `@dataclass(slots=True)` **funciona igual**,
    porque hereda el `__dict__` de esta base. La restricción es de la base, no de las
    subclases; hay una prueba que la fija en `tests/strategies/test_contrato_strategy.py`.
    """

    #: Nombre registrado. Lo rellena el decorador de registro.
    name: str = ""
    #: Versión del código de estrategia; viaja en cada evento que produce.
    version: str = "0.1.0"

    #: Última evaluación de cada símbolo: ticker -> (puntuación, identificador de
    #: correlación). Empieza en None y se crea en la primera llamada a
    #: `record_evaluation`, para no obligar a cada estrategia a acordarse de llamar a
    #: `super().__init__()`.
    _evaluations: dict[str, tuple["SignalScore", str]] | None = None

    def record_evaluation(
        self,
        symbol: str,
        score: "SignalScore",
        signal_id: str = "",
    ) -> None:
        """Anota el resultado de evaluar UN símbolo.

        La estrategia la llama en cada evaluación, se opere o no: una evaluación
        bloqueada sigue siendo un casi que interesa registrar.

        symbol    : str         — ticker evaluado.
        score     : SignalScore — puntuación completa de esa evaluación.
        signal_id : str         — identificador de correlación de esa evaluación.
        return    : None
        """
        if self._evaluations is None:
            self._evaluations = {}
        self._evaluations[symbol] = (score, signal_id)

    def score_for(self, symbol: str) -> "SignalScore | None":
        """Puntuación de la última evaluación de un símbolo.

        symbol : str — ticker.
        return : SignalScore | None — None si ese símbolo no se ha evaluado nunca.
                 Nunca la puntuación de otro símbolo.
        """
        entry = (self._evaluations or {}).get(symbol)
        return entry[0] if entry else None

    def signal_id_for(self, symbol: str) -> str:
        """Identificador de correlación de la última evaluación de un símbolo.

        symbol : str — ticker.
        return : str — vacío si ese símbolo no se ha evaluado nunca.
        """
        entry = (self._evaluations or {}).get(symbol)
        return entry[1] if entry else ""

    def drain_events(self) -> list["Event"]:
        """Hechos que la estrategia ha producido y no puede publicar.

        Una estrategia no conoce el bus: si necesita dejar constancia de algo —que una
        ventana de armado caducó sin disparar, por ejemplo— lo acumula y el motor lo recoge
        aquí después de cada `on_bar`. Es el mismo patrón que el bróker y que el proveedor
        de soportes.

        El defecto devuelve la lista vacía para que una estrategia que no produzca hechos
        no tenga que implementarla y el motor pueda llamarla siempre sin comprobar nada.

        return : list[Event] — hechos pendientes; vacía por defecto.
        """
        return []

    @abstractmethod
    def required_indicators(self) -> list[IndicatorSpec]:
        """Indicadores que esta estrategia necesita en el contexto.

        El motor lo lee al arrancar e instancia solo esos: con 300 símbolos no se
        calculan doce indicadores cuando la estrategia usa cuatro. Y si pide uno que no
        existe, el arranque falla con un mensaje claro en vez de reventar a las 14:32.

        return : list[IndicatorSpec] — especificaciones necesarias.
        """

    @abstractmethod
    def on_bar(self, ctx: "MarketContext") -> list[Intent]:
        """Evalúa el cierre de vela y decide si hay que actuar.

        ctx    : MarketContext — indicadores, zonas de soporte, posición abierta, foto
                                 de cuenta, hora y configuración. Solo lectura.
        return : list[Intent]  — intenciones; vacía si no hay que actuar.
        """
