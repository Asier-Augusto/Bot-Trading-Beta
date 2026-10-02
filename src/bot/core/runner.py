"""El bucle en vivo: montar, correr y cerrar bien.

El motor no cambia entre backtest y vivo — es toda la idea del reloj inyectado y de los
módulos intercambiables. Lo que cambia es quién lo monta y quién decide cuándo parar, y eso
es lo que hay aquí.

Tres cosas que solo existen en vivo:

1. **Ctrl-C tiene que cerrar la sesión, no matar el proceso.** Si el proceso muere a media
   vela, las claves anti-duplicado se quedan puestas en un `RiskState` que puede sobrevivir,
   no se avisa de posiciones huérfanas y no se emite "parada". Aquí se captura la señal, se le
   pide al feed que termine su flujo, y el cierre de sesión corre como en cualquier otra.
2. **Un límite de duración.** Para poder decir "corre media hora y para", que es lo que hace
   falta para un ensayo.
3. **Un resumen al final**, porque en vivo nadie va a leer el JSONL entero para saber si
   pasó algo.
4. **El panel de terminal** (§10.2), que solo tiene sentido cuando hay alguien mirando.
"""

from __future__ import annotations

import datetime as dt
import signal
from dataclasses import dataclass, field
from pathlib import Path

from bot.core.events import PositionClosed


@dataclass
class LiveResult:
    """Lo que dejó una sesión en vivo.

    trades         : list[PositionClosed] — operaciones cerradas, en orden.
    total_pnl      : float — suma de resultados, neta de comisiones.
    open_positions : int   — posiciones que quedaron abiertas. Debe ser 0.
    bars           : int   — velas de 1' evaluadas. Si es 0, no llegaron datos.
    signals        : int   — evaluaciones por encima de IDLE.
    config_hash    : str   — resumen de la configuración usada.
    delayed        : bool  — si los datos eran diferidos. Con True, las cifras de arriba NO
                     son un resultado de trading: el precio llegaba con quince minutos de
                     retraso y toda decisión se tomó sobre algo que ya no existía.
    """

    trades: list[PositionClosed] = field(default_factory=list)
    total_pnl: float = 0.0
    open_positions: int = 0
    bars: int = 0
    signals: int = 0
    config_hash: str = ""
    delayed: bool = False


class _Counter:
    """Observador mínimo que cuenta lo que pasó, para el resumen final."""

    def __init__(self) -> None:
        self.bars = 0
        self.signals = 0

    def handle(self, event) -> None:
        """Cuenta un evento.

        event  : Event — cualquiera del bus.
        return : None
        """
        kind = type(event).__name__
        if kind == "BarClosed" and getattr(event.bar, "timeframe", "") == "1m":
            self.bars += 1
        elif kind == "SignalEvaluated":
            self.signals += 1

    def close(self) -> None:
        """Nada que cerrar.

        return : None
        """


class OpenPositionsMismatch(RuntimeError):
    """IBKR tiene posiciones que la contabilidad del bot no conoce (deuda F2).

    Se levanta al arrancar en vez de seguir porque seguir es peor: esas acciones no cuentan
    para `max_positions`, su stop no se vigila desde aquí, y la estrategia puede abrir otra
    posición encima del mismo símbolo sin saber que ya hay una. Un bot que arranca creyendo
    que la cuenta está plana cuando no lo está toma todas sus decisiones sobre una cuenta que
    no existe.

    La salida es aplanar la cuenta o arrancar a sabiendas con `--ignore-open-positions`: una
    posición puesta a mano en la misma cuenta es legítima, lo que no vale es no enterarse.
    """


def check_open_positions(broker, allow: bool = False) -> list[dict]:
    """Reconcilia con el bróker antes de dejar correr el motor.

    broker : object — el bróker; si no sabe reconciliar, no hay nada que comprobar. Es el
             caso del simulado: su contabilidad ES la verdad, no hay una segunda fuente.
    allow  : bool — True deja arrancar con desajustes, dejándolos registrados igual.
    return : list[dict] — los desajustes encontrados.
    raises : OpenPositionsMismatch — si hay desajustes y `allow` es False.
    """
    reconciliar = getattr(broker, "reconcile", None)
    if reconciliar is None:
        return []

    desajustes = reconciliar()
    if desajustes and not allow:
        detalle = ", ".join(
            f"{d['symbol']}: IBKR {d['ibkr']:g} / bot {d['bot']:g}" for d in desajustes
        )
        raise OpenPositionsMismatch(
            f"IBKR tiene posiciones que el bot no conoce ({detalle}). Aplana la cuenta o "
            f"arranca con --ignore-open-positions si sabes lo que hay"
        )
    return desajustes


