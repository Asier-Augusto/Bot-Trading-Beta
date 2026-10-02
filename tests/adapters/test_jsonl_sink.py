"""Pruebas del sink JSONL, incluida su resistencia a una caída a media escritura."""

import datetime as dt
import json

from bot.adapters.sinks.jsonl import TOPIC_BY_KIND, JsonlSink, read_jsonl
from bot.core.events import Fill, RiskBlocked, SignalEvaluated, SystemEvent
from bot.core.types import Side

TS = dt.datetime(2026, 3, 10, 14, 32, tzinfo=dt.timezone.utc)


def senal(level: str, total: float = 0.9) -> SignalEvaluated:
    return SignalEvaluated(
        ts=TS, signal_id="sig1", symbol="TSLA", strategy="moris",
        level=level, total=total, armed=level == "ARMED",
        conditions=({"name": "rsi_1m", "raw_value": 28.4, "score": 1.0},),
        blocking=() if level == "ARMED" else ("rsi_ndx",),
    )


def test_cada_evento_va_a_su_fichero_tematico(tmp_path):
    sink = JsonlSink(root=tmp_path)
    sink.handle(senal("ARMED"))
    sink.handle(Fill(ts=TS, order_id="o1", symbol="TSLA", side=Side.BUY, shares=10, price=400.0))
    sink.handle(RiskBlocked(ts=TS, symbol="TSLA", rule="kill_switch", reason="x"))
    sink.close()

    dia = tmp_path / "2026-03-10"
    assert (dia / "signals.jsonl").exists()
    assert (dia / "orders.jsonl").exists()
    assert (dia / "risk.jsonl").exists()


def test_se_escribe_una_linea_de_json_por_evento(tmp_path):
    sink = JsonlSink(root=tmp_path)
    sink.handle(senal("ARMED"))
    sink.handle(senal("NEAR_MISS"))
    sink.close()

    lineas = (tmp_path / "2026-03-10" / "signals.jsonl").read_text(encoding="utf-8").splitlines()

    assert len(lineas) == 2
    assert json.loads(lineas[0])["level"] == "ARMED"
    assert json.loads(lineas[0])["signal_id"] == "sig1"


def test_las_senales_por_debajo_del_nivel_minimo_no_se_escriben(tmp_path):
    sink = JsonlSink(root=tmp_path, min_signal_level="NEAR_MISS")
    sink.handle(senal("PARTIAL"))
    sink.handle(senal("IDLE"))
    sink.close()

    assert not (tmp_path / "2026-03-10" / "signals.jsonl").exists()


def test_el_nivel_minimo_es_configurable(tmp_path):
    sink = JsonlSink(root=tmp_path, min_signal_level="PARTIAL")
    sink.handle(senal("PARTIAL"))
    sink.close()

    assert (tmp_path / "2026-03-10" / "signals.jsonl").exists()


def test_el_filtro_de_nivel_no_afecta_a_otros_eventos(tmp_path):
    sink = JsonlSink(root=tmp_path, min_signal_level="ARMED")
    sink.handle(SystemEvent(ts=TS, level="INFO", message="arranque"))
    sink.close()

    assert (tmp_path / "2026-03-10" / "system.jsonl").exists()


def test_los_eventos_de_dias_distintos_van_a_carpetas_distintas(tmp_path):
    sink = JsonlSink(root=tmp_path)
    sink.handle(SystemEvent(ts=TS, level="INFO", message="a"))
    sink.handle(SystemEvent(ts=TS + dt.timedelta(days=1), level="INFO", message="b"))
    sink.close()

    assert (tmp_path / "2026-03-10" / "system.jsonl").exists()
    assert (tmp_path / "2026-03-11" / "system.jsonl").exists()


def test_una_ultima_linea_truncada_se_detecta_y_no_revienta(tmp_path):
    # Simula que el proceso murió a media escritura: lo peor posible con JSONL.
    sink = JsonlSink(root=tmp_path)
    sink.handle(SystemEvent(ts=TS, level="INFO", message="completa"))
    sink.close()

    ruta = tmp_path / "2026-03-10" / "system.jsonl"
    with ruta.open("a", encoding="utf-8") as handle:
        handle.write('{"ts": "2026-03-10T14:33:00+00:00", "mess')

    registros = list(read_jsonl(ruta))

    assert len(registros) == 1
    assert registros[0]["message"] == "completa"


def test_el_sink_esta_registrado():
    from bot.core.registry import get_sink

    assert get_sink("jsonl") is JsonlSink


def test_toda_clave_de_TOPIC_BY_KIND_corresponde_a_un_evento_real():
    """Un mapa que enruta eventos inexistentes se lee como cobertura que no está.

    Quien buscara `shadow.jsonl` no encontraría el fichero y no podría distinguir "no
    hubo operaciones en la sombra" de "nadie las escribe". Cinco de los veintiún tipos
    enrutados no existían en `bot.core.events`.
    """
    import bot.core.events as eventos

    for kind in TOPIC_BY_KIND:
        assert hasattr(eventos, kind), f"{kind} no es un evento de bot.core.events"


def test_cada_evento_se_vacia_al_disco_al_escribirlo(tmp_path):
    """Sin vaciado inmediato, el registro es invisible mientras la sesión corre y una caída
    se lleva el búfer entero.

    Medido: un ensayo de doce minutos contra TWS no escribió una sola línea; todo seguía en el
    búfer de Python. La promesa del módulo —"lo peor posible es una última línea
    incompleta"— es falsa mientras haya búfer: lo que se pierde son los últimos kilobytes, que
    con eventos críticos son las últimas órdenes y ejecuciones.
    """
    sink = JsonlSink(root=tmp_path, min_signal_level="IDLE")
    sink.handle(SystemEvent(ts=TS, level="INFO", message="arranque"))

    # Sin cerrar el sink: el fichero tiene que existir y tener contenido YA.
    ruta = tmp_path / TS.date().isoformat() / "system.jsonl"
    assert ruta.exists()
    assert ruta.read_text(encoding="utf-8").strip() != ""

    sink.close()
