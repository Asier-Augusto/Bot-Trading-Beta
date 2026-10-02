"""Registro de implementaciones intercambiables.

Cada implementación se anuncia con un decorador y el perfil la selecciona por nombre.
Añadir la estrategia número veinte es crear un fichero y cambiar una línea de YAML.
"""

from __future__ import annotations

from typing import Callable, TypeVar

T = TypeVar("T", bound=type)

#: Familias registrables. La clave es el tipo; el valor, nombre -> clase.
_KINDS = (
    "strategy",
    "indicator",
    "sink",
    "risk_filter",
    "sizer",
    "broker",
    "feed",
    "history",
)
_REGISTRIES: dict[str, dict[str, type]] = {kind: {} for kind in _KINDS}


def _make_register(kind: str) -> Callable[[str], Callable[[T], T]]:
    """Fabrica el decorador de registro de una familia.

    kind   : str — familia, p. ej. "strategy".
    return : Callable — decorador que toma un nombre y devuelve el decorador real.
    """

    def register(name: str) -> Callable[[T], T]:
        def decorator(cls: T) -> T:
            if name in _REGISTRIES[kind]:
                raise ValueError(f"{kind} '{name}' ya registrado por {_REGISTRIES[kind][name]}")
            _REGISTRIES[kind][name] = cls
            return cls

        return decorator

    register.__name__ = f"register_{kind}"
    register.__qualname__ = f"register_{kind}"
    register.__doc__ = (
        f"""Registra una implementación de {kind} bajo un nombre.

        name   : str — nombre con el que el perfil seleccionará esta implementación.
        return : Callable — decorador que registra la clase y la devuelve sin modificarla.
        raises : ValueError — si el nombre ya está registrado por otra clase.
        """
    )
    return register


def _make_getter(kind: str) -> Callable[[str], type]:
    """Fabrica la función de búsqueda de una familia.

    kind   : str — familia.
    return : Callable — función que resuelve un nombre a su clase.
    """

    def get(name: str) -> type:
        try:
            return _REGISTRIES[kind][name]
        except KeyError:
            opciones = ", ".join(sorted(_REGISTRIES[kind])) or "(ninguna)"
            raise KeyError(f"{kind} '{name}' no registrado. Disponibles: {opciones}") from None

    get.__name__ = f"get_{kind}"
    get.__qualname__ = f"get_{kind}"
    get.__doc__ = (
        f"""Obtiene la clase registrada de {kind} por nombre.

        name   : str — nombre de la implementación de {kind}.
        return : type — la clase registrada para ese nombre.
        raises : KeyError — si el nombre no está registrado; incluye las opciones disponibles.
        """
    )
    return get


def available(kind: str) -> list[str]:
    """Lista los nombres registrados de una familia.

    kind   : str — familia, p. ej. "sink".
    return : list[str] — nombres ordenados alfabéticamente.
    """
    return sorted(_REGISTRIES[kind])


register_strategy = _make_register("strategy")
register_indicator = _make_register("indicator")
register_sink = _make_register("sink")
register_risk_filter = _make_register("risk_filter")
register_sizer = _make_register("sizer")
register_broker = _make_register("broker")
register_feed = _make_register("feed")
register_history = _make_register("history")

get_strategy = _make_getter("strategy")
get_indicator = _make_getter("indicator")
get_sink = _make_getter("sink")
get_risk_filter = _make_getter("risk_filter")
get_sizer = _make_getter("sizer")
get_broker = _make_getter("broker")
get_feed = _make_getter("feed")
get_history = _make_getter("history")
