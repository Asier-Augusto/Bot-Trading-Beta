"""Contrato del bróker: envío de órdenes, estado de la cuenta y cierre de posiciones.

El contrato tiene dos niveles a propósito.

`Broker` reúne lo que **todo** bróker tiene que saber hacer, incluido lo que solo se
usa en los caminos más críticos: cerrar todas las posiciones a una hora y modificar el
stop de una que ya está viva. Antes el motor los invocaba con `hasattr`, lo cual
funcionaba mientras solo existiera el bróker simulado; con un adaptador de IBKR,
olvidar `close_all` convertía el cierre forzado de las 15:45 en una guarda que falla en
silencio, con dinero real. Aquí eso es un `TypeError` al construir el objeto.

`BarDrivenBroker` añade `on_bar` y es el nivel del **backtest**: un bróker que avanza
cuando avanza la vela. IBKR no funciona así — sus ejecuciones llegan de forma asíncrona
— y por eso `on_bar` no está en el contrato general. La vía equivalente para un bróker
asíncrono es `drain_events`, que el motor consulta siempre.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from bot.core.types import Bar, Order

if TYPE_CHECKING:
    from bot.core.context import AccountSnapshot, Position
    from bot.core.events import Event


class Broker(ABC):
    """Ejecuta órdenes, informa del estado de la cuenta y cierra posiciones.

    Está separado del feed de datos a propósito: eso permite combinar históricos de
    fichero con bróker simulado para backtest, y datos de IBKR con órdenes de IBKR para
    paper, sin que el resto del sistema se entere.

    Patrón común a toda la capa de ejecución: los métodos que producen hechos los
    **devuelven** en vez de publicarlos. El bróker no conoce el bus. Es el motor quien
    publica y quien actúa sobre ellos — por ejemplo, liberando la clave anti-duplicado
    de una orden que acaba de resolverse.
    """

    @abstractmethod
    def submit(self, order: Order) -> str:
        """Envía una orden.

        order  : Order — orden ya dimensionada y aprobada por el riesgo.
        return : str   — identificador de la orden en el bróker.
        """

    @abstractmethod
    def cancel(self, order_id: str) -> list["Event"]:
        """Cancela una orden viva.

        Devuelve los eventos producidos en vez de publicarlos: el bróker no conoce el
        bus, así que el patrón de toda la capa de ejecución es devolver los hechos
        para que el motor los publique y actúe sobre ellos. Aquí no es cosmético: sin
        un `OrderCancelled` que llegue al motor, la clave anti-duplicado de la orden
        cancelada nunca se liberaría.

        Un bróker asíncrono (IBKR) devolverá `[]` y entregará el `OrderCancelled` más
        tarde por `drain_events`, cuando el mercado confirme. Con eso desaparece la
        ambigüedad de que `[]` significara a la vez "no tenía esa orden" y "te confirmo
        luego": lo primero se sabe porque nunca llega confirmación.

        order_id : str — identificador devuelto por `submit`.
        return   : list[Event] — normalmente un `OrderCancelled`; vacía si el bróker
                                 no tenía viva esa orden o si confirmará después.
        """

    @abstractmethod
    def positions(self) -> list["Position"]:
        """Posiciones abiertas.

        return : list[Position] — una entrada por posición viva. Con la variante mixta
                                  de Moris puede haber varias del mismo símbolo, una
                                  por tramo, distinguidas por su etiqueta.
        """

    @abstractmethod
    def account(self) -> "AccountSnapshot":
        """Foto del estado de la cuenta.

        return : AccountSnapshot — efectivo, patrimonio y poder de compra.
        """

    @abstractmethod
    def close_all(self, bars: dict[str, Bar], reason: str) -> list["Event"]:
        """Cierra todas las posiciones abiertas, cada una a la vela de su símbolo.

        Es el camino del cierre forzado de la sesión y del fin de datos. Está en el
        contrato, y no como método opcional, porque es el más crítico de todos: un
        bróker que no sepa cerrar por hora deja posiciones abiertas de un día para
        otro. Que falte tiene que reventar al construir el objeto, no a las 15:45.

        bars   : dict[str, Bar] — última vela conocida de cada símbolo, por ticker. Una
                                  posición sin vela en el mapa no se puede liquidar a un
                                  precio honesto y queda abierta.
        reason : str            — motivo, que viaja a `PositionClosed.exit_reason`.
        return : list[Event]    — un `PositionClosed` por posición liquidada.
        """

    @abstractmethod
    def modify_stop(self, symbol: str, tag: str, new_stop: float) -> list["Event"]:
        """Mueve el stop protector de una posición viva.

        Es el mecanismo del trailing (§7.3 del diseño): el stop vive en el bróker como
        orden real, de modo que la posición sigue protegida aunque el proceso muera, y
        el bot lo que hace es **modificarlo**, no sustituirlo por lógica propia.

        Una implementación nunca debe aceptar un stop más bajo que el actual: un
        trailing que retrocede amplía el riesgo de una posición que ya va a favor.

        symbol   : str   — ticker.
        tag      : str   — etiqueta del tramo cuya orden se modifica.
        new_stop : float — precio nuevo del stop.
        return   : list[Event] — un `StopModified` si se movió; vacía si no había
                                 posición con esa etiqueta o si el stop no mejoraba.
        """

    def drain_events(self) -> list["Event"]:
        """Recoge los hechos que el bróker ha acumulado desde la última llamada.

        Es la vía formalizada para un bróker **asíncrono**: en IBKR las ejecuciones, los
        rechazos y las confirmaciones de cancelación llegan por devolución de llamada,
        en un hilo que no es el del motor. El adaptador las encola y el motor las recoge
        aquí, en su propio hilo, por el mismo camino que cualquier otro evento.

        El defecto devuelve la lista vacía para que un bróker síncrono como el simulado
        no tenga que implementarla y el motor pueda llamarla siempre sin comprobar nada.

        return : list[Event] — hechos pendientes, en orden de llegada. Vacía por defecto.
        """
        return []


class BarDrivenBroker(Broker):
    """Bróker que avanza cuando avanza la vela. Es el modelo del backtest.

    Separado de `Broker` porque en vivo nadie "avanza" el bróker: los hechos llegan
    solos. Un adaptador de IBKR implementa `Broker` y entrega por `drain_events`; el
    simulado implementa esto y entrega por `on_bar`. El motor distingue con `isinstance`
    y usa las dos vías, así que un bróker que hiciera las dos cosas también encajaría.
    """

    @abstractmethod
    def on_bar(self, bar: Bar) -> list["Event"]:
        """Avanza el bróker una vela y devuelve lo que haya producido.

        bar    : Bar — vela recién cerrada.
        return : list[Event] — ejecuciones, cierres y rechazos provocados por esa vela.
        """

    def settle(self, bar: Bar) -> list["Event"]:
        """Ejecuta las órdenes que se resuelven DENTRO de la vela que las produjo.

        El motor la llama después de que la estrategia haya decidido sobre esa vela. No es
        abstracta a propósito: un bróker que solo sepa ejecutar en la apertura de la vela
        siguiente devuelve la lista vacía y sigue siendo un bróker correcto. Ver `Timing`
        en `bot.core.types` para qué órdenes caen aquí y por qué ninguna mira el futuro.

        bar    : Bar — vela recién cerrada.
        return : list[Event] — vacía si el bróker no modela ejecución en la misma vela.
        """
        return []
