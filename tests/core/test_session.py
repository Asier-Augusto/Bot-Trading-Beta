"""Pruebas de las franjas horarias.

Bloquear entradas NO es cerrar posiciones. Son dos parámetros independientes a
propósito, y confundirlos dejaría posiciones atrapadas o cerraría de más.
"""

import datetime as dt

import pytest

from bot.core.session import SessionSchedule

HORARIO = SessionSchedule.from_dict(
    {
        "timezone": "America/New_York",
        "trading_windows": [{"from": "09:45", "to": "15:00", "allow_entries": True}],
        "danger_zones": [
            {"from": "09:30", "to": "09:45", "label": "apertura", "allow_entries": False},
            {"from": "15:00", "to": "16:00", "label": "pre-cierre", "allow_entries": False},
        ],
        "force_close": "15:45",
    }
)


def ny(hora: int, minuto: int) -> dt.datetime:
    import zoneinfo

    return dt.datetime(2026, 3, 10, hora, minuto, tzinfo=zoneinfo.ZoneInfo("America/New_York"))


@pytest.mark.parametrize(
    "hora, minuto, permitido",
    [
        (9, 35, False),   # apertura
        (9, 45, True),    # empieza la franja de entradas
        (14, 59, True),
        (15, 0, False),   # una hora antes del cierre
        (15, 44, False),
        (15, 46, False),
    ],
)
def test_las_entradas_solo_se_permiten_en_su_franja(hora, minuto, permitido):
    assert HORARIO.entries_allowed_at(ny(hora, minuto)) is permitido


def test_la_etiqueta_de_franja_viaja_para_el_informe():
    assert HORARIO.label_at(ny(9, 35)) == "apertura"
    assert HORARIO.label_at(ny(15, 30)) == "pre-cierre"
    assert HORARIO.label_at(ny(12, 0)) == "regular"


def test_el_cierre_forzado_es_a_su_hora_y_despues():
    assert HORARIO.is_force_close_time(ny(15, 44)) is False
    assert HORARIO.is_force_close_time(ny(15, 45)) is True
    assert HORARIO.is_force_close_time(ny(15, 50)) is True


def test_bloquear_entradas_no_es_cerrar_posiciones():
    # A las 15:30 no se abre, pero tampoco se cierra: las posiciones siguen vivas.
    momento = ny(15, 30)

    assert HORARIO.entries_allowed_at(momento) is False
    assert HORARIO.is_force_close_time(momento) is False
