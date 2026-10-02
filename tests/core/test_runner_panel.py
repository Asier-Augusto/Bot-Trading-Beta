"""Pruebas del cableado del panel de terminal en el bucle en vivo.

El modelo y el pintado ya están probados en `tests/reporting/`. Lo que se comprueba aquí es lo
que solo puede fallar al enchufarlo: el interruptor del perfil, la salida redirigida y que el
observador acabe realmente suscrito al bus.
"""

import contextlib

import pytest
from dataclasses import dataclass, field

from bot.core.runner import _build_panel, _is_a_terminal


@dataclass
class _Perfil:
    raw: dict = field(default_factory=dict)


class _Bus:
    def __init__(self):
        self.sinks = []

    def subscribe(self, sink):
        self.sinks.append(sink)


def _perfil(activado: bool):
    return _Perfil(raw={
        "dashboard": {"enabled": activado},
        "risk": {"max_daily_loss_pct": 0.02},
        "execution": {"starting_cash": 50_000.0},
    })


def test_sin_el_interruptor_del_perfil_no_hay_panel():
    resultado = _build_panel(_perfil(False), _Bus(), enabled=True)

    assert isinstance(resultado, contextlib.nullcontext)


def test_un_perfil_sin_bloque_dashboard_tampoco_lo_monta():
    """Es el caso del backtest: nadie está mirando una terminal."""
    resultado = _build_panel(_Perfil(raw={}), _Bus(), enabled=True)

    assert isinstance(resultado, contextlib.nullcontext)


def test_quien_llama_puede_apagarlo_aunque_el_perfil_lo_pida():
    """Con la salida redirigida a un fichero, el panel escribiría códigos de control y
    reposicionamientos del cursor en medio del log — que es como se corre un ensayo largo."""
    resultado = _build_panel(_perfil(True), _Bus(), enabled=False)

    assert isinstance(resultado, contextlib.nullcontext)


def test_el_panel_se_suscribe_al_bus_solo_al_entrar():
    """Suscribirlo antes dejaría un observador colgado del bus si el pintado falla al montar."""
    bus = _Bus()
    sesion = _build_panel(_perfil(True), bus, enabled=True)

    assert bus.sinks == []
    with sesion:
        assert bus.sinks == [sesion.dashboard]


def test_el_panel_toma_las_cifras_del_kill_switch_del_perfil():
    """Si tomara las de por defecto, el margen mostrado sería el de otro sistema."""
    with _build_panel(_perfil(True), _Bus(), enabled=True) as sesion:
        assert sesion.dashboard.model.kill_switch_limit == -1_000.0    # 2% de 50.000


def test_la_deteccion_de_terminal_no_revienta_con_la_salida_capturada():
    assert _is_a_terminal() in (True, False)


def test_una_salida_sin_isatty_no_se_considera_terminal(monkeypatch):
    """`sys.stdout` no siempre es un fichero: bajo pytest o en un servicio puede ser cualquier
    objeto. Suponer que tiene `isatty` haría fallar el arranque, no el panel."""
    import sys

    class _SinIsatty:
        pass

    monkeypatch.setattr(sys, "stdout", _SinIsatty())
    assert _is_a_terminal() is False


# --- F2: el arranque en vivo se planta si hay posiciones que el bot no conoce ----------


class _BrokerConDesajuste:
    def __init__(self, desajustes):
        self._desajustes = desajustes

    def reconcile(self):
        return self._desajustes


def test_arrancar_en_vivo_con_una_posicion_desconocida_se_planta():
    """Seguir seria peor que no arrancar: esas acciones no cuentan para `max_positions`, su
    stop no se vigila desde aqui, y la estrategia puede abrir otra encima del mismo simbolo."""
    from bot.core.runner import OpenPositionsMismatch, check_open_positions

    broker = _BrokerConDesajuste([{"symbol": "TSLA", "ibkr": 50.0, "bot": 0}])

    with pytest.raises(OpenPositionsMismatch, match="TSLA"):
        check_open_positions(broker, allow=False)


def test_se_puede_arrancar_a_sabiendas():
    """Una posicion a mano en la misma cuenta es legitima; lo que no vale es no enterarse."""
    from bot.core.runner import check_open_positions

    broker = _BrokerConDesajuste([{"symbol": "TSLA", "ibkr": 50.0, "bot": 0}])

    assert check_open_positions(broker, allow=True) == broker._desajustes


def test_sin_desajustes_arranca_sin_mas():
    from bot.core.runner import check_open_positions

    assert check_open_positions(_BrokerConDesajuste([]), allow=False) == []


def test_un_broker_que_no_sabe_reconciliar_no_bloquea_el_arranque():
    """El simulado no tiene con quien reconciliar: su contabilidad ES la verdad."""
    from bot.core.runner import check_open_positions

    assert check_open_positions(object(), allow=False) == []
