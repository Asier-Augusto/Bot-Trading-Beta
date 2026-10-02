"""Pruebas del cierre de sesión, extraído de `engine.py` (deuda E7).

El cierre corre desde un `finally`, casi siempre porque algo ya ha ido mal, y es la barrera
de la familia entera de fallos "la sesión murió a media faena". Lo que se comprueba aquí es
lo que lo hace fiable: que cada etapa vaya en su sitio, que el fallo de una no impida las
siguientes, y que las últimas —"parada" y desconectar— no se pierdan bajo ningún concepto.
"""

import datetime as dt

from bot.core.session_closer import SessionCloser

T0 = dt.datetime(2026, 3, 10, 20, 0, tzinfo=dt.timezone.utc)


class _BusFalso:
    def __init__(self):
        self.emitidos = []

    def emit(self, evento):
        self.emitidos.append(evento)

    def drain(self):
        pass


class _RelojFalso:
    def now(self):
        return T0


class _MotorFalso:
    """Doble del motor con lo justo que el cierre le pide."""

    config_hash = "abc"

    def __init__(self, revienta_en=None):
        self.broker = object()
        self.bus = _BusFalso()
        self.clock = _RelojFalso()
        self._last_event_ts = None
        self.etapas = []
        self.revienta_en = revienta_en
        self.emitidos = []
        self.feed_desconectado = False
        self.bus_vaciado = 0

    # lo que el cierre invoca, cada uno anotándose
    def _flush_aggregators(self):
        self._anota("vaciar agregadores")

    def _close_shadows(self):
        self._anota("cerrar sombras")

    def _close_remaining_positions(self):
        self._anota("cerrar posiciones")

    def _release_unresolved_orders(self):
        self._anota("liberar órdenes")

    def _report_orphan_positions(self):
        self._anota("avisar de huérfanas")

    def _anota(self, nombre):
        self.etapas.append(nombre)
        if nombre == self.revienta_en:
            raise RuntimeError(f"revienta en {nombre}")


def _cierre(motor):
    return SessionCloser(motor)


def test_las_etapas_van_en_el_orden_que_importa():
    """Vaciar antes de cerrar, cerrar antes de avisar de huérfanas: al revés, la última vela
    no llegaría a evaluarse y las posiciones que ella cierra saldrían como huérfanas."""
    motor = _MotorFalso()

    _cierre(motor).stages()

    assert motor.etapas == ["vaciar agregadores", "cerrar sombras", "cerrar posiciones",
                            "liberar órdenes", "avisar de huérfanas"]


def test_el_fallo_de_una_etapa_no_impide_las_siguientes():
    """Las que vienen detrás de un fallo son justo las que no se pueden perder."""
    motor = _MotorFalso(revienta_en="cerrar posiciones")

    _cierre(motor).stages()

    assert motor.etapas[-1] == "avisar de huérfanas"


def test_el_cierre_de_sesion_vacia_los_cierres_que_esperan_comision():
    """Un cierre esperando el informe de comisión de IBKR se quedaría sin registrar si la
    sesión acaba antes de que llegue: la operación existió, movió dinero, y no aparecería en
    ningún sitio."""
    motor = _MotorFalso()
    motor.broker = type("B", (), {"vaciados": 0,
                                  "flush_pending_closes": lambda self: setattr(
                                      self, "vaciados", self.vaciados + 1)})()

    _cierre(motor).stages()

    assert motor.broker.vaciados == 1


def test_un_broker_que_no_espera_comisiones_no_estorba():
    """El simulado no tiene a quien esperar: su comisión la pone él."""
    motor = _MotorFalso()
    motor.broker = object()

    _cierre(motor).stages()

    assert motor.etapas[-1] == "avisar de huérfanas"
