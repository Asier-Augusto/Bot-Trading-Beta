"""Registro de las órdenes enviadas y aún sin resolver.

Existe para una sola cosa, y es la que más caro sale hacer mal: saber QUÉ clave
anti-duplicado hay que liberar cuando una orden concreta termina su ciclo de vida.

El `DuplicateOrderFilter` bloquea una intención si ya hay una orden pendiente con su misma
clave (símbolo, sentido, etiqueta). Esa guarda es necesaria —una venta reenviada antes de
que llegue la confirmación de la primera crearía un corto accidental— y es una trampa si la
clave no se libera: un simple rechazo del bróker dejaría esa posición sin poder cerrarse
jamás. De ahí que aquí se libere la clave EXACTA de la orden que se resuelve, y nunca
todas a la vez con un `clear()`: eso soltaría también las de otras órdenes vivas, por
ejemplo el otro tramo de una variante mixta.

En el Plan 3 este registro crece: con IBKR las órdenes viven más tiempo, se aceptan antes de
ejecutarse y pueden resolverse minutos después por un camino asíncrono.
"""

from __future__ import annotations

from typing import Callable

from bot.core.types import Order

#: Tipos de evento cuya llegada resuelve el ciclo de vida de una orden concreta. Todos
#: llevan `order_id`. Es la resolución de la ORDEN lo que libera la clave anti-duplicado,
#: nunca el cierre de la posición: una venta rechazada por el bróker no cierra ninguna
#: posición, y sin esta liberación esa posición no podría cerrarse jamás.
ORDER_RESOLVING_KINDS = frozenset({"Fill", "OrderRejected", "OrderCancelled"})


class OrderBook:
    """Las órdenes enviadas que todavía no han terminado su ciclo de vida."""

    def __init__(self, release: Callable[[Order], None]) -> None:
        """Crea el registro.

        release : Callable[[Order], None] — qué hacer cuando una orden se resuelve.
                  Normalmente `RiskManager.release_order`. Se inyecta en vez de recibir el
                  gestor entero porque esto no necesita saber nada más de él.
        """
        self._orders: dict[str, Order] = {}
        self._release = release

    def __len__(self) -> int:
        """Cuántas órdenes siguen sin resolverse.

        return : int
        """
        return len(self._orders)

    def __contains__(self, order_id: object) -> bool:
        """Indica si una orden sigue sin resolverse.

        order_id : object — identificador de orden.
        return   : bool
        """
        return order_id in self._orders

    def track(self, order: Order) -> None:
        """Registra una orden que se va a enviar.

        Se llama ANTES de `Broker.submit`, no después. Un bróker que rechace de forma
        síncrona lanzando desde `submit` dejaría si no la clave anti-duplicado puesta por el
        gestor de riesgo y la orden sin registrar aquí: invisible incluso para el paracaídas
        del cierre de sesión, y con ella ninguna orden equivalente volvería a pasar el
        filtro en toda la sesión.

        order  : Order — orden aprobada por el riesgo.
        return : None
        """
        self._orders[order.order_id] = order

    def resolves(self, kind: str) -> bool:
        """Indica si un tipo de evento cierra el ciclo de vida de una orden.

        kind   : str  — nombre de la clase del evento.
        return : bool
        """
        return kind in ORDER_RESOLVING_KINDS

    def resolve(self, order_id: str) -> Order | None:
        """Libera la clave anti-duplicado de la orden que acaba de resolverse.

        Libera SOLO su clave, nunca todas las pendientes — eso es justo lo que distingue
        este mecanismo de un `clear()` indiscriminado.

        order_id : str — identificador de la orden resuelta.
        return   : Order | None — la orden, o None si no estaba registrada (por ejemplo, un
                   evento de una orden que ya se había resuelto antes).
        """
        order = self._orders.pop(order_id, None)
        if order is not None:
            self._release(order)
        return order

    def release_all(self) -> list[Order]:
        """Libera las claves de todo lo que quede vivo, al cerrar la sesión.

        Es el paracaídas de las tres vías de liberación (ejecución, rechazo y cancelación):
        pase lo que pase durante la sesión —una orden enviada en la última vela, que ya no
        tiene vela siguiente en la que ejecutarse; un bróker que no confirma— al terminar no
        queda ninguna clave colgada. Importa porque ese estado sobrevive al motor: un runner
        que reutilice el mismo `RiskState` entre sesiones arrancaría el día siguiente
        bloqueando órdenes por culpa de una clave de ayer.

        return : list[Order] — las que quedaban sin resolver, para poder registrarlas.
        """
        pendientes = list(self._orders.values())
        self._orders.clear()
        for order in pendientes:
            self._release(order)
        return pendientes
