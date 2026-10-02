"""Identificadores de correlación deterministas.

Con `uuid4`, la reproducibilidad del backtest estaba garantizada a nivel agregado —el
mismo P&L, el mismo número de operaciones— pero no campo a campo: dos corridas idénticas
producían identificadores distintos, así que no se podían casar sus operaciones una a una.

Eso importa concretamente para la variante mixta de Moris, cuyo valor entero está en
comparar el tramo agresivo con el conservador **sobre exactamente las mismas señales**. Si
los identificadores cambian entre corridas, la comparación hay que hacerla por posición en
la lista, y basta con que una corrida genere una operación de más para que todo se
desplace y la comparación mienta sin avisar.

Un identificador determinista es una función de lo que HACE única a la señal: qué símbolo,
en qué instante, con qué configuración y con qué estrategia. Dos corridas de los mismos
datos con la misma configuración producen los mismos identificadores; cambiar cualquiera de
los cuatro los cambia, que es justamente lo que se quiere — un backtest con otros umbrales
no debe parecer el mismo.
"""

from __future__ import annotations

import datetime as dt
import hashlib

#: Longitud del identificador. 16 caracteres hexadecimales son 64 bits: con las decenas
#: de miles de señales que produce un año de backtest, la probabilidad de colisión es
#: despreciable y la línea de log sigue siendo legible.
_LENGTH = 16


def _digest(*parts: str) -> str:
    """Resume una lista de partes en un identificador estable.

    Las partes se unen con un separador que no puede aparecer dentro de ninguna, para que
    ("ab", "c") y ("a", "bc") no colisionen.

    parts  : str  — componentes de la identidad, en orden fijo.
    return : str  — los `_LENGTH` primeros caracteres del SHA-256.
    """
    canonical = "\x1f".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_LENGTH]


def signal_id(symbol: str, ts: dt.datetime, config_hash: str, strategy: str) -> str:
    """Identificador de correlación de una evaluación de estrategia.

    symbol      : str      — ticker evaluado.
    ts          : datetime — instante de cierre de la vela evaluada.
    config_hash : str      — resumen de la configuración activa.
    strategy    : str      — nombre registrado de la estrategia.
    return      : str      — identificador de 16 caracteres, estable entre corridas.
    """
    return _digest("signal", symbol, ts.isoformat(), config_hash, strategy)


def order_id(signal: str, side: str, tag: str, ts: dt.datetime) -> str:
    """Identificador de una orden concreta.

    Incluye la etiqueta del tramo porque los dos tramos de la variante mixta nacen de la
    MISMA señal y son órdenes legítimamente distintas: sin la etiqueta compartirían
    identificador y el registro de órdenes no podría distinguirlas.

    signal : str      — identificador de la señal que la origina.
    side   : str      — sentido como texto, "BUY" o "SELL".
    tag    : str      — etiqueta del tramo.
    ts     : datetime — instante en que se decide la orden.
    return : str      — identificador de 16 caracteres, estable entre corridas.
    """
    return _digest("order", signal, side, tag, ts.isoformat())
