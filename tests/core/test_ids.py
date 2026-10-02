"""Pruebas de los identificadores deterministas."""

import datetime as dt

from bot.core.ids import order_id, signal_id

TZ = dt.timezone.utc
T = dt.datetime(2026, 3, 10, 14, 30, tzinfo=TZ)


def test_la_misma_senal_da_el_mismo_identificador():
    """Es toda la razón de ser del módulo: dos corridas del mismo backtest producen
    operaciones que se pueden casar una a una."""
    assert signal_id("TSLA", T, "abc123", "moris") == signal_id("TSLA", T, "abc123", "moris")


def test_cambiar_el_simbolo_cambia_el_identificador():
    assert signal_id("TSLA", T, "abc123", "moris") != signal_id("NVDA", T, "abc123", "moris")


def test_cambiar_el_instante_cambia_el_identificador():
    otro = T + dt.timedelta(minutes=1)
    assert signal_id("TSLA", T, "abc123", "moris") != signal_id("TSLA", otro, "abc123", "moris")


def test_cambiar_la_configuracion_cambia_el_identificador():
    """Un backtest con otros umbrales no debe parecer el mismo."""
    assert signal_id("TSLA", T, "abc123", "moris") != signal_id("TSLA", T, "xyz789", "moris")


def test_cambiar_la_estrategia_cambia_el_identificador():
    assert signal_id("TSLA", T, "abc123", "moris") != signal_id("TSLA", T, "abc123", "otra")


def test_dos_tramos_de_la_misma_senal_dan_ordenes_distintas():
    """Los dos tramos de la variante mixta nacen de la misma señal y son órdenes
    legítimamente distintas: sin la etiqueta en la clave compartirían identificador."""
    senal = signal_id("TSLA", T, "abc123", "moris")
    a = order_id(senal, "BUY", "aggressive", T)
    b = order_id(senal, "BUY", "conservative", T)
    assert a != b


def test_la_compra_y_la_venta_de_un_tramo_dan_ordenes_distintas():
    senal = signal_id("TSLA", T, "abc123", "moris")
    assert order_id(senal, "BUY", "base", T) != order_id(senal, "SELL", "base", T)


def test_el_identificador_mide_dieciseis_caracteres_hexadecimales():
    identificador = signal_id("TSLA", T, "abc123", "moris")
    assert len(identificador) == 16
    assert all(c in "0123456789abcdef" for c in identificador)


def test_las_partes_no_se_pueden_confundir_entre_si():
    """("ab", "c") y ("a", "bc") tienen que dar identificadores distintos: el separador
    interno existe para eso."""
    assert signal_id("AB", T, "C", "moris") != signal_id("A", T, "BC", "moris")


def test_una_senal_y_una_orden_del_mismo_material_no_colisionan():
    """El prefijo de dominio evita que un signal_id valga por casualidad como order_id."""
    assert signal_id("TSLA", T, "x", "moris") != order_id("TSLA", T.isoformat(), "x", T)
