"""Pruebas del pintado con `rich`.

El modelo ya está probado en `test_dashboard.py`. Aquí solo se comprueba lo que el pintado
puede romper por su cuenta: que no se congele, que no reviente y que la falta de `rich` no
impida arrancar una sesión.
"""

import datetime as dt

import pytest

from bot.core.events import BarClosed, PositionClosed, PositionOpened
from bot.core.types import Bar
from bot.reporting.dashboard import Dashboard
from bot.reporting.live_panel import LivePanel, _Repainted, build_renderable, rich_available

T0 = dt.datetime(2026, 8, 27, 14, 0, tzinfo=dt.timezone.utc)

pytestmark = pytest.mark.skipif(not rich_available(), reason="rich no está instalado")


def _texto(renderable) -> str:
    from rich.console import Console

    consola = Console(width=200, record=True, file=open("nul" if __import__("os").name == "nt"
                                                        else "/dev/null", "w"))
    consola.print(renderable)
    salida = consola.export_text()
    consola.file.close()
    return salida


def _panel_con_datos():
    panel = Dashboard(starting_equity=100_000.0, max_daily_loss_pct=0.03)
    panel.handle(BarClosed(ts=T0, bar=Bar("TSLA", "1m", T0, 412.0, 413.0, 411.0, 412.50, 1000)))
    panel.handle(PositionOpened(ts=T0, symbol="TSLA", shares=24, entry_price=412.49,
                                stop_price=409.78, tag="aggressive"))
    return panel


def test_el_panel_pintado_lleva_precio_stock_y_margen():
    texto = _texto(build_renderable(_panel_con_datos().snapshot()))

    assert "TSLA" in texto
    assert "412.50" in texto
    assert "409.78" in texto
    assert "margen" in texto


def test_un_panel_vacio_se_pinta_sin_reventar():
    texto = _texto(build_renderable(Dashboard().snapshot()))

    assert "Posiciones abiertas" in texto


def test_el_kill_switch_cortado_se_ve():
    panel = Dashboard(starting_equity=100_000.0, max_daily_loss_pct=0.03)
    panel.handle(PositionClosed(ts=T0, symbol="TSLA", pnl=-3_100.0, tag="base"))

    assert "CORTADO" in _texto(build_renderable(panel.snapshot()))


def test_el_renderable_vuelve_a_leer_el_panel_en_cada_refresco():
    """`Live` repinta el MISMO objeto varias veces por segundo. Si se le entregara el grupo de
    tablas ya construido, el panel se quedaría congelado en el estado del arranque."""
    panel = _panel_con_datos()
    envoltorio = _Repainted(panel)
    antes = _texto(envoltorio)

    panel.handle(BarClosed(ts=T0, bar=Bar("TSLA", "1m", T0, 420.0, 421.0, 419.0, 420.00, 1000)))

    assert "412.50" in antes
    assert "420.00" in _texto(envoltorio)


def test_desactivado_no_pinta_en_vivo_pero_deja_el_resumen(capsys):
    panel = _panel_con_datos()

    with LivePanel(panel, enabled=False):
        pass

    assert "TSLA" in capsys.readouterr().out


def test_sin_rich_el_panel_no_impide_arrancar(monkeypatch):
    """Un bot que no opera porque falta una biblioteca de presentación sería mal cambio."""
    import bot.reporting.live_panel as modulo

    monkeypatch.setattr(modulo, "rich_available", lambda: False)
    panel = LivePanel(_panel_con_datos())

    assert panel.enabled is False
    with panel:
        pass
