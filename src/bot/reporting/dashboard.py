"""Panel de terminal: qué está pasando ahora mismo (§10.2).

**El modelo de vista está separado del pintado**, y no por elegancia: el modelo es lo que se
puede probar. Una tabla bonita que muestra el margen del kill switch mal calculado es peor que
no tener panel, porque invita a confiar en ella.

**Solo lee estado en memoria; nunca toca disco.** Es un `EventSink` como los demás, así que si
va lento o falla no puede parar el motor — el bus ya lo aísla.

Lo que muestra, que es lo que el §10.2 pide: precio, las cuatro condiciones con su valor y su
puntuación, las posiciones abiertas con su resultado y su stop, y **cuánto margen queda hasta
el kill switch**. Ese último es el que se mira cuando el día va mal.
"""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass, field, replace

from bot.contracts.sink import EventSink
from bot.core.events import Event


@dataclass(frozen=True, slots=True)
class ConditionView:
    """Una condición tal como se muestra.

    name      : str   — nombre de la condición.
    raw_value : float — valor crudo del indicador.
    score     : float — puntuación en [0, 1].
    passed    : bool  — si se cumple literalmente.
    """

    name: str
    raw_value: float
    score: float
    passed: bool

    @property
    def display_value(self) -> str:
        """Valor crudo listo para pintar.

        Un NaN significa "no hay dato" y se muestra como tal: pintar "nan" haría dudar de si
        el indicador está roto o simplemente calentando.

        return : str
        """
        if self.raw_value != self.raw_value:
            return "sin dato"
        return f"{self.raw_value:.2f}"


@dataclass(frozen=True, slots=True)
class SymbolView:
    """El estado de un símbolo tal como se muestra.

    symbol     : str   — ticker.
    price      : float — último cierre de vela de 1'.
    total      : float — puntuación total de la última evaluación.
    level      : str   — nivel de esa evaluación.
    conditions : tuple[ConditionView, ...] — desglose por condición.
    updated_at : datetime | None — instante de la última evaluación.
    """

    symbol: str
    price: float = 0.0
    total: float = 0.0
    level: str = "—"
    conditions: tuple[ConditionView, ...] = ()
    updated_at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class PositionView:
    """Una posición abierta tal como se muestra.

    symbol      : str   — ticker.
    tag         : str   — etiqueta del tramo.
    shares      : int   — acciones.
    entry_price : float — precio de entrada.
    stop_price  : float — stop vigente.
    last_price  : float — último precio conocido del símbolo.
    """

    symbol: str
    tag: str
    shares: int
    entry_price: float
    stop_price: float
    last_price: float

    @property
    def unrealized(self) -> float:
        """Resultado no realizado, en divisa.

        return : float — 0.0 si no hay precio conocido.
        """
        if self.last_price <= 0:
            return 0.0
        return (self.last_price - self.entry_price) * self.shares

    @property
    def risk_per_share(self) -> float:
        """Riesgo por acción vigente.

        return : float — entrada menos stop; 0.0 o menos si el stop ya está por encima.
        """
        return self.entry_price - self.stop_price


