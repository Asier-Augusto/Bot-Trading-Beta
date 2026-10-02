"""Pruebas del registro de módulos intercambiables."""

import pytest

from bot.core import registry


@pytest.fixture(autouse=True)
def registro_limpio():
    """Aísla cada prueba del estado global del registro."""
    original = {k: dict(v) for k, v in registry._REGISTRIES.items()}
    for familia in registry._REGISTRIES.values():
        familia.clear()
    yield
    for familia in registry._REGISTRIES.values():
        familia.clear()
    for k, v in original.items():
        registry._REGISTRIES[k].update(v)


def test_registrar_y_recuperar_una_estrategia():
    @registry.register_strategy("prueba")
    class Prueba:
        pass

    assert registry.get_strategy("prueba") is Prueba


def test_nombre_desconocido_da_error_util_con_las_opciones():
    @registry.register_sink("jsonl")
    class Jsonl:
        pass

    with pytest.raises(KeyError, match="jsonl"):
        registry.get_sink("parquet")


def test_registrar_dos_veces_el_mismo_nombre_es_un_error():
    @registry.register_indicator("rsi")
    class Uno:
        pass

    with pytest.raises(ValueError, match="ya registrado"):

        @registry.register_indicator("rsi")
        class Dos:
            pass


def test_listar_lo_disponible():
    @registry.register_sizer("percent_equity")
    class PorCiento:
        pass

    assert "percent_equity" in registry.available("sizer")


def test_funciones_publicas_tienen_docstring_y_nombre():
    """Verifica que cada función pública expone docstring y nombre correcto."""
    # Registradores
    assert registry.register_strategy.__name__ == "register_strategy"
    assert registry.register_strategy.__doc__ is not None
    assert "strategy" in registry.register_strategy.__doc__
    assert "ValueError" in registry.register_strategy.__doc__

    assert registry.register_indicator.__name__ == "register_indicator"
    assert registry.register_indicator.__doc__ is not None
    assert "indicator" in registry.register_indicator.__doc__

    # Getters
    assert registry.get_strategy.__name__ == "get_strategy"
    assert registry.get_strategy.__doc__ is not None
    assert "strategy" in registry.get_strategy.__doc__
    assert "KeyError" in registry.get_strategy.__doc__

    assert registry.get_sink.__name__ == "get_sink"
    assert registry.get_sink.__doc__ is not None
    assert "sink" in registry.get_sink.__doc__

    # Disponibles
    assert registry.available.__doc__ is not None
    assert "Disponibles" in registry.available.__doc__ or "lista" in registry.available.__doc__.lower()
