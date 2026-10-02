"""Pruebas del conjunto de indicadores.

Dos fallos latentes del Plan 1 vivían en este trozo de código, y los dos se vuelven
reales en cuanto hay más de un indicador del mismo nombre o más de un símbolo:

- la clave no incluía los parámetros, así que un RSI-14 y un RSI-7 en el mismo marco
  colapsaban en una sola instancia, en silencio;
- el filtro por símbolo ignoraba su argumento y devolvía los indicadores de todos.
"""

import datetime as dt

import pytest

import bot.indicators.atr  # noqa: F401  — solo por su efecto de registro
import bot.indicators.rsi  # noqa: F401  — ídem
from bot.contracts.strategy import IndicatorSpec
from bot.core.indicator_set import IndicatorSet
from bot.core.types import Bar

TZ = dt.timezone.utc


def _vela(symbol: str, timeframe: str, close: float, minuto: int = 0) -> Bar:
    return Bar(
        symbol=symbol,
        timeframe=timeframe,
        ts=dt.datetime(2026, 3, 10, 10, minuto, tzinfo=TZ),
        open=close,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=1000,
    )


def test_dos_periodos_distintos_producen_dos_instancias():
    """Un RSI-14 y un RSI-7 a 5m no son el mismo indicador.

    Antes la clave era (nombre, marco, símbolo) y los dos colapsaban en uno solo, en
    silencio: la estrategia leía el valor del que se hubiera construido primero. Como
    ambos se piden con la misma clave pública, el conjunto no puede elegir: falla al
    construirse y dice cuáles chocan.
    """
    specs = [
        IndicatorSpec("rsi", "5m", params={"period": 14}),
        IndicatorSpec("rsi", "5m", params={"period": 7}),
    ]
    with pytest.raises(ValueError, match="rsi"):
        IndicatorSet(["TSLA"], specs)


def test_dos_periodos_distintos_conviven_si_sus_marcos_difieren():
    """La colisión es de clave pública, no de nombre: 1m y 5m son claves distintas."""
    conjunto = IndicatorSet(
        ["TSLA"],
        [
            IndicatorSpec("rsi", "1m", params={"period": 14}),
            IndicatorSpec("rsi", "5m", params={"period": 7}),
        ],
    )
    assert len(conjunto) == 2


def test_values_for_no_devuelve_indicadores_de_otro_simbolo():
    """El argumento `symbol` no era decorativo: filtraba de verdad, o debía hacerlo.

    Inerte mientras `ctx.indicator()` indexe siempre por símbolo, pero es un arma cargada
    para cualquier cosa que itere `ctx.indicators` — y la extracción de características
    para el aprendizaje automático es literalmente eso.
    """
    conjunto = IndicatorSet(
        ["TSLA", "NVDA"], [IndicatorSpec("rsi", "1m", params={"period": 2})]
    )
    for indice, cierre in enumerate((10.0, 11.0, 12.0, 13.0)):
        conjunto.update(_vela("TSLA", "1m", cierre, indice))

    assert conjunto.values_for("TSLA") != {}
    assert conjunto.values_for("NVDA") == {}


def test_una_referencia_explicita_se_ve_desde_cualquier_simbolo():
    """ONEQ es la referencia del Nasdaq: TSLA y NVDA la leen las dos.

    El filtro de `values_for` no puede tirar los indicadores con símbolo explícito, o la
    condición `rsi_ndx` dejaría de poder cumplirse — que es justo el fallo que el Plan 1
    corrigió en `subscribed_symbols`.
    """
    conjunto = IndicatorSet(
        ["TSLA", "NVDA"],
        [IndicatorSpec("rsi", "5m", symbol="ONEQ", params={"period": 2})],
    )
    for indice, cierre in enumerate((10.0, 11.0, 12.0, 13.0)):
        conjunto.update(_vela("ONEQ", "5m", cierre, indice))

    clave = ("rsi", "5m", "ONEQ")
    assert clave in conjunto.values_for("TSLA")
    assert clave in conjunto.values_for("NVDA")


def test_un_indicador_sin_valor_no_aparece_en_el_contexto():
    """Durante el calentamiento no hay valor. Publicar un None obligaría a cada
    estrategia a distinguir "no lo pedí" de "aún no vale", que es el mismo error."""
    conjunto = IndicatorSet(["TSLA"], [IndicatorSpec("rsi", "1m", params={"period": 14})])
    conjunto.update(_vela("TSLA", "1m", 10.0))
    assert conjunto.values_for("TSLA") == {}


