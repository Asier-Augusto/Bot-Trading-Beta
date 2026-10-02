"""Las instancias de indicador vivas durante una sesión, y su identidad.

Vive fuera del motor por dos motivos. El primero es de tamaño: el motor es el bucle de
decisión y no debe además llevar la contabilidad de qué indicador es cuál. El segundo es
que aquí estaban dos fallos latentes que solo se vuelven reales al haber más de un
indicador del mismo nombre o más de un símbolo, y ambos merecían pruebas propias.

**La identidad de una instancia son cuatro campos**, no tres: nombre, marco, símbolo y
parámetros. Un RSI-14 y un RSI-7 sobre velas de 5' del mismo símbolo son dos indicadores
distintos. Con la clave de tres campos colapsaban en uno solo, en silencio, y la
estrategia leía el que se hubiera construido primero.

**La clave con la que la estrategia los pide sí son tres campos**: `ctx.indicator("rsi",
"5m")`. Los parámetros forman parte de la identidad de la instancia, no de cómo se pide.
De ahí que dos especificaciones que compartan clave pública y difieran en parámetros sean
un error de configuración: el conjunto no puede elegir cuál devolver, así que falla al
construirse y dice cuáles chocan.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterator, TYPE_CHECKING

from bot.contracts.strategy import IndicatorSpec
from bot.core.registry import get_indicator
from bot.core.types import Bar

if TYPE_CHECKING:
    from bot.contracts.history import HistoricalSource

#: Identidad completa de una instancia: (nombre, marco, símbolo, parámetros congelados).
IndicatorKey = tuple[str, str, str, tuple[tuple[str, Any], ...]]

#: Clave con la que la estrategia lo pide: (nombre, marco, símbolo RESUELTO).
#:
#: Resuelto quiere decir el ticker de verdad, nunca None. `MarketContext.indicator` hace
#: `self.indicators.get((name, timeframe, symbol or self.symbol))`, así que una entrada
#: publicada con None por símbolo no la encuentra NADIE: `ctx.indicator("rsi", "1m")` busca
#: ("rsi", "1m", "TSLA"). Publicar la clave sin resolver dejaba los tres indicadores
#: propios de cada símbolo invisibles para la estrategia, con una consecuencia silenciosa y
#: total: las condiciones `rsi_1m`, `rsi_5m` y `support` puntuaban cero en todas las velas
#: de todas las sesiones, y solo la referencia del Nasdaq —que sí lleva símbolo explícito—
#: llegaba a valer algo. El backtest no producía ninguna operación y nada lo denunciaba.
PublicKey = tuple[str, str, str]


def _freeze(params: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    """Congela los parámetros de un indicador para poder usarlos como clave.

    params : dict — parámetros de la especificación, p. ej. {"period": 14}.
    return : tuple — pares (clave, valor) ordenados por clave.
    """
    return tuple(sorted(params.items()))


class IndicatorSet:
    """Las instancias de indicador de una sesión, indexadas por su identidad completa."""

    def __init__(self, watchlist: list[str], specs: list[IndicatorSpec]) -> None:
        """Instancia solo los indicadores que la estrategia declara necesitar.

        Con 300 símbolos no se calculan doce indicadores cuando la estrategia usa cuatro.
        Y si pide uno que no existe, el arranque falla con un mensaje claro en vez de
        reventar a las 14:32.

        watchlist : list[str]            — símbolos operables.
        specs     : list[IndicatorSpec]  — lo que la estrategia pide.
        raises    : KeyError   — si un indicador declarado no está registrado.
                    ValueError — si dos especificaciones comparten clave pública con
                                 parámetros distintos, caso en el que el conjunto no
                                 podría decidir cuál devuelve `ctx.indicator()`.
        """
        self._instances: dict[IndicatorKey, Any] = {}
        #: Clave pública -> clave completa, para resolver `values_for` sin recomponer.
        self._public: dict[PublicKey, IndicatorKey] = {}
        #: Claves públicas que corresponden a una referencia con símbolo explícito. Toda la
        #: watchlist las ve; las demás, solo su propio símbolo.
        self._references: set[PublicKey] = set()
        self._warmed = False

        for symbol in watchlist:
            for spec in specs:
                target = spec.symbol or symbol
                frozen = _freeze(spec.params)
                key: IndicatorKey = (spec.name, spec.timeframe, target, frozen)
                # El símbolo de la clave pública es el RESUELTO, no `spec.symbol`: es el
                # que `MarketContext.indicator` va a buscar. Ver `PublicKey`.
                public: PublicKey = (spec.name, spec.timeframe, target)
                es_referencia = spec.symbol is not None

                existente = self._public.get(public)
                if existente is not None and existente != key:
                    raise ValueError(
                        f"dos indicadores '{spec.name}' en '{spec.timeframe}' para "
                        f"'{target}' comparten la clave con la que la estrategia los "
                        f"pide pero tienen parámetros distintos: {dict(existente[3])} "
                        f"y {dict(frozen)}. La estrategia no podría distinguirlos: "
                        f"dales marcos distintos o nombres registrados distintos."
                    )

                if key not in self._instances:
                    self._instances[key] = get_indicator(spec.name)(**spec.params)
                self._public[public] = key
                self._references.add(public) if es_referencia else None

    def __len__(self) -> int:
        """Cuántas instancias vivas hay.

        return : int — una por identidad completa distinta.
        """
        return len(self._instances)

    def __iter__(self) -> Iterator[tuple[IndicatorKey, Any]]:
        """Recorre las instancias con su identidad completa.

        return : Iterator[tuple[IndicatorKey, Indicator]]
        """
        return iter(self._instances.items())

    def aggregator_keys(self) -> list[tuple[str, str]]:
        """Pares (símbolo, marco) que hace falta agregar para alimentar estos indicadores.

        El motor construye sus agregadores a partir de esto: si faltara un par, ese
        indicador no recibiría una vela en toda la sesión y su valor quedaría en None sin
        que nada lo denunciara.

        return : list[tuple[str, str]] — sin repetidos y en orden determinista.
        """
        vistos: dict[tuple[str, str], None] = {}
        for name, timeframe, target, _params in self._instances:
            vistos[(target, timeframe)] = None
        return list(vistos)

    def update(self, bar: Bar) -> None:
        """Alimenta con una vela a los indicadores de su símbolo y su marco.

        bar    : Bar — vela recién cerrada.
        return : None
        """
        for (name, timeframe, target, _params), indicator in self._instances.items():
            if target == bar.symbol and timeframe == bar.timeframe:
                indicator.update(bar)

    def values_for(self, symbol: str) -> dict[PublicKey, float]:
        """Valores vigentes de los indicadores que puede ver un símbolo concreto.

        "Puede ver" son dos conjuntos, y confundirlos rompe cosas distintas:

        - Los suyos: las especificaciones **sin** símbolo explícito, que significan "el
          símbolo que se está evaluando". Devolver aquí los de otro símbolo era el fallo
          D12: inerte mientras `ctx.indicator()` indexe siempre por símbolo, pero un arma
          cargada para cualquier cosa que itere `ctx.indicators`.
        - Las referencias: las especificaciones **con** símbolo explícito (ONEQ, QQQ), que
          toda la watchlist comparte. Filtrarlas por símbolo dejaría la condición
          `rsi_ndx` sin poder cumplirse jamás.

        symbol : str  — ticker que se está evaluando.
        return : dict — (nombre, marco, símbolo RESUELTO) -> valor, con la clave exacta que
                        `MarketContext.indicator` va a buscar. Solo los que ya tienen valor:
                        un indicador en calentamiento no aparece, para que la estrategia
                        no tenga que distinguir "no lo pedí" de "aún no vale".
        """
        values: dict[PublicKey, float] = {}
        for public, key in self._public.items():
            if public not in self._references and public[2] != symbol:
                continue
            indicator = self._instances[key]
            if indicator.value is not None:
                values[public] = indicator.value
        return values

    def warmup(
        self,
        source: "HistoricalSource",
        end: dt.datetime,
        skip_session_gap: bool = True,
    ) -> dict[str, Any]:
        """Alimenta cada indicador con histórico para que llegue listo al primer tick.

        Es la respuesta al problema estructural del RSI de 5': a las 09:30, en vivo, no
        hay ni una vela de 5' de hoy, y un RSI-14 sobre ellas no vale nada hasta las
        10:45. Lo mismo pasa con el ATR de 5', del que dependen el stop y la tolerancia de
        las zonas de soporte. Calentar solo el backtest dejaría el bot ciego durante los
        primeros 75 minutos de cada sesión real, que es la franja de más volatilidad.

        Se llama **una vez por sesión**, con `end` en el instante del primer tick: las
        velas devueltas cierran en `end` o antes, así que ninguna del futuro entra aquí.

        **El hueco nocturno se descarta por defecto.** El salto entre el cierre de ayer y
        la apertura de hoy no es un movimiento intradía, y meterlo en el indicador lo
        distorsiona durante horas: medido, un hueco del 1 % deja el RSI de 5' de TSLA en 78
        al abrir con el intradía neutro, y las 30 primeras velas de la sesión difieren en
        17,5 puntos de media. Con el umbral de Moris en 35, la condición `rsi_5m` estaría
        dominada por el hueco durante toda la ventana de entrada. La tabla completa está en
        `Indicator.start_session`.

        source           : HistoricalSource — de dónde salen las velas del pasado.
        end              : datetime — corte superior, incluido.
        skip_session_gap : bool — si se descarta el salto entre la última vela calentada y
                           la primera de la sesión. True salvo que se quiera reproducir el
                           comportamiento de una plataforma que lo incluye.
        return : dict — resumen para el registro:
                 {"bars", "indicators", "not_ready", "skip_session_gap"}.
        """
        if self._warmed:
            return {"bars": 0, "indicators": 0, "not_ready": [], "skip_session_gap": False}
        self._warmed = True

        velas = 0
        for (name, timeframe, target, _params), indicator in self._instances.items():
            necesarias = indicator.warmup_bars
            if necesarias <= 0:
                continue
            for bar in source.bars(target, timeframe, end, necesarias):
                indicator.update(bar)
                velas += 1

        if skip_session_gap:
            for indicator in self._instances.values():
                indicator.start_session()

        frios = [
            f"{name}:{timeframe}:{target}"
            for (name, timeframe, target, _p), indicator in self._instances.items()
            if indicator.value is None
        ]
        return {
            "bars": velas,
            "indicators": len(self._instances),
            "not_ready": frios,
            "skip_session_gap": skip_session_gap,
        }