def run_live(
    profile_path: Path,
    log_root: Path,
    watchlist: list[str] | None = None,
    max_minutes: float | None = None,
    client_id: int | None = None,
    panel: bool = True,
    ignore_open_positions: bool = False,
    viewer: bool = False,
) -> LiveResult:
    """Monta el sistema en modo vivo y corre hasta que la sesión termina o se pide parar.

    profile_path : Path — perfil YAML. De él salen el feed, el bróker y los ajustes de IBKR.
    log_root     : Path — carpeta raíz de los logs.
    watchlist    : list[str] | None — sobrescribe la del perfil.
    max_minutes  : float | None — para al cabo de N minutos. None corre hasta el fin de sesión.
    client_id    : int | None — sobrescribe el `clientId` del perfil.
    panel        : bool — pinta el panel de terminal si el perfil lo trae activado. False lo
                   apaga aunque el perfil lo pida, que es lo que hace falta al redirigir la
                   salida a un fichero o al correr sin terminal.
    ignore_open_positions : bool — arranca aunque IBKR tenga posiciones que el bot no
                   conoce. Por defecto NO: ver `OpenPositionsMismatch`.
    viewer       : bool — levanta el visor de mercado en http://localhost:8770 durante la
                   sesión. Es un sink más: no toca el camino de decisión.
    return       : LiveResult
    raises       : OpenPositionsMismatch — desajuste de posiciones al arrancar (deuda F2).
    """
    # NOTA DE LA VERSION PUBLICA
    # `build_engine` vive en `bot.main`: es el cableado que, leyendo un perfil, elige el
    # feed, el broker y los observadores concretos y los enchufa al motor. Ese cableado y el
    # adaptador de IBKR no forman parte de esta version publica, asi que el modo vivo no
    # puede arrancar aqui. El resto del modulo -- el panel, la comprobacion de posiciones
    # abiertas, los manejadores de parada -- si esta, y es lo que se prueba.
    raise NotImplementedError(
        "El modo vivo necesita `bot.main.build_engine` y el adaptador de IBKR, que no "
        "forman parte de esta version publica. Ver README."
    )

    from bot.config.loader import load_profile
    from bot.main import build_engine  # noqa: F401  (inalcanzable; ver nota de arriba)

    profile = load_profile(profile_path)
    engine, broker = build_engine(profile, data_path=None, log_root=log_root,
                                  watchlist=watchlist, live=True, client_id=client_id,
                                  extra_sinks=["web"] if viewer else None)
    if viewer:
        print("Visor en http://localhost:8770", flush=True)

    # Antes de mirar una sola vela: lo que IBKR dice tener tiene que cuadrar con lo que el
    # bot cree tener. Su contabilidad arranca vacía, así que una sesión anterior que murió
    # con una posición abierta la dejaría invisible.
    check_open_positions(broker, allow=ignore_open_positions)

    contador = _Counter()
    engine.bus.subscribe(contador)

    feed = engine.feed
    _install_stop_handlers(feed)
    _install_deadline(feed, engine.clock, max_minutes)

    with _build_panel(profile, engine.bus, panel and _is_a_terminal()):
        try:
            engine.run()
        finally:
            engine.bus.stop()

    trades = list(getattr(broker, "closed_trades", []))
    return LiveResult(
        trades=trades,
        total_pnl=sum(t.pnl for t in trades),
        open_positions=len(broker.positions()),
        bars=contador.bars,
        signals=contador.signals,
        config_hash=profile.config_hash,
        delayed=bool(getattr(feed, "connection", None) and feed.connection.delayed),
    )


