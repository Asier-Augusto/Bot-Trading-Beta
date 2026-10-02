"""Pintado del panel con `rich`, separado del modelo.

El modelo (`dashboard.DashboardModel`) es lo que se prueba y lo que decide qué se muestra.
Esto solo lo convierte en tablas. La separación es del §10.2 y no es de estilo: una tabla
bonita que muestra el margen del kill switch mal calculado invita a confiar en ella.

Dos decisiones que no son de estilo:

- **Si `rich` no está instalado, el panel cae al texto plano** en vez de impedir que la sesión
  arranque. Un bot que no opera porque falta una biblioteca de presentación sería mal cambio.
- **Se pinta desde el `snapshot()` del panel, no de su modelo vivo.** El modelo se actualiza en
  el hilo del bus; iterarlo desde el hilo de pintado se caería justo cuando más pasa.
"""

from __future__ import annotations

from bot.reporting.dashboard import Dashboard, DashboardModel, render

#: Cuántas veces por segundo se repinta. Suficiente para que se vea vivo y no tanto como para
#: gastar CPU dibujando.
REFRESH_PER_SECOND = 4


def rich_available() -> bool:
    """Indica si `rich` se puede importar.

    return : bool
    """
    try:
        import rich  # noqa: F401
    except ImportError:
        return False
    return True


def build_renderable(model: DashboardModel):
    """Compone el panel como objeto de `rich`.

    model  : DashboardModel — estado a pintar, ya estable.
    return : RenderableType — grupo de tablas.
    raises : ImportError — si `rich` no está instalado.
    """
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    # --- cabecera: el resultado del día y el margen del kill switch ---
    if model.kill_switch_tripped:
        estado = Text("KILL SWITCH CORTADO", style="bold white on red")
    else:
        apurado = model.kill_switch_headroom < abs(model.kill_switch_limit) / 3
        estado = Text(f"margen {model.kill_switch_headroom:,.2f}",
                      style="yellow" if apurado else "green")

    cabecera = Table.grid(padding=(0, 3))
    cabecera.add_row(
        Text(f"P&L día {model.daily_pnl:+,.2f}",
             style="green" if model.daily_pnl >= 0 else "red"),
        Text(f"no realizado {model.total_unrealized:+,.2f}",
             style="green" if model.total_unrealized >= 0 else "red"),
        Text(f"cerradas {model.closed_trades}"),
        estado,
    )

    # --- símbolos y sus condiciones ---
    simbolos = Table(title="Vigilando", expand=True)
    for columna in ("símbolo", "precio", "nivel", "total", "condiciones"):
        simbolos.add_column(columna, justify="right" if columna != "condiciones" else "left")

    for vista in sorted(model.symbols.values(), key=lambda v: v.symbol):
        detalle = Text()
        for condicion in vista.conditions:
            if detalle.plain:
                detalle.append("  ")
            detalle.append(
                f"{condicion.name}={condicion.display_value}({condicion.score:.2f})",
                style="green" if condicion.passed else "red",
            )
        simbolos.add_row(
            vista.symbol,
            f"{vista.price:.2f}",
            Text(vista.level, style="bold cyan" if vista.level == "ARMED" else ""),
            f"{vista.total:.3f}",
            detalle,
        )

    # --- posiciones abiertas ---
    posiciones = Table(title="Posiciones abiertas", expand=True)
    for columna in ("símbolo", "tramo", "accs", "entrada", "stop", "no real."):
        posiciones.add_column(columna, justify="left" if columna in ("símbolo", "tramo") else "right")

    for p in sorted(model.positions.values(), key=lambda p: (p.symbol, p.tag)):
        posiciones.add_row(
            p.symbol, p.tag, str(p.shares), f"{p.entry_price:.2f}", f"{p.stop_price:.2f}",
            Text(f"{p.unrealized:+,.2f}", style="green" if p.unrealized >= 0 else "red"),
        )
    if not model.positions:
        posiciones.add_row("—", "", "", "", "", "")

    return Group(Panel(cabecera, title="Sesión"), simbolos, posiciones)


class _Repainted:
    """Renderable que vuelve a leer el panel en cada refresco.

    `rich.live.Live` repinta el MISMO objeto varias veces por segundo. Si se le entregara el
    grupo de tablas ya construido, el panel se quedaría congelado en el estado del arranque.
    """

    def __init__(self, dashboard: Dashboard) -> None:
        """Envuelve un panel.

        dashboard : Dashboard — de donde se saca el estado en cada refresco.
        """
        self.dashboard = dashboard

    def __rich__(self):
        """Construye el panel con el estado del momento.

        return : RenderableType
        """
        return build_renderable(self.dashboard.snapshot())


class LivePanel:
    """Repinta el panel en la terminal mientras la sesión corre.

    Se usa como gestor de contexto. Sin `rich` o desactivado, no pinta nada durante la sesión y
    deja el resumen en texto plano al terminar: es preferible a impedir que el bot arranque.
    """

    def __init__(self, dashboard: Dashboard, enabled: bool = True) -> None:
        """Crea el panel.

        dashboard : Dashboard — el observador cuyo estado se pinta.
        enabled   : bool — si se pinta en vivo.
        """
        self.dashboard = dashboard
        self.enabled = enabled and rich_available()
        self._live = None

    def __enter__(self) -> "LivePanel":
        """Arranca el repintado.

        return : LivePanel
        """
        if not self.enabled:
            return self

        from rich.live import Live

        self._live = Live(_Repainted(self.dashboard), refresh_per_second=REFRESH_PER_SECOND)
        self._live.__enter__()
        return self

    def __exit__(self, *excepcion) -> None:
        """Para el repintado y deja el estado final visible.

        return : None
        """
        if self._live is not None:
            self._live.__exit__(*excepcion)
            self._live = None
        else:
            print(render(self.dashboard.snapshot()), flush=True)
