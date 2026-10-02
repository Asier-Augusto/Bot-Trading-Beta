"""Trailing stop: subir el stop de una posición que ya va a favor.

Vive en el riesgo y no en la estrategia, y no por comodidad. El §7.3 del diseño dice que el
stop vive **en el bróker como orden real**, de modo que si el proceso muere o se va la luz
la posición sigue protegida; lo que el bot hace cuando el beneficio no realizado supera el
umbral es **modificar esa orden**. Una estrategia no habla con el bróker —esa restricción es
lo que permite probarla sin red y que sea idéntica en vivo y en backtest—, así que el
trailing no puede ser una señal de estrategia: es una decisión de gestión de la posición.

**Nunca baja un stop.** No es una precaución: un trailing que retrocede amplía el riesgo de
una posición que ya va a favor, sin que nadie lo haya decidido, y convierte una operación
ganadora en perdedora. La regla se aplica dos veces —aquí, que no lo propone, y en el
bróker, que no lo aceptaría— porque es la clase de error que solo se descubre con dinero
puesto.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from bot.core.context import Position
from bot.core.types import Bar


@dataclass(frozen=True, slots=True)
class TrailingParams:
    """Cuándo se activa el trailing y a qué distancia sigue al precio.

    enabled       : bool  — si está activo. Apagarlo deja los stops donde la entrada los puso.
    activate_at_r : float — beneficio no realizado, en múltiplos de R, que lo activa. Existe
                    para no estrechar una posición que todavía respira: a +0,3R el precio
                    aún está dentro del ruido normal de la entrada, y subir el stop ahí es
                    la forma más eficaz de convertir una operación buena en una salida
                    prematura.
    distance_atr  : float — a cuántos ATR del máximo alcanzado se sitúa el stop.
    wide_tags     : tuple[str, ...] — tramos que siguen al precio MÁS DE LEJOS. El tramo
                    agresivo entra antes que el conservador, sin la confirmación de la
                    ruptura, y la subida no está garantizada: se le deja más sitio para
                    respirar. Va por etiqueta y no por una constante de la estrategia
                    porque el riesgo no conoce a las estrategias, ni debe.
    wide_slack    : float — cuánto se multiplica `distance_atr` en esos tramos.
    breakeven_at_r: float — a partir de este beneficio en R, el stop no baja del precio de
                    entrada. Es la regla de "no devolver nunca lo ganado": pedida el
                    2026-09-10 después de ver operaciones que subían un 1 % y se cerraban
                    en pérdida. 0 la desactiva.
    """

    enabled: bool = True
    activate_at_r: float = 1.0
    distance_atr: float = 1.5
    wide_tags: tuple[str, ...] = ("aggressive",)
    wide_slack: float = 1.5
    breakeven_at_r: float = 1.0

    @classmethod
    def from_dict(cls, raw: dict) -> "TrailingParams":
        """Construye los parámetros desde el bloque `risk.trailing` del perfil.

        raw    : dict — contenido de `risk.trailing` en el YAML.
        return : TrailingParams
        """
        return cls(
            enabled=bool(raw.get("enabled", True)),
            activate_at_r=float(raw.get("activate_at_r", 1.0)),
            distance_atr=float(raw.get("distance_atr", 1.5)),
            wide_tags=tuple(raw.get("wide_tags", ("aggressive",))),
            wide_slack=float(raw.get("wide_slack", 1.5)),
            breakeven_at_r=float(raw.get("breakeven_at_r", 1.0)),
        )

    def distance_for(self, tag: str) -> float:
        """Distancia al máximo, en ATR, del tramo con esta etiqueta.

        tag    : str — etiqueta del tramo.
        return : float — ATR de separación.
        """
        return self.distance_atr * (self.wide_slack if tag in self.wide_tags else 1.0)


class TrailingStopManager:
    """Decide qué stops hay que subir, tramo a tramo."""

    def __init__(self, params: TrailingParams) -> None:
        """Crea el gestor.

        params : TrailingParams — umbral de activación y distancia.
        """
        self.params = params

    def review(
        self,
        positions: Sequence[Position],
        bar: Bar,
        atr: float | None,
    ) -> list[tuple[str, str, float]]:
        """Qué stops hay que subir tras esta vela.

        Se revisa TRAMO A TRAMO. En la variante mixta el agresivo entra más abajo y con un
        stop más amplio, así que alcanza su 1R en un punto distinto del conservador: un
        umbral común los trataría como si fueran la misma posición y borraría justamente la
        diferencia que la variante mixta existe para medir.

        La referencia del beneficio es el MÁXIMO de la vela, no su cierre. El stop es una
        orden viva en el mercado: lo que importa es hasta dónde llegó el precio mientras la
        orden estaba puesta, no dónde acabó el minuto.

        De cada tramo salen hasta dos candidatos —el seguido, a su distancia; y el precio de
        entrada, si ya se ha ganado `breakeven_at_r`— y gana el más alto de los dos. Nunca
        se propone uno peor que el que la posición ya tiene: el stop no retrocede.

        positions : Sequence[Position] — tramos abiertos del símbolo de la vela.
        bar       : Bar   — vela recién cerrada.
        atr       : float | None — ATR vigente. None o no positivo (calentamiento) no mueve
                    nada: inventar una distancia sería peor que dejar el stop original.
        return    : list[tuple[str, str, float]] — (símbolo, etiqueta, stop nuevo), solo de
                    los tramos cuyo stop MEJORA. Un tramo que ya está por encima no aparece.
        """
        if not self.params.enabled or atr is None or atr <= 0:
            return []

        subidas: list[tuple[str, str, float]] = []
        for position in positions:
            if position.symbol != bar.symbol or position.shares <= 0:
                continue

            riesgo = position.entry_price - position.stop_price
            if riesgo <= 0:
                # Sin riesgo por acción no hay R que medir. Pasa cuando una posición se
                # abrió sin stop, y en ese caso lo que falta es el stop, no el trailing.
                continue

            beneficio = bar.high - position.entry_price

            candidatos = []
            if beneficio >= self.params.activate_at_r * riesgo:
                candidatos.append(bar.high - self.params.distance_for(position.tag) * atr)
            # El stop a la entrada NO depende del ATR ni de la distancia del tramo: una vez
            # ganado 1R, esta operación ya no puede acabar en pérdida. Va aparte del
            # trailing porque con un tramo de trailing ancho el stop seguido puede quedar
            # todavía por debajo de la entrada, que es justo lo que se quería evitar.
            if self.params.breakeven_at_r > 0 and (
                beneficio >= self.params.breakeven_at_r * riesgo
            ):
                candidatos.append(position.entry_price)
            if not candidatos:
                continue

            nuevo = max(candidatos)
            if nuevo <= position.stop_price:
                continue

            subidas.append((position.symbol, position.tag, nuevo))
        return subidas