def _is_a_terminal() -> bool:
    """Indica si la salida es una terminal de verdad.

    Con la salida redirigida a un fichero, el panel escribiría códigos de control y secuencias
    de reposicionamiento del cursor en medio del log. Es exactamente lo que pasa en un ensayo
    largo, que es como se corre esto de verdad.

    return : bool
    """
    import sys

    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _build_panel(profile, bus, enabled: bool):
    """Monta el panel de terminal y lo suscribe al bus si el perfil lo pide.

    Devuelve siempre un gestor de contexto para que el bucle de arriba no tenga que
    ramificarse: sin panel, es uno que no hace nada.

    profile : Profile — de aquí salen el interruptor y las cifras del kill switch.
    bus     : EventBus — al que se suscribe el observador.
    enabled : bool — permiso de quien llama; el perfil manda además.
    return  : contextmanager — el panel, o uno inerte.
    """
    import contextlib

    if not enabled or not (profile.raw.get("dashboard") or {}).get("enabled", False):
        return contextlib.nullcontext()

    from bot.reporting.dashboard import Dashboard

    riesgo = profile.raw.get("risk", {})
    ejecucion = profile.raw.get("execution", {})
    panel = Dashboard(
        starting_equity=float(ejecucion.get("starting_cash", 100_000.0)),
        max_daily_loss_pct=float(riesgo.get("max_daily_loss_pct", 0.03)),
    )
    return _PanelSession(panel, bus)


class _PanelSession:
    """Suscribe el panel al bus mientras dura la sesión.

    Se suscribe DENTRO del gestor de contexto, no antes, para que un fallo montando el pintado
    no deje un observador colgado del bus.
    """

    def __init__(self, dashboard, bus) -> None:
        """Guarda el panel y el bus.

        dashboard : Dashboard — el observador que se va a suscribir y pintar.
        bus       : EventBus — al que suscribirlo.
        """
        self.dashboard = dashboard
        self.bus = bus
        self._live = None

    def __enter__(self):
        """Suscribe el panel y arranca el pintado.

        return : _PanelSession
        """
        from bot.reporting.live_panel import LivePanel

        self.bus.subscribe(self.dashboard)
        self._live = LivePanel(self.dashboard)
        self._live.__enter__()
        return self

    def __exit__(self, *excepcion) -> None:
        """Para el pintado.

        return : None
        """
        if self._live is not None:
            self._live.__exit__(*excepcion)
            self._live = None


def _install_stop_handlers(feed) -> None:
    """Hace que Ctrl-C pida parar el flujo en vez de matar el proceso.

    Matar el proceso a media vela deja las claves anti-duplicado puestas, sin aviso de
    posiciones huérfanas y sin "parada" en el registro. Pedir al feed que termine su
    generador deja que el cierre de sesión corra como en cualquier otra.

    Si el feed no sabe parar —el de fichero, por ejemplo—, no se instala nada: el
    comportamiento por defecto de Ctrl-C ya es el correcto ahí.

    feed   : MarketDataFeed — el feed activo.
    return : None
    """
    if not hasattr(feed, "stop"):
        return

    def _parar(_signum, _frame):
        print("\nparando: se cerrará la sesión antes de salir...", flush=True)
        feed.stop()

    for señal in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if señal is not None:
            try:
                signal.signal(señal, _parar)
            except (ValueError, OSError):
                # En un hilo secundario no se pueden instalar manejadores. No es un error:
                # simplemente no habrá parada por señal.
                pass


def _install_deadline(feed, clock, max_minutes: float | None) -> None:
    """Envuelve el flujo del feed para que pare al cabo de N minutos.

    Se hace envolviendo `stream` y no con un temporizador aparte porque así la parada ocurre
    entre dos ticks, nunca a mitad de procesar uno: el motor no se entera de que existe un
    límite.

    feed        : MarketDataFeed — el feed activo.
    clock       : Clock — reloj inyectado.
    max_minutes : float | None — minutos de corrida; None no instala nada.
    return      : None
    """
    if max_minutes is None:
        return

    original = feed.stream
    limite = clock.now() + dt.timedelta(minutes=max_minutes)

    def _acotado():
        for tick in original():
            yield tick
            if clock.now() >= limite:
                if hasattr(feed, "stop"):
                    feed.stop()
                return

    feed.stream = _acotado