@dataclass
class DashboardModel:
    """Todo lo que el panel muestra, sin nada de pintado.

    Es lo que se prueba. Se alimenta de los eventos del bus y no consulta a nadie: si el motor
    dejara de emitir, el panel se quedaría quieto en vez de mentir con datos recalculados.

    symbols          : dict[str, SymbolView] — estado por símbolo.
    positions        : dict[tuple, PositionView] — posiciones abiertas por (símbolo, etiqueta).
    daily_pnl        : float — resultado acumulado del día, ya realizado.
    starting_equity  : float — patrimonio al abrir, base del kill switch.
    max_daily_loss_pct : float — fracción que dispara el kill switch.
    closed_trades    : int   — operaciones cerradas hoy.
    last_event_at    : datetime | None — cuándo llegó el último evento.
    """

    symbols: dict[str, SymbolView] = field(default_factory=dict)
    positions: dict[tuple[str, str], PositionView] = field(default_factory=dict)
    daily_pnl: float = 0.0
    starting_equity: float = 0.0
    max_daily_loss_pct: float = 0.03
    closed_trades: int = 0
    last_event_at: dt.datetime | None = None

    @property
    def kill_switch_limit(self) -> float:
        """Pérdida a la que el kill switch corta las entradas.

        return : float — negativa.
        """
        return -abs(self.max_daily_loss_pct * self.starting_equity)

    @property
    def kill_switch_headroom(self) -> float:
        """Cuánto se puede perder todavía antes de que el kill switch corte.

        Es el número que se mira cuando el día va mal, y por eso se calcula y se muestra en
        vez de dejar que nadie lo haga de cabeza con dos cifras de la pantalla.

        return : float — positivo mientras quede margen; 0.0 si ya se agotó.
        """
        return max(0.0, self.daily_pnl - self.kill_switch_limit)

    @property
    def kill_switch_tripped(self) -> bool:
        """Indica si el kill switch ya ha cortado las entradas.

        return : bool
        """
        return self.daily_pnl < self.kill_switch_limit

    @property
    def total_unrealized(self) -> float:
        """Resultado no realizado de todas las posiciones abiertas.

        return : float
        """
        return sum(p.unrealized for p in self.positions.values())


class Dashboard(EventSink):
    """Mantiene el modelo del panel al día con lo que pasa por el bus.

    El modelo se actualiza en el hilo del bus y se pinta desde otro. Por eso hay un cerrojo y
    un `snapshot()`: sin ellos, el pintado iteraría un diccionario que el bus está cambiando y
    el panel se caería con "dictionary changed size during iteration" justo cuando más pasa,
    que es cuando se abren y se cierran posiciones.
    """

    def __init__(self, starting_equity: float = 0.0, max_daily_loss_pct: float = 0.03) -> None:
        """Crea el panel.

        starting_equity    : float — patrimonio al abrir la sesión.
        max_daily_loss_pct : float — fracción que dispara el kill switch.
        """
        self.model = DashboardModel(
            starting_equity=starting_equity, max_daily_loss_pct=max_daily_loss_pct
        )
        self._lock = threading.Lock()

    def snapshot(self) -> DashboardModel:
        """Devuelve una copia estable del modelo, apta para pintar desde otro hilo.

        Las vistas son inmutables, así que basta con copiar los dos diccionarios.

        return : DashboardModel — copia; el original sigue vivo.
        """
        with self._lock:
            return replace(
                self.model,
                symbols=dict(self.model.symbols),
                positions=dict(self.model.positions),
            )

    def handle(self, event: Event) -> None:
        """Actualiza el modelo con un evento.

        event  : Event — cualquiera del bus.
        return : None
        """
        kind = type(event).__name__
        with self._lock:
            self.model.last_event_at = event.ts

            if kind == "BarClosed" and event.bar is not None and event.bar.timeframe == "1m":
                self._on_bar(event.bar)
            elif kind == "SignalEvaluated":
                self._on_evaluation(event)
            elif kind == "PositionOpened":
                self._on_open(event)
            elif kind == "PositionClosed":
                self._on_close(event)
            elif kind == "StopModified":
                self._on_stop(event)

    def close(self) -> None:
        """Nada que cerrar: el panel no toca disco.

        return : None
        """

    def _on_bar(self, bar) -> None:
        """Actualiza el precio del símbolo y el de sus posiciones.

        bar    : Bar — vela de 1' recién cerrada.
        return : None
        """
        actual = self.model.symbols.get(bar.symbol, SymbolView(symbol=bar.symbol))
        self.model.symbols[bar.symbol] = SymbolView(
            symbol=bar.symbol, price=bar.close, total=actual.total, level=actual.level,
            conditions=actual.conditions, updated_at=actual.updated_at,
        )
        for clave, posicion in list(self.model.positions.items()):
            if posicion.symbol != bar.symbol:
                continue
            self.model.positions[clave] = PositionView(
                symbol=posicion.symbol, tag=posicion.tag, shares=posicion.shares,
                entry_price=posicion.entry_price, stop_price=posicion.stop_price,
                last_price=bar.close,
            )

    def _on_evaluation(self, evaluation) -> None:
        """Actualiza la puntuación y el desglose de condiciones de un símbolo.

        evaluation : SignalEvaluated — la evaluación recién publicada.
        return     : None
        """
        actual = self.model.symbols.get(evaluation.symbol, SymbolView(symbol=evaluation.symbol))
        self.model.symbols[evaluation.symbol] = SymbolView(
            symbol=evaluation.symbol,
            price=actual.price,
            total=evaluation.total,
            level=evaluation.level,
            conditions=tuple(
                ConditionView(
                    name=c.get("name", "?"),
                    raw_value=float(c.get("raw_value", float("nan"))),
                    score=float(c.get("score", 0.0)),
                    passed=bool(c.get("passed")),
                )
                for c in evaluation.conditions
            ),
            updated_at=evaluation.ts,
        )

    def _on_open(self, event) -> None:
        """Añade una posición abierta.

        event  : PositionOpened — la apertura.
        return : None
        """
        precio = self.model.symbols.get(event.symbol, SymbolView(symbol=event.symbol)).price
        self.model.positions[(event.symbol, event.tag)] = PositionView(
            symbol=event.symbol, tag=event.tag, shares=event.shares,
            entry_price=event.entry_price, stop_price=event.stop_price,
            last_price=precio or event.entry_price,
        )

    def _on_close(self, event) -> None:
        """Quita una posición cerrada y acumula su resultado.

        event  : PositionClosed — el cierre.
        return : None
        """
        self.model.positions.pop((event.symbol, event.tag), None)
        self.model.daily_pnl += event.pnl
        self.model.closed_trades += 1

    def _on_stop(self, event) -> None:
        """Mueve el stop mostrado de un tramo.

        event  : StopModified — la modificación.
        return : None
        """
        clave = (event.symbol, event.tag)
        posicion = self.model.positions.get(clave)
        if posicion is None:
            return
        self.model.positions[clave] = PositionView(
            symbol=posicion.symbol, tag=posicion.tag, shares=posicion.shares,
            entry_price=posicion.entry_price, stop_price=event.new_stop,
            last_price=posicion.last_price,
        )


