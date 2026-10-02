"""Pruebas del panel de terminal.

Van contra el MODELO, no contra el pintado. Es la razón por la que están separados: una tabla
bonita que muestra el margen del kill switch mal calculado es peor que no tener panel, porque
invita a confiar en ella.
"""

import datetime as dt

import pytest

from bot.core.events import (
    BarClosed,
    PositionClosed,
    PositionOpened,
    SignalEvaluated,
    StopModified,
)
from bot.core.types import Bar
from bot.reporting.dashboard import Dashboard, render

TZ = dt.timezone.utc
T0 = dt.datetime(2026, 8, 27, 14, 0, tzinfo=TZ)


def _panel(equity=100_000.0, pct=0.03):
    return Dashboard(starting_equity=equity, max_daily_loss_pct=pct)


def _vela(cierre, symbol="TSLA", minuto=1):
    return Bar(symbol, "1m", T0 + dt.timedelta(minutes=minuto), cierre, cierre + 1,
               cierre - 1, cierre, 1000)


def _evaluacion(symbol="TSLA", total=0.88, nivel="NEAR_MISS", condiciones=None):
    return SignalEvaluated(
        ts=T0, symbol=symbol, level=nivel, total=total, armed=False,
        conditions=tuple(condiciones or [
            {"name": "support", "raw_value": 412.0, "score": 0.73, "passed": False},
            {"name": "rsi_1m", "raw_value": 28.4, "score": 1.0, "passed": True},
        ]),
    )


# --- Precio y condiciones ------------------------------------------------------------


def test_el_precio_se_actualiza_con_cada_vela():
    panel = _panel()

    panel.handle(BarClosed(ts=T0, bar=_vela(412.50)))

    assert panel.model.symbols["TSLA"].price == 412.50


def test_las_velas_que_no_son_de_un_minuto_no_mueven_el_precio():
    """El panel muestra el precio de la vela de decisión. Mezclar marcos haría que saltara
    entre el cierre de 1' y el de 5' sin que nadie entienda por qué."""
    panel = _panel()
    panel.handle(BarClosed(ts=T0, bar=_vela(412.50)))

    cinco = Bar("TSLA", "5m", T0, 400.0, 401.0, 399.0, 400.0, 5000)
    panel.handle(BarClosed(ts=T0, bar=cinco))

    assert panel.model.symbols["TSLA"].price == 412.50


def test_las_condiciones_se_muestran_con_su_valor_y_su_puntuacion():
    """§10.2: las cuatro condiciones con su valor, su puntuación y el total."""
    panel = _panel()

    panel.handle(_evaluacion())

    vista = panel.model.symbols["TSLA"]
    assert vista.total == 0.88
    assert {c.name for c in vista.conditions} == {"support", "rsi_1m"}
    assert vista.conditions[0].score == 0.73


def test_una_condicion_sin_dato_se_muestra_como_tal():
    """Pintar "nan" haría dudar de si el indicador está roto o simplemente calentando."""
    panel = _panel()
    panel.handle(_evaluacion(condiciones=[
        {"name": "rsi_ndx", "raw_value": float("nan"), "score": 0.0, "passed": False}
    ]))

    assert panel.model.symbols["TSLA"].conditions[0].display_value == "sin dato"


def test_una_evaluacion_no_borra_el_precio_ni_al_reves():
    panel = _panel()

    panel.handle(BarClosed(ts=T0, bar=_vela(412.50)))
    panel.handle(_evaluacion())
    panel.handle(BarClosed(ts=T0, bar=_vela(413.00, minuto=2)))

    vista = panel.model.symbols["TSLA"]
    assert vista.price == 413.00
    assert vista.total == 0.88


# --- Posiciones ----------------------------------------------------------------------


def test_una_apertura_aparece_con_su_stop():
    panel = _panel()

    panel.handle(PositionOpened(ts=T0, symbol="TSLA", shares=24, entry_price=412.49,
                                stop_price=409.78, tag="aggressive"))

    posicion = panel.model.positions[("TSLA", "aggressive")]
    assert posicion.shares == 24
    assert posicion.stop_price == 409.78


def test_los_dos_tramos_de_una_mixta_aparecen_por_separado():
    panel = _panel()

    panel.handle(PositionOpened(ts=T0, symbol="TSLA", shares=12, entry_price=412.49,
                                stop_price=409.78, tag="aggressive"))
    panel.handle(PositionOpened(ts=T0, symbol="TSLA", shares=12, entry_price=412.84,
                                stop_price=411.60, tag="conservative"))

    assert len(panel.model.positions) == 2


def test_el_no_realizado_se_recalcula_con_el_precio():
    panel = _panel()
    panel.handle(PositionOpened(ts=T0, symbol="TSLA", shares=10, entry_price=400.0,
                                stop_price=395.0, tag="base"))

    panel.handle(BarClosed(ts=T0, bar=_vela(405.0)))

    assert panel.model.positions[("TSLA", "base")].unrealized == pytest.approx(50.0)


def test_una_vela_de_otro_simbolo_no_mueve_el_no_realizado():
    panel = _panel()
    panel.handle(PositionOpened(ts=T0, symbol="TSLA", shares=10, entry_price=400.0,
                                stop_price=395.0, tag="base"))
    panel.handle(BarClosed(ts=T0, bar=_vela(405.0)))

    panel.handle(BarClosed(ts=T0, bar=_vela(999.0, symbol="NVDA", minuto=2)))

    assert panel.model.positions[("TSLA", "base")].unrealized == pytest.approx(50.0)


