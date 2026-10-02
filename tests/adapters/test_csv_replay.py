"""Pruebas del feed de reproducción de históricos desde CSV."""

import datetime as dt

import pytest

from bot.adapters.data.csv_replay import CsvReplayFeed

CABECERA = "timestamp,symbol,open,high,low,close,volume\n"


def escribir(tmp_path, filas: str):
    ruta = tmp_path / "datos.csv"
    ruta.write_text(CABECERA + filas, encoding="utf-8")
    return ruta


def test_cada_vela_produce_cuatro_ticks_en_orden_ohlc(tmp_path):
    ruta = escribir(tmp_path, "2026-03-10T14:32:00+00:00,TSLA,410,413,409,412,1000\n")
    feed = CsvReplayFeed(ruta)
    feed.connect()
    feed.subscribe(["TSLA"])

    ticks = list(feed.stream())

    assert [t[2] for t in ticks] == [410.0, 413.0, 409.0, 412.0]
    assert all(t[0] == "TSLA" for t in ticks)


def test_el_volumen_se_reparte_y_no_se_inventa(tmp_path):
    ruta = escribir(tmp_path, "2026-03-10T14:32:00+00:00,TSLA,410,413,409,412,1000\n")
    feed = CsvReplayFeed(ruta)
    feed.connect()

    assert sum(t[3] for t in feed.stream()) == 1000


def test_los_ticks_salen_en_orden_cronologico_entre_simbolos(tmp_path):
    filas = (
        "2026-03-10T14:33:00+00:00,NVDA,800,801,799,800,10\n"
        "2026-03-10T14:32:00+00:00,TSLA,410,413,409,412,10\n"
    )
    feed = CsvReplayFeed(escribir(tmp_path, filas))
    feed.connect()

    ticks = list(feed.stream())

    assert [t[1] for t in ticks] == sorted(t[1] for t in ticks)
    assert ticks[0][0] == "TSLA"


def test_solo_se_emiten_los_simbolos_suscritos(tmp_path):
    filas = (
        "2026-03-10T14:32:00+00:00,TSLA,410,413,409,412,10\n"
        "2026-03-10T14:32:00+00:00,NVDA,800,801,799,800,10\n"
    )
    feed = CsvReplayFeed(escribir(tmp_path, filas))
    feed.connect()
    feed.subscribe(["TSLA"])

    assert {t[0] for t in feed.stream()} == {"TSLA"}


def test_sin_suscripcion_se_emite_todo(tmp_path):
    filas = (
        "2026-03-10T14:32:00+00:00,TSLA,410,413,409,412,10\n"
        "2026-03-10T14:32:00+00:00,NVDA,800,801,799,800,10\n"
    )
    feed = CsvReplayFeed(escribir(tmp_path, filas))
    feed.connect()

    assert {t[0] for t in feed.stream()} == {"TSLA", "NVDA"}


def test_fichero_inexistente_da_error_claro(tmp_path):
    feed = CsvReplayFeed(tmp_path / "no_existe.csv")

    with pytest.raises(FileNotFoundError, match="no_existe.csv"):
        feed.connect()


def test_las_marcas_sin_zona_se_interpretan_como_utc(tmp_path):
    ruta = escribir(tmp_path, "2026-03-10T14:32:00,TSLA,410,413,409,412,10\n")
    feed = CsvReplayFeed(ruta)
    feed.connect()

    primer_tick = next(iter(feed.stream()))

    assert primer_tick[1].tzinfo == dt.timezone.utc


def test_el_feed_esta_registrado():
    from bot.core.registry import get_feed

    assert get_feed("csv_replay") is CsvReplayFeed


def test_los_ticks_de_dos_simbolos_en_el_mismo_minuto_no_van_hacia_atras(tmp_path):
    # TSLA y NVDA comparten vela en el mismo minuto. Si el feed expandiera las velas
    # una a una en lugar de ordenar TODOS los ticks globalmente antes de emitir,
    # emitiría los cuatro ticks de un símbolo (.000 a .003) y luego el primero del
    # otro (.000), que retrocede en el tiempo. Con varios símbolos en la watchlist
    # (el perfil por defecto trae TSLA y NVDA) eso revienta SimulatedClock.advance_to,
    # que lanza ValueError ante cualquier retroceso.
    filas = (
        "2026-03-10T14:32:00+00:00,TSLA,410,413,409,412,8\n"
        "2026-03-10T14:32:00+00:00,NVDA,800,804,798,802,8\n"
    )
    feed = CsvReplayFeed(escribir(tmp_path, filas))
    feed.connect()

    instantes = [t[1] for t in feed.stream()]

    assert instantes == sorted(instantes)


# --- D-B5: reproducir a velocidad humana para poder MIRAR una sesion ------------------


def _feed(tmp_path, speed, sleep, hueco_horas=0):
    """Feed sobre dos velas separadas un minuto, o las horas que se pidan."""
    segunda = dt.datetime(2026, 3, 10, 14, 33, tzinfo=dt.timezone.utc) + dt.timedelta(
        hours=hueco_horas
    )
    filas = ("2026-03-10T14:32:00+00:00,TSLA,410,413,409,412,1000" + chr(10)
             + segunda.isoformat() + ",TSLA,412,414,411,413,1000" + chr(10))
    feed = CsvReplayFeed(escribir(tmp_path, filas), speed=speed, sleep=sleep)
    feed.connect()
    feed.subscribe(["TSLA"])
    return feed


def test_sin_velocidad_los_ticks_salen_de_corrido(tmp_path):
    """Es lo de siempre y lo que quiere el barrido: 2.011 sesiones no se miran, se cuentan."""
    esperas = []

    list(_feed(tmp_path, speed=None, sleep=esperas.append).stream())

    assert esperas == []


def test_con_velocidad_se_espera_lo_que_separa_a_dos_ticks(tmp_path):
    """A velocidad 60 un minuto de mercado pasa en un segundo, que es el ritmo al que una
    persona puede seguir lo que hace el bot sin que sea ni un borron ni una tarde."""
    esperas = []

    list(_feed(tmp_path, speed=60.0, sleep=esperas.append).stream())

    # Dos velas separadas un minuto: 60 s de mercado / 60 = 1 s de espera. Los tres ticks
    # dentro de cada vela van a milisegundos y no se esperan de forma apreciable.
    assert max(esperas) == pytest.approx(1.0, abs=0.01)


def test_un_hueco_largo_no_deja_la_reproduccion_colgada(tmp_path):
    """Entre el cierre y la apertura hay diecisiete horas. Esperarlas a escala haria que
    reproducir un dia tardara mas que el dia."""
    esperas = []

    list(_feed(tmp_path, speed=60.0, sleep=esperas.append, hueco_horas=17).stream())

    assert max(esperas) <= 2.0
