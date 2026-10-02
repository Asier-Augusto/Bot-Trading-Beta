# Bot Trading Beta

**Núcleo de un bot de trading intradía**: motor de decisión, bus de eventos, indicadores
incrementales, gestión de riesgo y modelo de ejecución para backtest. Escrito en Python,
con 484 pruebas que corren en menos de tres segundos y sin tocar la red.

> *English summary — This is the public, partial core of an intraday trading bot: event bus,
> injectable clock, O(1) incremental indicators, risk-filter chain and a deliberately
> pessimistic backtest execution model. The trading strategy itself, the support-zone
> detection and the live broker adapter are **not** part of this repository. See
> "Qué hay y qué no hay" below.*

---

## Qué es esto

Un bot de trading tiene dos mitades muy distintas. Una es **la estrategia**: qué comprar y
cuándo. La otra es **todo lo demás**: recibir datos sin perder ticks, mantener indicadores al
día, decidir el tamaño, no saltarse los límites de riesgo, hablar con el bróker, dejar un
registro de lo que pasó y poder reproducirlo después sobre históricos.

Este repositorio es la segunda mitad. Es la parte que no cambia cuando cambias de estrategia,
y la que decide si el sistema entero se sostiene o no.

## Las decisiones que lo definen

**1. Motor directo, observadores por bus.** El camino de las órdenes
(feed → motor → indicadores → estrategia → riesgo → bróker) son llamadas directas y
síncronas. Una orden tiene que ejecutarse exactamente una vez y en orden; una llamada directa
lo garantiza sin tener que demostrarlo, y si el bróker rechaza algo el error aparece en la
misma pila, con su contexto. En paralelo, cada etapa **emite eventos** a un bus del que
cuelgan los observadores. Dos razones concretas: repartir a N consumidores sin que el motor
los conozca, y sacar el disco y la red del camino de decisión — emitir un evento en memoria
cuesta cientos de nanosegundos, una escritura sincronizada milisegundos, una petición de red
centenares de milisegundos.

> La regla que evita que esto degenere en sopa de eventos: si un mensaje tiene un solo
> destinatario y alguien espera su resultado, es una **llamada**. Si es un hecho consumado
> que puede interesar a cualquiera, es un **evento**.

**2. Reloj inyectable.** Ningún componente llama a `datetime.now()`. La hora se pide a un
`Clock` inyectado: en vivo es el reloj real, en backtest avanza saltando de evento en evento.
La consecuencia práctica es que la misma estrategia, sin cambiar una línea, corre en paper y
en backtest. Sin esto acabarían existiendo dos versiones de cada estrategia que divergen en
silencio, y entonces el backtest deja de significar nada.

**3. Indicadores incrementales, no recálculo con pandas.** Cada indicador se actualiza en
**O(1)** por vela guardando dos o tres números de estado. El estocástico usa colas monótonas
para dar máximo y mínimo de la ventana en coste constante amortizado en lugar de recorrer 14
velas cada vez. `pandas` solo aparece en los bordes: carga de históricos e informes.

> Honestidad sobre la latencia: en intradía la latencia la manda la red y el bróker, del orden
> de decenas de milisegundos. Python no compite con HFT y no es lo que se persigue. Lo que gana
> el diseño incremental es vigilar muchos símbolos con muchos indicadores en el mismo hilo sin
> perder ticks, y escalar de 30 a 300 símbolos sin rediseñar nada.

**4. La estrategia propone, el riesgo dispone.** Una estrategia no habla con el bróker, no
escribe en disco y no consulta la hora del sistema: recibe un contexto de **solo lectura** y
devuelve intenciones. Puede expresar el tamaño que desea, pero una cadena de filtros de riesgo
tiene la última palabra, recorta a los límites globales y emite el motivo. Así una estrategia
con un fallo no puede vaciar la cuenta.

**5. Las velas las construye el bot, no el bróker.** El agregador va de tick a 1′ a 5′ con su
propio reloj de pared. Dos motivos: el proceso es idéntico en vivo y en backtest, y la vela se
cierra aunque no llegue ningún tick, para que un símbolo poco líquido no congele sus
indicadores.

