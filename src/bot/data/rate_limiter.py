"""Límite de ritmo para las peticiones que un servicio ajeno cuenta.

IBKR limita las peticiones de histórico al orden de 60 cada 10 minutos (§6.4 del diseño), y
pasarse no devuelve un error educado: corta. Dos símbolos por cuatro marcos son ocho
peticiones y no pasa nada; 300 símbolos por cuatro marcos son 1.200, y ahí esto es la
diferencia entre arrancar y no arrancar.

**El reloj se inyecta**, como en todo el sistema. No es una formalidad: con un reloj simulado,
las pruebas comprueban que la espera es la correcta sin dormir de verdad, que es la única
forma de probar un limitador sin que la batería tarde minutos.

**La ventana es deslizante, no por bloques.** Con bloques, 60 peticiones al final de un bloque
y 60 al principio del siguiente son 120 en un instante — que es exactamente lo que el límite
existe para impedir.
"""

from __future__ import annotations

import collections
import datetime as dt


class RateLimiter:
    """Deja pasar como mucho N llamadas por ventana de tiempo, esperando lo que haga falta."""

    def __init__(self, max_calls: int, per_seconds: float, clock) -> None:
        """Crea el limitador.

        max_calls   : int   — llamadas permitidas por ventana. Debe ser positivo.
        per_seconds : float — duración de la ventana, en segundos. Debe ser positiva.
        clock       : Clock — reloj inyectado. De él salen la hora y la espera.
        raises      : ValueError — si el límite o la ventana no son positivos.
        """
        if max_calls <= 0 or per_seconds <= 0:
            raise ValueError("el límite y la ventana deben ser positivos")
        self.max_calls = max_calls
        self.per_seconds = per_seconds
        self.clock = clock
        #: Instantes de las llamadas que siguen dentro de la ventana, en orden.
        self._calls: collections.deque[dt.datetime] = collections.deque()

    def _prune(self, now: dt.datetime) -> None:
        """Descarta las llamadas que ya han salido de la ventana.

        now    : datetime — instante actual.
        return : None
        """
        limite = now - dt.timedelta(seconds=self.per_seconds)
        while self._calls and self._calls[0] <= limite:
            self._calls.popleft()

    def would_wait(self) -> float:
        """Cuánto habría que esperar para poder llamar ahora mismo.

        NO consume una ficha. Sirve para decidir en qué orden pedir varias cosas, no para
        pedirlas: si contara como petición, preguntar saldría igual de caro que hacer.

        return : float — segundos de espera; 0.0 si se puede llamar ya.
        """
        now = self.clock.now()
        self._prune(now)
        if len(self._calls) < self.max_calls:
            return 0.0
        libre = self._calls[0] + dt.timedelta(seconds=self.per_seconds)
        return max(0.0, (libre - now).total_seconds())

    def acquire(self) -> float:
        """Espera lo necesario y anota una llamada.

        La espera se le pide al reloj inyectado, no a `time.sleep`: con un reloj simulado no
        bloquea y la prueba sigue comprobando el número exacto.

        return : float — segundos que hubo que esperar; 0.0 si no hubo que esperar.
        """
        espera = self.would_wait()
        if espera > 0:
            self.clock.sleep(espera)

        self._prune(self.clock.now())
        self._calls.append(self.clock.now())
        return espera