def test_cada_simbolo_de_la_watchlist_tiene_su_propia_instancia():
    """Una especificación sin símbolo significa "el símbolo que se está evaluando": son
    N instancias, una por símbolo, no una compartida."""
    conjunto = IndicatorSet(
        ["TSLA", "NVDA"], [IndicatorSpec("rsi", "1m", params={"period": 2})]
    )
    for indice, cierre in enumerate((10.0, 11.0, 12.0, 13.0)):
        conjunto.update(_vela("TSLA", "1m", cierre, indice))
    for indice, cierre in enumerate((50.0, 49.0, 48.0, 47.0)):
        conjunto.update(_vela("NVDA", "1m", cierre, indice))

    tsla = conjunto.values_for("TSLA")[("rsi", "1m", "TSLA")]
    nvda = conjunto.values_for("NVDA")[("rsi", "1m", "NVDA")]
    assert tsla > 50 > nvda


def test_aggregator_keys_incluye_los_simbolos_y_marcos_necesarios():
    """El motor construye sus agregadores a partir de esto: si falta un par, ese
    indicador nunca recibiría una vela."""
    conjunto = IndicatorSet(
        ["TSLA"],
        [
            IndicatorSpec("rsi", "1m", params={"period": 14}),
            IndicatorSpec("atr", "5m", params={"period": 14}),
            IndicatorSpec("rsi", "5m", symbol="ONEQ", params={"period": 14}),
        ],
    )
    assert set(conjunto.aggregator_keys()) == {("TSLA", "1m"), ("TSLA", "5m"), ("ONEQ", "5m")}


def test_una_vela_solo_actualiza_los_indicadores_de_su_simbolo_y_marco():
    """Una vela de 5' no puede alimentar el RSI de 1'."""
    conjunto = IndicatorSet(
        ["TSLA"],
        [
            IndicatorSpec("rsi", "1m", params={"period": 2}),
            IndicatorSpec("rsi", "5m", params={"period": 2}),
        ],
    )
    for indice, cierre in enumerate((10.0, 11.0, 12.0, 13.0)):
        conjunto.update(_vela("TSLA", "1m", cierre, indice))

    valores = conjunto.values_for("TSLA")
    assert ("rsi", "1m", "TSLA") in valores
    assert ("rsi", "5m", "TSLA") not in valores


def test_un_indicador_no_registrado_falla_al_construir_el_conjunto():
    """Falla al arrancar con un mensaje claro, en vez de reventar a las 14:32."""
    with pytest.raises(KeyError, match="inventado"):
        IndicatorSet(["TSLA"], [IndicatorSpec("inventado", "1m")])


def test_la_clave_publicada_es_la_que_el_contexto_va_a_buscar():
    """La prueba que faltaba, y cuya ausencia dejó el bot sin operar en silencio.

    `MarketContext.indicator` resuelve `(name, timeframe, symbol or self.symbol)`. Si el
    conjunto publica la clave con `None` por símbolo —que es lo que la especificación trae
    cuando significa "el símbolo que se está evaluando"— esa entrada no la encuentra nadie:
    `ctx.indicator("rsi", "1m")` busca ("rsi", "1m", "TSLA"). La consecuencia era total y
    callada: `rsi_1m`, `rsi_5m` y `support` puntuaban cero en TODAS las velas de TODAS las
    sesiones, el backtest no producía una sola operación, y ninguna prueba lo veía porque
    las de este módulo comprobaban la clave equivocada.
    """
    from bot.core.context import AccountSnapshot, MarketContext

    conjunto = IndicatorSet(
        ["TSLA"],
        [
            IndicatorSpec("rsi", "1m", params={"period": 2}),
            IndicatorSpec("rsi", "5m", symbol="ONEQ", params={"period": 2}),
        ],
    )
    for indice, cierre in enumerate((10.0, 11.0, 12.0, 13.0)):
        conjunto.update(_vela("TSLA", "1m", cierre, indice))
        conjunto.update(_vela("ONEQ", "5m", cierre, indice))

    ctx = MarketContext(
        symbol="TSLA",
        bar=_vela("TSLA", "1m", 13.0),
        now=_vela("TSLA", "1m", 13.0).ts,
        indicators=conjunto.values_for("TSLA"),
        account=AccountSnapshot(0.0, 0.0, 0.0),
        position=None,
    )

    assert ctx.indicator("rsi", "1m") is not None
    assert ctx.indicator("rsi", "5m", symbol="ONEQ") is not None
