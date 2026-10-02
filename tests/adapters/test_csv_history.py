"""Pruebas de la fuente de históricos sobre CSV."""

import datetime as dt

import pytest

from bot.adapters.history.csv_history import CsvHistoricalSource, _ticks_de
from bot.indicators.aggregator import BarAggregator

TZ = dt.timezone.utc


def _t(hora: int, minuto: int = 0, dia: int = 10) -> dt.datetime:
    return dt.datetime(2026, 3, dia, hora, minuto, tzinfo=TZ)


def _escribir(tmp_path, filas: list[str], nombre: str = "history.csv"):
    cabecera = "timestamp,symbol,timeframe,open,high,low,close,volume"
    ruta = tmp_path / nombre
    ruta.write_text("\n".join([cabecera, *filas]) + "\n", encoding="utf-8")
    return ruta


def _minutos(tmp_path, cuantos: int, symbol: str = "TSLA", base: float = 400.0):
    """Escribe `cuantos` velas de 1' consecutivas a partir de las 10:00."""
    filas = []
    for i in range(cuantos):
        ts = _t(10, 0) + dt.timedelta(minutes=i + 1)  # ts es el CIERRE de la vela
        p = base + i
        filas.append(f"{ts.isoformat()},{symbol},1m,{p},{p + 1},{p - 1},{p + 0.5},1000")
    return _escribir(tmp_path, filas)


def test_devuelve_las_ultimas_n_velas_en_orden(tmp_path):
    fuente = CsvHistoricalSource(_minutos(tmp_path, 10))

    velas = fuente.bars("TSLA", "1m", _t(11), count=3)

    assert len(velas) == 3
    assert [v.close for v in velas] == [407.5, 408.5, 409.5]
    assert velas[0].ts < velas[1].ts < velas[2].ts


def test_ninguna_vela_devuelta_cierra_despues_del_corte(tmp_path):
    """Es la garantía que impide que el calentamiento se alimente del futuro.

    Sin ella, el backtest calentaría los indicadores con velas posteriores al instante
    que está simulando: mirar la respuesta antes del examen, y el resultado saldría mejor
    de lo que la realidad daría.
    """
    fuente = CsvHistoricalSource(_minutos(tmp_path, 10))
    corte = _t(10, 5)

    velas = fuente.bars("TSLA", "1m", corte, count=100)

    assert velas
    assert all(v.ts <= corte for v in velas)


def test_el_corte_incluye_la_vela_que_cierra_justo_en_el(tmp_path):
    """`end` es un corte cerrado: una vela que cierra exactamente ahí ya es pasado."""
    fuente = CsvHistoricalSource(_minutos(tmp_path, 10))
    velas = fuente.bars("TSLA", "1m", _t(10, 5), count=100)
    assert velas[-1].ts == _t(10, 5)


def test_pide_mas_velas_de_las_que_hay_y_devuelve_las_que_hay(tmp_path):
    """Un histórico corto es un hecho, no un error: el motor lo registra como aviso."""
    fuente = CsvHistoricalSource(_minutos(tmp_path, 3))
    assert len(fuente.bars("TSLA", "1m", _t(11), count=50)) == 3


def test_pedir_cero_velas_no_lee_nada(tmp_path):
    fuente = CsvHistoricalSource(tmp_path / "no_existe.csv")
    assert fuente.bars("TSLA", "1m", _t(11), count=0) == []


def test_un_simbolo_desconocido_devuelve_lista_vacia(tmp_path):
    fuente = CsvHistoricalSource(_minutos(tmp_path, 5))
    assert fuente.bars("NVDA", "1m", _t(11), count=5) == []


def test_un_marco_ausente_se_agrega_desde_el_mas_fino(tmp_path):
    """El fichero trae 1m; se piden 5m y salen agregadas."""
    fuente = CsvHistoricalSource(_minutos(tmp_path, 20))

    velas = fuente.bars("TSLA", "5m", _t(11), count=10)

    assert velas
    assert all(v.timeframe == "5m" for v in velas)
    assert all(v.ts.minute % 5 == 0 for v in velas)


