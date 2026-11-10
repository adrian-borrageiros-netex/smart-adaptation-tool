"""Horas para personas: la base guarda UTC sin zona, la pantalla enseña hora de Madrid."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

ZONA = ZoneInfo("Europe/Madrid")


def local(valor: str | dt.datetime | None) -> dt.datetime | None:
    """Un instante guardado en UTC (objeto o texto ISO), en hora de Madrid."""
    if not valor:
        return None
    if isinstance(valor, str):
        valor = dt.datetime.fromisoformat(valor)
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=dt.timezone.utc)
    return valor.astimezone(ZONA)


def hora(valor: str | dt.datetime | None, vacio: str = "—") -> str:
    """«14:05» si es de hoy, «27/10 14:05» si es de este año, «27/10/2025 14:05» si no."""
    instante = local(valor)
    if instante is None:
        return vacio
    hoy = dt.datetime.now(ZONA).date()
    if instante.date() == hoy:
        return instante.strftime("%H:%M")
    if instante.year == hoy.year:
        return instante.strftime("%d/%m %H:%M")
    return instante.strftime("%d/%m/%Y %H:%M")


def fecha_hora(valor: str | dt.datetime | None, vacio: str = "—") -> str:
    """Siempre con fecha completa: para la auditoría, donde «14:05» no basta."""
    instante = local(valor)
    return instante.strftime("%d/%m/%Y %H:%M:%S") if instante else vacio


def inicio_del_dia(dia: dt.date) -> dt.datetime:
    """Las 0:00 de ese día en Madrid, en UTC sin zona, para comparar con la base."""
    return (dt.datetime.combine(dia, dt.time(), ZONA)
            .astimezone(dt.timezone.utc).replace(tzinfo=None))