**6. El backtest es deliberadamente pesimista.** Las órdenes a mercado entran en la
**apertura de la vela siguiente**, nunca al precio que disparó la señal; los stops se evalúan
por el recorrido completo de la vela; el deslizamiento y las comisiones son configurables y
están puestos. Es preferible que el backtest dé cifras peores que la realidad y no al revés:
un backtest optimista es peor que no tener backtest.

**7. Se registra lo que *casi* pasó.** El evento `SignalEvaluated` no dice "no compré": guarda
el vector completo de condiciones con sus valores, se cumplan o no. Eso permite preguntarle
después a los datos cuántas operaciones se perdieron por cada condición y qué habrían rendido,
en lugar de afinar umbrales a ojo.

## Qué hay y qué no hay

Este repositorio es una **versión pública parcial**. Lo que falta no falta por estar sin
hacer: está fuera a propósito.

| Incluido | Fuera de esta versión |
|---|---|
| Motor, bus de eventos, reloj inyectable, registro de módulos | La estrategia de entrada y salida |
| Los seis contratos abstractos (feed, bróker, estrategia, sizer, sink, reloj) | La detección y puntuación de zonas de soporte |
| Indicadores incrementales: RSI, estocástico, ATR, VWAP, agregador de velas | El adaptador de IBKR (datos y órdenes reales) |
| Cadena de filtros de riesgo, sizers, trailing | El visor web de mercado |
| Bróker simulado con modelo de ejecución, feed de fichero, sink JSONL | Las herramientas de barrido y optimización de parámetros |
| Panel de terminal como observador del bus | Los perfiles YAML con parámetros reales |
| 484 pruebas | |

Donde una pieza pública depende de una privada, lo que queda es **el contrato**, no un hueco:
`bot.support.provider` define la interfaz que el motor consume y una implementación nula, de
modo que el motor arranca y se prueba sin detección de zonas. La lógica de detección no está.

## Probarlo

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

```
484 passed, 6 skipped
```

Las pruebas no tocan la red ni necesitan un bróker: el bróker simulado y el feed de fichero
son implementaciones de primera clase de los mismos contratos que usa el adaptador real. La
batería de conformidad (`tests/contracts/test_broker_conformance.py`) está escrita para correr
contra **varias** implementaciones de `Broker` a la vez; aquí solo viaja el simulado, pero la
estructura parametrizada es la que permite añadir otro bróker sin tocar ni una prueba.

## Mapa del repositorio

```
src/bot/
├── core/           motor, bus, reloj, eventos, contexto, sesión, registro de módulos
├── contracts/      las seis clases abstractas que hacen intercambiables los módulos
├── indicators/     RSI, estocástico, ATR, VWAP y el agregador tick → 1′ → 5′
├── risk/           filtros, sizers, trailing y el gestor que los encadena
├── adapters/
│   ├── brokers/    bróker simulado con modelo de ejecución y costes
│   ├── data/       feed de reproducción desde CSV
│   ├── history/    carga de históricos
│   └── sinks/      registro JSONL, un fichero por tema y por día
├── reporting/      panel de terminal (observador del bus)
├── support/        contrato de zonas de soporte (sin la detección)
└── config/         carga de perfiles YAML y de credenciales
tests/              484 pruebas, espejo de la estructura de src/
```

## Convenciones

Nombres de clases, funciones y carpetas en **inglés**; comentarios y documentación en
**español**. Cada función documenta su papel y, para cada parámetro y para el valor devuelto,
el tipo y qué representa. Los comentarios explican **por qué**, no qué: si hay una decisión
medida detrás, está escrita con su medición.

El desarrollo es guiado por pruebas: la prueba se escribe antes que la implementación.

## Estado

Beta. El núcleo está completo y probado; la versión privada añade la estrategia, el adaptador
de IBKR, el visor de mercado y las herramientas de investigación.

## Aviso

Esto no es consejo financiero. Es software experimental para investigación propia, publicado
sin ninguna garantía. Operar en mercados financieros con dinero real puede acarrear la pérdida
total del capital; quien ejecute este código lo hace bajo su entera responsabilidad.

## Licencia

**Todos los derechos reservados.** Este repositorio se publica para que pueda leerse y
evaluarse. No se concede licencia de uso, copia, modificación ni distribución. Si quieres usar
algo de aquí, escríbeme.

© 2026 Asier Augusto
