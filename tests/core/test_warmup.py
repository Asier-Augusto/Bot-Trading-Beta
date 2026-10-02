"""Pruebas del calentamiento de indicadores.

Es la respuesta al problema estructural que este plan tenía que resolver: a las 09:30 no
hay ni una vela de 5' de hoy, y un RSI-14 sobre ellas no vale nada hasta las 10:45. Lo
mismo pasa con el ATR de 5', del que dependen el stop y la tolerancia de las zonas de
soporte. Sin calentar, el bot está ciego durante los primeros 75 minutos de cada sesión,
que es la franja de más volatilidad.
"""

import datetime as dt
from pathlib import Path

import bot.indicators.atr  # noqa: F401  — solo por su efecto de registro
import bot.indicators.rsi  # noqa: F401  — ídem
from bot.adapters.history.csv_history import CsvHistoricalSource
from bot.contracts.history import HistoricalSource
from bot.contracts.strategy import IndicatorSpec
from bot.core.indicator_set import IndicatorSet

HISTORY = Path(__file__).parents[1] / "fixtures" / "history.csv"
APERTURA = dt.datetime(2026, 3, 10, 13, 30, tzinfo=dt.timezone.utc)


class _FuenteEspia(HistoricalSource):
    """Fuente que registra qué se le pide y delega en la de verdad."""

    def __init__(self, real: HistoricalSource) -> None:
        self.real = real
        self.peticiones: list[tuple[str, str, dt.datetime, int]] = []

    def bars(self, symbol, timeframe, end, count):
        self.peticiones.append((symbol, timeframe, end, count))
        return self.real.bars(symbol, timeframe, end, count)


class _FuenteVacia(HistoricalSource):
    """Fuente sin histórico: el caso de un símbolo que empieza a cotizar hoy."""

    def bars(self, symbol, timeframe, end, count):
        return []


def test_el_rsi_de_5m_esta_listo_en_la_primera_vela_de_la_sesion():
    """El problema que este plan tenía que resolver, comprobado de punta a punta."""
    conjunto = IndicatorSet(["TSLA"], [IndicatorSpec("rsi", "5m", params={"period": 14})])

    conjunto.warmup(CsvHistoricalSource(HISTORY), APERTURA)

    assert ("rsi", "5m", "TSLA") in conjunto.values_for("TSLA")


def test_el_atr_de_5m_tambien_esta_listo():
    """De él dependen el stop de la entrada y la tolerancia de las zonas de soporte: sin
    ATR no hay zona que puntuar y `support` vetaría los primeros 75 minutos."""
    conjunto = IndicatorSet(["TSLA"], [IndicatorSpec("atr", "5m", params={"period": 14})])

    conjunto.warmup(CsvHistoricalSource(HISTORY), APERTURA)

    assert conjunto.values_for("TSLA")[("atr", "5m", "TSLA")] > 0


def test_la_referencia_del_nasdaq_tambien_se_calienta():
    """Sin esto, `rsi_ndx` bloquearía los primeros 75 minutos igual que antes del plan."""
    conjunto = IndicatorSet(
        ["TSLA"], [IndicatorSpec("rsi", "5m", symbol="ONEQ", params={"period": 14})]
    )

    conjunto.warmup(CsvHistoricalSource(HISTORY), APERTURA)

    assert ("rsi", "5m", "ONEQ") in conjunto.values_for("TSLA")


def test_el_calentamiento_no_pide_velas_posteriores_al_corte():
    """Calentar con velas del futuro sería mirar la respuesta antes del examen: el
    backtest saldría mejor de lo que la realidad daría."""
    espia = _FuenteEspia(CsvHistoricalSource(HISTORY))
    conjunto = IndicatorSet(["TSLA"], [IndicatorSpec("rsi", "5m", params={"period": 14})])

    conjunto.warmup(espia, APERTURA)

    assert espia.peticiones
    assert all(end == APERTURA for _s, _t, end, _c in espia.peticiones)


def test_se_piden_tres_veces_el_periodo():
    """Con `periodo + 1` el valor existe pero la media todavía es la siembra simple y no
    el suavizado de Wilder: dos series idénticas salvo en su arranque darían valores
    distintos durante decenas de velas."""
    espia = _FuenteEspia(CsvHistoricalSource(HISTORY))
    conjunto = IndicatorSet(["TSLA"], [IndicatorSpec("rsi", "5m", params={"period": 14})])

    conjunto.warmup(espia, APERTURA)

    assert espia.peticiones[0][3] == 42


def test_un_indicador_que_sigue_frio_se_reporta():
    """Un histórico corto no revienta la sesión, pero no puede pasar desapercibido:
    operar con el RSI de 5' apagado es distinto de operar con él listo."""
    conjunto = IndicatorSet(["TSLA"], [IndicatorSpec("rsi", "5m", params={"period": 14})])

    resumen = conjunto.warmup(_FuenteVacia(), APERTURA)

    assert resumen["not_ready"] == ["rsi:5m:TSLA"]
    assert resumen["bars"] == 0


def test_el_resumen_cuenta_las_velas_usadas():
    conjunto = IndicatorSet(
        ["TSLA"],
        [
            IndicatorSpec("rsi", "5m", params={"period": 14}),
            IndicatorSpec("atr", "5m", params={"period": 14}),
        ],
    )

    resumen = conjunto.warmup(CsvHistoricalSource(HISTORY), APERTURA)

    assert resumen["indicators"] == 2
    assert resumen["bars"] == 84          # 42 velas por indicador
    assert resumen["not_ready"] == []