def render(model: DashboardModel) -> str:
    """Pinta el modelo como texto plano.

    Existe además del pintado con `rich` para poder mirar el panel en un log, en una tubería o
    en una terminal sin colores — y porque una prueba puede leerlo.

    model  : DashboardModel — el estado a pintar.
    return : str — el panel en texto.
    """
    lineas = []

    estado = "CORTADO" if model.kill_switch_tripped else f"margen {model.kill_switch_headroom:,.2f}"
    lineas.append(
        f"P&L día {model.daily_pnl:+,.2f}  ·  no realizado {model.total_unrealized:+,.2f}  ·  "
        f"cerradas {model.closed_trades}  ·  kill switch: {estado}"
    )
    lineas.append("")

    if model.symbols:
        lineas.append(f"{'símbolo':<8}{'precio':>10}{'nivel':>11}{'total':>8}   condiciones")
        for vista in sorted(model.symbols.values(), key=lambda v: v.symbol):
            detalle = "  ".join(
                f"{'v' if c.passed else 'x'} {c.name}={c.display_value}({c.score:.2f})"
                for c in vista.conditions
            )
            lineas.append(
                f"{vista.symbol:<8}{vista.price:>10.2f}{vista.level:>11}"
                f"{vista.total:>8.3f}   {detalle}"
            )
        lineas.append("")

    if model.positions:
        lineas.append(
            f"{'posición':<8}{'tramo':<14}{'accs':>6}{'entrada':>10}{'stop':>10}{'no real.':>11}"
        )
        for posicion in sorted(model.positions.values(), key=lambda p: (p.symbol, p.tag)):
            lineas.append(
                f"{posicion.symbol:<8}{posicion.tag:<14}{posicion.shares:>6}"
                f"{posicion.entry_price:>10.2f}{posicion.stop_price:>10.2f}"
                f"{posicion.unrealized:>+11.2f}"
            )
    else:
        lineas.append("sin posiciones abiertas")

    return "\n".join(lineas)