def test_la_agregacion_usa_el_mismo_agregador_que_en_vivo(tmp_path):
    """Si la serie del calentamiento difiriera de la de la sesión, esa diferencia se
    colaría en el RSI sin que nada la denunciara."""
    fuente = CsvHistoricalSource(_minutos(tmp_path, 10))
    agregadas = fuente.bars("TSLA", "5m", _t(11), count=10)
    finas = fuente.bars("TSLA", "1m", _t(11), count=10)

    # La misma serie fina, por el mismo agregador, tiene que dar exactamente lo mismo.
    referencia = BarAggregator("TSLA", "5m")
    esperadas = []
    for fina in finas:
        for instante, precio, volumen in _ticks_de(fina):
            cerrada = referencia.on_tick(instante, precio, volumen)
            if cerrada is not None:
                esperadas.append(cerrada)
    if referencia.current is not None:
        esperadas.extend(referencia.on_clock(referencia.current.ts + dt.timedelta(microseconds=1)))

    assert agregadas == esperadas


def test_la_agregacion_conserva_maximo_minimo_y_volumen(tmp_path):
    """El máximo de la vela de 5' es el mayor de los cinco máximos, y su volumen la suma
    de los cinco: si el volumen se contara cuatro veces, la puntuación por volumen de las
    zonas de soporte quedaría inflada."""
    filas = []
    for i in range(5):
        ts = (_t(10, 0) + dt.timedelta(minutes=i + 1)).isoformat()
        filas.append(f"{ts},TSLA,1m,{100 + i},{110 + i},{90 + i},{105 + i},200")
    fuente = CsvHistoricalSource(_escribir(tmp_path, filas))

    vela = fuente.bars("TSLA", "5m", _t(11), count=1)[0]

    assert vela.high == 114.0
    assert vela.low == 90.0
    assert vela.volume == 1000


def test_un_marco_explicito_del_fichero_gana_sobre_la_agregacion(tmp_path):
    """Si el fichero trae 1d, no se agrega desde 1m: las velas diarias reales llevan la
    sesión entera, y agregar desde el intradía que haya en el fichero daría una serie
    truncada sin avisar."""
    filas = [
        f"{_t(21, 0, dia=9).isoformat()},TSLA,1d,400,410,395,405,1000000",
        f"{_t(21, 0, dia=10).isoformat()},TSLA,1d,405,415,400,412,1200000",
        f"{_t(10, 1).isoformat()},TSLA,1m,1,2,0.5,1.5,10",
    ]
    fuente = CsvHistoricalSource(_escribir(tmp_path, filas))

    velas = fuente.bars("TSLA", "1d", _t(23), count=5)

    assert len(velas) == 2
    assert velas[-1].close == 412.0


def test_dos_llamadas_iguales_no_releen_el_fichero(tmp_path):
    """Pedir 1d, 4h, 1h y 15m de dos símbolos son ocho consultas: sin caché serían ocho
    lecturas del fichero, y con 300 símbolos eso no escala."""
    ruta = _minutos(tmp_path, 10)
    fuente = CsvHistoricalSource(ruta)
    fuente.bars("TSLA", "1m", _t(11), count=5)

    ruta.unlink()  # si volviera a leer el fichero, reventaría

    assert len(fuente.bars("TSLA", "1m", _t(11), count=5)) == 5


def test_la_agregacion_tambien_se_cachea(tmp_path):
    fuente = CsvHistoricalSource(_minutos(tmp_path, 20))
    primera = fuente.bars("TSLA", "5m", _t(11), count=10)
    segunda = fuente.bars("TSLA", "5m", _t(11), count=10)
    assert primera == segunda


def test_el_csv_del_feed_se_rechaza_con_un_mensaje_claro(tmp_path):
    """Los dos ficheros se parecen y no son intercambiables: el del feed lleva el instante
    de APERTURA del minuto y este el de CIERRE de la vela. Confundirlos desplaza todo un
    periodo, y eso no puede fallar en silencio."""
    ruta = tmp_path / "feed.csv"
    ruta.write_text(
        "timestamp,symbol,open,high,low,close,volume\n"
        f"{_t(10, 0).isoformat()},TSLA,400,401,399,400.5,1000\n",
        encoding="utf-8",
    )
    with pytest.raises(KeyError, match="timeframe"):
        CsvHistoricalSource(ruta).bars("TSLA", "1m", _t(11), count=1)


def test_un_fichero_inexistente_falla_al_consultarlo(tmp_path):
    with pytest.raises(FileNotFoundError):
        CsvHistoricalSource(tmp_path / "no_existe.csv").bars("TSLA", "1m", _t(11), count=1)
