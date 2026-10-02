"""El cierre de una sesión, separado del motor que la corre (deuda E7).

Vive aparte porque es **lo más delicado que hay en el motor** y estaba mezclado con el resto:
corre desde el `finally` de `run()`, casi siempre porque algo ya ha ido mal, y es el
paracaídas de la familia entera de fallos "la sesión murió a media faena". Leerlo entre
seiscientas líneas de decisión de trading no ayudaba a nadie.

Toma el motor entero y no quince colaboradores sueltos, y conviene decir por qué: el cierre
toca el bus, el reloj, el feed, el bróker, el registro de órdenes, las sombras, los
agregadores, el acumulador de IDLE y dos vueltas atrás al propio motor (`_on_bar_closed` al
vaciar agregadores, `_apply_close` al liquidar). Enumerarlos en un constructor sería el mismo
acoplamiento con más ceremonia y una lista que se desincroniza al primer cambio. Lo que se
gana con la separación es que el cierre se lee, se prueba y se cambia solo.
"""

from __future__ import annotations

import datetime as dt

from bot.core.events import SystemEvent


class SessionCloser:
    """Cierra una sesión: vacía, liquida, avisa, sella y desconecta."""

    def __init__(self, engine) -> None:
        """Crea el cierre.

        engine : Engine — el motor cuya sesión se cierra. Ver el docstring del módulo.
        """
        self.engine = engine

    def finish(self) -> None:
        """Vacía los agregadores, cierra lo que quede abierto, vuelca resúmenes y para.

        Se invoca desde el `finally` de `run()`, así que puede llegar aquí con la sesión a
        medias y con una excepción viva. Cada etapa va protegida para que el fallo de una no
        impida las siguientes; las últimas —"parada" y desconectar el feed— son las que no se
        pueden perder bajo ningún concepto.

        return : None
        """
        motor = self.engine
        self.stages()

        cierre = self.stop_timestamp()
        if motor._idle.pending():
            self.guarded("volcar IDLE", lambda: motor._flush_idle(cierre))

        motor.bus.emit(SystemEvent(ts=cierre, level="INFO", message="parada"))
        motor.feed.disconnect()
        motor.bus.drain()

    def stages(self) -> None:
        """Corre las etapas de cierre en orden, cada una protegida de su propio fallo.

        El orden no es arbitrario: vaciar los agregadores antes de cerrar posiciones, porque
        la última vela puede cerrar alguna; cerrar antes de avisar de huérfanas, porque si no
        saldrían como huérfanas las que el cierre acaba de liquidar.

        return : None
        """
        motor = self.engine
        self.guarded("vaciar agregadores", motor._flush_aggregators)
        self.guarded("cerrar sombras", motor._close_shadows)
        self.guarded("cerrar posiciones", motor._close_remaining_positions)
        self.guarded("liberar órdenes", motor._release_unresolved_orders)
        self.guarded("avisar de huérfanas", motor._report_orphan_positions)
        self.guarded("cerrar los que esperan comisión", self._flush_broker_closes)

    def _flush_broker_closes(self) -> None:
        """Registra los cierres que el bróker aún guarda esperando su comisión.

        Solo el de IBKR los tiene: la comisión no viaja con la ejecución, llega después por su
        propio evento, y el cierre espera a tenerla antes de registrarse. Si la sesión acaba
        primero, esa operación —que existió y movió dinero— no aparecería en ningún sitio. El
        bróker la registra igual y deja un aviso diciendo que le falta la comisión.

        return : None
        """
        vaciar = getattr(self.engine.broker, "flush_pending_closes", None)
        if vaciar is not None:
            vaciar()

    def guarded(self, etapa: str, accion) -> None:
        """Ejecuta una etapa del cierre sin dejar que su fallo tumbe las siguientes.

        Si una etapa revienta —el vaciado de agregadores sobre un feed corrupto, un bróker que
        no puede liquidar— las que vienen detrás son justo las que no se pueden perder:
        liberar las claves de orden, emitir "parada" y desconectar. El fallo no se traga en
        silencio: queda como `SystemEvent` de nivel ERROR.

        etapa  : str      — nombre de la etapa, para el registro.
        accion : Callable — la etapa, sin argumentos.
        return : None
        """
        try:
            accion()
        except Exception as error:  # noqa: BLE001 — cerrar la sesión manda sobre todo
            self.engine.bus.emit(
                SystemEvent(
                    ts=self.engine.clock.now(),
                    level="ERROR",
                    message=f"fallo al cerrar la sesión en la etapa '{etapa}'",
                    detail={"error": repr(error)},
                    config_hash=self.engine.config_hash,
                )
            )

    def stop_timestamp(self) -> dt.datetime:
        """Instante con el que sellar "parada", para que cierre la sesión también en el tiempo.

        La última vela de la sesión se sella con su `period_end`, que es POSTERIOR al último
        tick; "parada" se emitiría con `clock.now()`, que es el último tick. Sin este ajuste,
        los últimos `BarClosed` y `SignalEvaluated` llevan un `ts` posterior al de "parada", y
        como la carpeta del día sale de `event.ts`, una sesión que acabe cerca de medianoche
        reparte sus registros entre dos carpetas. Los Planes 4 y 5 leen esas carpetas.

        El arreglo NO es cambiar el sellado de la vela: `period_end` es el instante correcto de
        cierre y así se dejó a propósito. Lo que se corrige es "parada".

        **Vale para los dos relojes.** El intento anterior adelantaba `SimulatedClock`, y en
        vivo no hacía nada: `RealClock` no se puede adelantar, y una vela de 5' en curso al
        parar se sella hasta cinco minutos por delante del reloj. Medido en el ensayo del
        2026-08-26: "parada" a las 11:21:26 y las últimas velas a las 11:22 y las 11:25. Tomar
        el máximo no toca el reloj de nadie y deja el invariante cierto siempre.

        return : datetime — el mayor entre la hora actual y el último instante registrado.
        """
        ahora = self.engine.clock.now()
        if self.engine._last_event_ts is None:
            return ahora
        return max(ahora, self.engine._last_event_ts)