def test_un_cierre_quita_la_posicion_y_acumula_el_resultado():
    panel = _panel()
    panel.handle(PositionOpened(ts=T0, symbol="TSLA", shares=10, entry_price=400.0,
                                stop_price=395.0, tag="base"))

    panel.handle(PositionClosed(ts=T0, symbol="TSLA", pnl=125.0, tag="base"))

    assert panel.model.positions == {}
    assert panel.model.daily_pnl == 125.0
    assert panel.model.closed_trades == 1


def test_el_trailing_mueve_el_stop_mostrado():
    """Si el panel siguiera enseñando el stop de la entrada, mostraría un riesgo que ya no
    existe justo en la posición que más se mira."""
    panel = _panel()
    panel.handle(PositionOpened(ts=T0, symbol="TSLA", shares=10, entry_price=400.0,
                                stop_price=395.0, tag="base"))

    panel.handle(StopModified(ts=T0, symbol="TSLA", tag="base", old_stop=395.0,
                              new_stop=398.0, reason="trailing"))

    assert panel.model.positions[("TSLA", "base")].stop_price == 398.0


def test_una_modificacion_de_stop_de_una_posicion_que_no_esta_no_revienta():
    panel = _panel()

    panel.handle(StopModified(ts=T0, symbol="TSLA", tag="fantasma", old_stop=1.0,
                              new_stop=2.0, reason="trailing"))

    assert panel.model.positions == {}


# --- Kill switch ---------------------------------------------------------------------


def test_el_margen_del_kill_switch_es_lo_que_queda_por_perder():
    """Es el número que se mira cuando el día va mal, y por eso se calcula en vez de dejar que
    nadie lo haga de cabeza con dos cifras de la pantalla."""
    panel = _panel(equity=100_000.0, pct=0.03)     # corta a -3.000
    panel.handle(PositionClosed(ts=T0, symbol="TSLA", pnl=-1_000.0, tag="base"))

    assert panel.model.kill_switch_limit == pytest.approx(-3_000.0)
    assert panel.model.kill_switch_headroom == pytest.approx(2_000.0)


def test_el_margen_no_baja_de_cero():
    panel = _panel(equity=100_000.0, pct=0.03)
    panel.handle(PositionClosed(ts=T0, symbol="TSLA", pnl=-5_000.0, tag="base"))

    assert panel.model.kill_switch_headroom == 0.0


def test_se_ve_cuando_el_kill_switch_ya_ha_cortado():
    panel = _panel(equity=100_000.0, pct=0.03)

    panel.handle(PositionClosed(ts=T0, symbol="TSLA", pnl=-3_100.0, tag="base"))

    assert panel.model.kill_switch_tripped is True


def test_un_dia_en_positivo_tiene_todo_el_margen_y_algo_mas():
    panel = _panel(equity=100_000.0, pct=0.03)
    panel.handle(PositionClosed(ts=T0, symbol="TSLA", pnl=500.0, tag="base"))

    assert panel.model.kill_switch_headroom == pytest.approx(3_500.0)
    assert panel.model.kill_switch_tripped is False


# --- Pintado -------------------------------------------------------------------------


def test_el_panel_pintado_lleva_lo_esencial():
    panel = _panel()
    panel.handle(BarClosed(ts=T0, bar=_vela(412.50)))
    panel.handle(_evaluacion())
    panel.handle(PositionOpened(ts=T0, symbol="TSLA", shares=24, entry_price=412.49,
                                stop_price=409.78, tag="aggressive"))

    texto = render(panel.model)

    assert "TSLA" in texto
    assert "412.50" in texto
    assert "409.78" in texto
    assert "kill switch" in texto
    assert "support" in texto


def test_un_panel_vacio_se_pinta_sin_reventar():
    assert "sin posiciones abiertas" in render(_panel().model)


def test_el_panel_dice_cuando_el_kill_switch_corto():
    panel = _panel(equity=100_000.0, pct=0.03)
    panel.handle(PositionClosed(ts=T0, symbol="TSLA", pnl=-3_100.0, tag="base"))

    assert "CORTADO" in render(panel.model)


def test_el_panel_no_toca_disco():
    """Es un observador y solo lee estado en memoria (§10.2). Si tocara disco, un hipo de
    entrada/salida se metería en el hilo del bus."""
    panel = _panel()

    assert not hasattr(panel, "path")
    assert not hasattr(panel, "root")


def test_el_snapshot_no_se_mueve_cuando_el_bus_sigue_publicando():
    """El modelo se actualiza en el hilo del bus y se pinta desde otro. Sin copia, el pintado
    iteraría un diccionario que está cambiando y se caería justo cuando más pasa."""
    panel = _panel()
    panel.handle(PositionOpened(ts=T0, symbol="TSLA", shares=10, entry_price=400.0,
                                stop_price=395.0, tag="base"))

    copia = panel.snapshot()
    panel.handle(PositionClosed(ts=T0, symbol="TSLA", pnl=10.0, tag="base"))

    assert len(copia.positions) == 1
    assert panel.model.positions == {}


def test_el_snapshot_aguanta_que_el_bus_escriba_mientras_se_pinta():
    """Reproduce la carrera de verdad: un hilo publicando eventos mientras otro pinta."""
    import threading

    panel = _panel()
    parar = threading.Event()
    fallos = []

    def publicar():
        i = 0
        while not parar.is_set():
            i += 1
            panel.handle(BarClosed(ts=T0, bar=_vela(400.0 + i % 10, symbol=f"S{i % 50}")))

    def pintar():
        try:
            while not parar.is_set():
                render(panel.snapshot())
        except RuntimeError as error:      # "dictionary changed size during iteration"
            fallos.append(error)

    hilos = [threading.Thread(target=publicar), threading.Thread(target=pintar)]
    for hilo in hilos:
        hilo.start()
    parar.wait(0.5)
    parar.set()
    for hilo in hilos:
        hilo.join(2)

    assert fallos == []
