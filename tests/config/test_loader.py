"""Pruebas de la carga de perfiles y del hash de configuración."""

import pytest
import yaml

from bot.config.loader import compute_config_hash, load_profile, load_strategy_params

PERFIL = {
    "strategy": "moris",
    "broker": "simulated",
    "feed": "csv_replay",
    "sizer": "percent_equity",
    "sinks": ["jsonl"],
    "watchlist": ["TSLA", "NVDA"],
    "signals": {"min_log_level": "NEAR_MISS", "levels": {"near_miss": 0.8, "partial": 0.55}},
    "session": {
        "timezone": "America/New_York",
        "trading_windows": [{"from": "09:45", "to": "15:00", "allow_entries": True}],
        "danger_zones": [],
        "force_close": "15:45",
    },
}


def escribir(tmp_path, datos: dict, nombre: str = "backtest.yaml"):
    ruta = tmp_path / nombre
    ruta.write_text(yaml.safe_dump(datos, allow_unicode=True), encoding="utf-8")
    return ruta


def test_carga_las_implementaciones_seleccionadas(tmp_path):
    perfil = load_profile(escribir(tmp_path, PERFIL))

    assert perfil.strategy == "moris"
    assert perfil.broker == "simulated"
    assert perfil.sinks == ["jsonl"]
    assert perfil.watchlist == ["TSLA", "NVDA"]


def test_carga_las_franjas_horarias(tmp_path):
    perfil = load_profile(escribir(tmp_path, PERFIL))

    assert perfil.session.force_close.hour == 15
    assert perfil.session.force_close.minute == 45


def test_carga_los_umbrales_de_nivel(tmp_path):
    perfil = load_profile(escribir(tmp_path, PERFIL))

    assert perfil.levels.near_miss == 0.8
    assert perfil.levels.armed_requires_all is True


def test_armed_requires_all_no_se_puede_desactivar_por_descuido(tmp_path):
    # Desactivarlo cambia la ESTRATEGIA, no el registro. Debe ser explícito y ruidoso.
    datos = {**PERFIL, "signals": {**PERFIL["signals"], "levels": {"armed_requires_all": False}}}

    with pytest.raises(ValueError, match="armed_requires_all"):
        load_profile(escribir(tmp_path, datos))


@pytest.mark.parametrize("valor", [0, "false", "no", "off", "False", ""])
def test_armed_requires_all_tambien_avisa_con_valores_no_booleanos(tmp_path, valor):
    # `armed_requires_all: 0` o `armed_requires_all: "false"` expresan la misma
    # intención de desactivarlo que `False`; antes se ignoraban en silencio porque la
    # comprobación comparaba por identidad exacta con el booleano.
    datos = {
        **PERFIL,
        "signals": {**PERFIL["signals"], "levels": {"armed_requires_all": valor}},
    }

    with pytest.raises(ValueError, match="armed_requires_all"):
        load_profile(escribir(tmp_path, datos))


@pytest.mark.parametrize("valor", [True, 1, "true"])
def test_armed_requires_all_no_avisa_con_valores_que_no_intentan_desactivarlo(tmp_path, valor):
    # Un valor que no expresa intención de apagarlo no debe disparar el aviso: solo
    # las formas que un usuario escribiría para decir "no" merecen el ValueError.
    datos = {
        **PERFIL,
        "signals": {**PERFIL["signals"], "levels": {"armed_requires_all": valor}},
    }

    perfil = load_profile(escribir(tmp_path, datos))

    assert perfil.levels.armed_requires_all is True


def test_el_hash_de_configuracion_es_estable_e_independiente_del_orden():
    a = {"strategy": "moris", "watchlist": ["TSLA", "NVDA"]}
    b = {"watchlist": ["TSLA", "NVDA"], "strategy": "moris"}

    assert compute_config_hash(a) == compute_config_hash(b)


def test_el_hash_cambia_al_cambiar_un_parametro():
    a = {"strategy": "moris", "rsi_1m_threshold": 30}
    b = {"strategy": "moris", "rsi_1m_threshold": 35}

    assert compute_config_hash(a) != compute_config_hash(b)


def test_perfil_inexistente_da_error_claro(tmp_path):
    with pytest.raises(FileNotFoundError, match="no_existe.yaml"):
        load_profile(tmp_path / "no_existe.yaml")


def test_carga_los_parametros_de_la_estrategia(tmp_path):
    (tmp_path / "moris.yaml").write_text(
        yaml.safe_dump({"rsi_1m_threshold": 28.0}), encoding="utf-8"
    )

    assert load_strategy_params("moris", root=tmp_path)["rsi_1m_threshold"] == 28.0


def test_el_hash_tolera_claves_de_tipos_mezclados():
    """Un YAML con `30: alto` junto a `bajo: 10` no debe reventar el arranque.

    `json.dumps(sort_keys=True)` ordena comparando las claves entre sí, y en Python un
    `int` y un `str` no se pueden comparar. Fallaba de forma ruidosa, no silenciosa, pero
    fallaba con un `TypeError` que no decía nada útil.
    """
    assert compute_config_hash({"niveles": {30: "alto", "bajo": 10}})


def test_el_hash_sigue_siendo_independiente_del_orden_con_claves_mezcladas():
    a = compute_config_hash({"niveles": {30: "alto", "bajo": 10}})
    b = compute_config_hash({"niveles": {"bajo": 10, 30: "alto"}})
    assert a == b


def test_el_hash_tolera_claves_mezcladas_dentro_de_una_lista():
    """Las listas también se recorren: `observed_references` es una lista de diccionarios."""
    assert compute_config_hash({"refs": [{1: "a"}, {"b": 2}]})
