"""Pruebas del agregador de velas.

Las velas las construye el bot, no el bróker: así el proceso es idéntico en vivo y en
backtest, y una vela cierra aunque no llegue ningún tick.
"""

import datetime as dt

import pytest

from bot.indicators.aggregator import BarAggregator

TZ = dt.timezone.utc


def t(minuto: int, segundo: int = 0) -> dt.datetime:
    return dt.datetime(2026, 3, 10, 14, minuto, segundo, tzinfo=TZ)


def test_los_ticks_del_mismo_minuto_no_cierran_vela():
    agg = BarAggregator("TSLA", "1m")

    assert agg.on_tick(t(32, 5), 410.0, 100) is None
    assert agg.on_tick(t(32, 40), 412.0, 50) is None
    assert agg.current.close == 412.0


def test_un_tick_del_minuto_siguiente_cierra_la_vela_anterior():
    agg = BarAggregator("TSLA", "1m")
    agg.on_tick(t(32, 5), 410.0, 100)
    agg.on_tick(t(32, 40), 413.0, 50)
    agg.on_tick(t(32, 55), 409.0, 25)

    cerrada = agg.on_tick(t(33, 1), 411.0, 10)

    assert cerrada is not None
    assert cerrada.open == 410.0
    assert cerrada.high == 413.0
    assert cerrada.low == 409.0
    assert cerrada.close == 409.0
    assert cerrada.volume == 175
    # El instante de la vela es su CIERRE, alineado al límite del reloj de pared.
    assert cerrada.ts == t(33, 0)


def test_la_vela_cierra_por_reloj_sin_ticks():
    # `on_clock` devuelve una LISTA desde que rellena los periodos vacíos: aquí son la
    # vela real del minuto 32 y la de relleno del 33, que ya cerró también.
    agg = BarAggregator("TSLA", "1m")
    agg.on_tick(t(32, 5), 410.0, 100)

    cerradas = agg.on_clock(t(33, 30))

    assert cerradas
    assert cerradas[0].close == 410.0
    assert cerradas[0].volume == 100


def test_el_reloj_sin_vela_en_curso_no_inventa_nada():
    agg = BarAggregator("TSLA", "1m")

    assert agg.on_clock(t(33, 30)) == []


def test_agrega_a_cinco_minutos_alineando_al_limite():
    agg = BarAggregator("TSLA", "5m")
    agg.on_tick(t(32, 10), 410.0, 100)
    agg.on_tick(t(34, 50), 415.0, 100)

    cerrada = agg.on_tick(t(35, 5), 411.0, 10)

    assert cerrada is not None
    # 14:32 cae en el periodo 14:30-14:35, que cierra a las 14:35.
    assert cerrada.ts == t(35, 0)
    assert cerrada.high == 415.0


# --- D9: la vela cierra por reloj, aunque no haya operaciones ------------------------
# --- D6: el agregador rechaza un tick que retrocede ---------------------------------


def _t(hora: str) -> dt.datetime:
    h, m, s = (hora.split(":") + ["0"])[:3]
    return dt.datetime(2026, 3, 10, int(h), int(m), int(s), tzinfo=dt.timezone.utc)


def test_un_periodo_sin_ticks_produce_una_vela_de_relleno():
    """§5.1: "una vela sin operaciones arrastra el cierre anterior".

    Es la promesa que sostiene la elección de ONEQ como referencia del Nasdaq: la
    medición del §7.2 se hizo rellenando huecos como haría el agregador. Sin relleno, la
    serie de 5' de ONEQ —que no cotiza en la mitad de los minutos— sale comprimida y su
    RSI no es el que se midió.
    """
    agg = BarAggregator("ONEQ", "1m")
    agg.on_tick(_t("10:00:10"), 100.0, 10)

    velas = agg.on_clock(_t("10:03:05"))

    assert [v.ts.minute for v in velas] == [1, 2, 3]
    assert [v.close for v in velas] == [100.0, 100.0, 100.0]
    assert [v.volume for v in velas] == [10, 0, 0]


def test_una_vela_de_relleno_es_plana_al_cierre_anterior():
    agg = BarAggregator("ONEQ", "1m")
    agg.on_tick(_t("10:00:10"), 100.0, 10)
    agg.on_tick(_t("10:00:40"), 101.0, 10)

    velas = agg.on_clock(_t("10:02:05"))
    relleno = velas[1]

    assert (relleno.open, relleno.high, relleno.low, relleno.close) == (101.0, 101.0, 101.0, 101.0)
    assert relleno.volume == 0


def test_sin_ninguna_vela_previa_no_se_rellena_nada():
    """No hay cierre anterior que arrastrar: inventar un precio sería peor que callarse."""
    agg = BarAggregator("ONEQ", "1m")
    assert agg.on_clock(_t("10:05:00")) == []


def test_un_tick_tras_el_relleno_abre_la_vela_del_periodo_correcto():
    agg = BarAggregator("ONEQ", "1m")
    agg.on_tick(_t("10:00:10"), 100.0, 10)
    agg.on_clock(_t("10:03:05"))
    agg.on_tick(_t("10:03:30"), 105.0, 5)

    velas = agg.on_clock(_t("10:04:05"))

    assert len(velas) == 1
    assert velas[0].ts == _t("10:04:00")
    assert velas[0].open == 105.0


def test_un_hueco_enorme_no_fabrica_miles_de_velas():
    """Un fin de semana entre dos ticks no puede producir 4.000 velas planas: eso no es
    reconstruir la serie, es inventarla."""
    agg = BarAggregator("ONEQ", "1m")
    agg.on_tick(_t("10:00:10"), 100.0, 10)

    velas = agg.on_clock(_t("10:00:10") + dt.timedelta(days=3))

    assert len(velas) <= 1441


def test_el_agregador_rechaza_un_tick_que_va_hacia_atras():
    """RealClock no tiene `advance_to`: aquí desaparecía la única protección del sistema
    contra una secuencia de ticks fuera de orden."""
    agg = BarAggregator("TSLA", "1m")
    agg.on_tick(_t("10:00:10"), 100.0, 10)

    with pytest.raises(ValueError, match="hacia atrás"):
        agg.on_tick(_t("10:00:09"), 101.0, 10)


def test_el_agregador_rechaza_un_reloj_que_va_hacia_atras():
    agg = BarAggregator("TSLA", "1m")
    agg.on_tick(_t("10:00:10"), 100.0, 10)

    with pytest.raises(ValueError, match="hacia atrás"):
        agg.on_clock(_t("10:00:09"))


def test_dos_ticks_en_el_mismo_instante_se_admiten():
    """El feed de reproducción desplaza los cuatro ticks de una vela por milisegundos,
    pero dos símbolos distintos pueden coincidir. Igual no es retroceder."""
    agg = BarAggregator("TSLA", "1m")
    agg.on_tick(_t("10:00:10"), 100.0, 10)
    agg.on_tick(_t("10:00:10"), 101.0, 10)
    assert agg.current.close == 101.0