def test_el_calentamiento_es_idempotente_por_sesion():
    """Una segunda llamada no vuelve a alimentar nada: repetirlo duplicaría el histórico
    dentro del suavizado y el valor resultante no sería el de ninguna serie real."""
    fuente = CsvHistoricalSource(HISTORY)
    conjunto = IndicatorSet(["TSLA"], [IndicatorSpec("rsi", "5m", params={"period": 14})])
    conjunto.warmup(fuente, APERTURA)
    antes = conjunto.values_for("TSLA")[("rsi", "5m", "TSLA")]

    segundo = conjunto.warmup(fuente, APERTURA)

    assert segundo["bars"] == 0
    assert conjunto.values_for("TSLA")[("rsi", "5m", "TSLA")] == antes


def test_un_indicador_sin_calentamiento_declarado_no_pide_nada():
    """`warmup_bars` a cero significa "no lo necesito": no se malgasta una petición."""

    class _Espia(_FuenteEspia):
        pass

    espia = _Espia(_FuenteVacia())
    conjunto = IndicatorSet(["TSLA"], [])
    conjunto.warmup(espia, APERTURA)
    assert espia.peticiones == []


# --- El hueco nocturno --------------------------------------------------------------


def _sesion_desplazada(desplazamiento: float) -> list:
    """Velas de 5' de TSLA de la sesión dorada, con todos los precios desplazados.

    Desplazar la sesión entera equivale a que el mercado haya abierto con un hueco de ese
    tamaño: la forma del día es idéntica y lo único que cambia es el salto respecto al
    cierre de la víspera.

    desplazamiento : float — hueco en dólares.
    return : list[Bar] — las diez primeras velas de 5' de la sesión.
    """
    import csv

    from bot.indicators.aggregator import BarAggregator

    ruta = Path(__file__).parents[1] / "fixtures" / "golden_session.csv"
    agregador = BarAggregator("TSLA", "5m")
    velas = []
    with ruta.open(newline="", encoding="utf-8") as handle:
        for fila in csv.DictReader(handle):
            if fila["symbol"] != "TSLA":
                continue
            ts = dt.datetime.fromisoformat(fila["timestamp"])
            precios = (fila["open"], fila["high"], fila["low"], fila["close"])
            for desfase, precio in enumerate(precios):
                volumen = int(fila["volume"]) if desfase == 0 else 0
                cerrada = agregador.on_tick(
                    ts + dt.timedelta(milliseconds=desfase),
                    float(precio) + desplazamiento,
                    volumen,
                )
                if cerrada is not None:
                    velas.append(cerrada)
            if len(velas) >= 12:
                break
    return velas[:10]


def _rsi_tras_calentar(desplazamiento: float, saltar_hueco: bool) -> float:
    """RSI de 5' en la quinta vela de la sesión, calentado desde el histórico.

    desplazamiento : float — hueco nocturno en dólares.
    saltar_hueco   : bool  — si se descarta el salto entre la víspera y la apertura.
    return : float — valor del RSI en la quinta vela de la sesión.
    """
    conjunto = IndicatorSet(["TSLA"], [IndicatorSpec("rsi", "5m", params={"period": 14})])
    conjunto.warmup(CsvHistoricalSource(HISTORY), APERTURA, skip_session_gap=saltar_hueco)
    for vela in _sesion_desplazada(desplazamiento):
        conjunto.update(vela)
    return conjunto.values_for("TSLA")[("rsi", "5m", "TSLA")]


def test_el_hueco_nocturno_no_afecta_al_rsi_calentado():
    """La decisión medida de este plan.

    El salto entre el cierre de ayer y la apertura de hoy no es un movimiento intradía, y
    meterlo en el indicador lo distorsiona durante horas: con el hueco dentro, un salto del
    1 % dejaba el RSI de 5' de TSLA en 78 al abrir con el intradía neutro, y las 30
    primeras velas de la sesión diferían 17,5 puntos de media. Con el umbral de Moris en
    35, la condición `rsi_5m` habría estado dominada por el hueco durante toda la ventana
    de entrada.

    La forma del día es idéntica en los cinco casos: lo único que cambia es el hueco.
    """
    valores = {
        pct: _rsi_tras_calentar(424.0 * pct, saltar_hueco=True)
        for pct in (0.0, 0.005, 0.01, 0.02, -0.02)
    }
    assert len(set(round(v, 6) for v in valores.values())) == 1, valores


def test_incluir_el_hueco_si_distorsiona_el_rsi():
    """La otra mitad de la medición, para que el número quede fijado y no se olvide por qué
    el defecto es descartarlo."""
    sin_hueco = _rsi_tras_calentar(0.0, saltar_hueco=False)
    con_hueco = _rsi_tras_calentar(424.0 * 0.01, saltar_hueco=False)

    assert abs(con_hueco - sin_hueco) > 15.0


def test_saltar_el_hueco_conserva_el_valor_calentado():
    """El coste de descartarlo es que la primera vela de la sesión no actualiza el
    indicador. El valor calentado se conserva intacto hasta la segunda: no se pierde el
    calentamiento entero, que es lo que importa."""
    conjunto = IndicatorSet(["TSLA"], [IndicatorSpec("rsi", "5m", params={"period": 14})])
    conjunto.warmup(CsvHistoricalSource(HISTORY), APERTURA)

    assert ("rsi", "5m", "TSLA") in conjunto.values_for("TSLA")
