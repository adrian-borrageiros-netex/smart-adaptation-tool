"""Carga en la memoria las equivalencias geológicas Galicia → Cataluña.

No son traducciones: son decisiones de materia. Que la unidad gallega hable de granitos y
del gneis ollo de sapo no se convierte en contenido catalán traduciendo, porque Cataluña
tiene otra geología. Se registran **aprobadas** para que el sistema las proponga, pero
`web/trabajador.py` nunca las aplica solo: siempre pasan por el editor.

Las tres zonas son las de la actividad de emparejar de «Al cierre», y tienen que quedar
coherentes con la lección territorial y con el mapa. Ese cruce fue el error que detectaron
los revisores en la adaptación a Asturias, así que aquí van juntas y en un solo sitio.

    python scripts/sembrar_cataluna.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from web import bd, memoria  # noqa: E402

ORIGEN, DESTINO = "Galicia", "Cataluña"

# concepto tal y como queda tras traducir → propuesta catalana, y por qué.
EQUIVALENCIAS: list[tuple[str, str, str]] = [
    # --- Las tres zonas de la actividad de emparejar y de la lección territorial.
    ("Granits i roques metamòrfiques",
     "Granits, esquists i calcàries",
     "Zona axial del Pirineu: granits i esquists paleozoics, amb calcàries mesozoiques "
     "a les serres prepirinenques."),
    ("Pissarres i algunes roques sedimentàries",
     "Granits i pissarres",
     "Serralades Costaneres Catalanes: granits de la Costa Brava i el Montseny, i "
     "pissarres paleozoiques."),
    ("Gneis ull de gripau",
     "Conglomerats, calcàries i sals potàssiques",
     "Depressió Central Catalana: conglomerats de Montserrat i Sant Llorenç del Munt, "
     "calcàries lacustres i la conca potàssica del Bages."),
    ("Est de Galícia", "Serralades Costaneres",
     "La franja litoral i prelitoral sustitueix a la zona este gallega."),
    ("Oest de Galícia", "Pirineu",
     "El domini de roques ígnies i metamòrfiques a Catalunya és el Pirineu axial."),
    ("Zona central de Galícia", "Depressió Central",
     "La conca sedimentària terciària ocupa la posició central del país."),

    # --- Minerales y recursos característicos.
    ("la caolinita, quarsos, magnesita, estany, wolframita, berils",
     "les sals potàssiques i la sal gemma, la fluorita, la baritina, la galena i el caolí",
     "Recursos miners propis de Catalunya: conca potàssica del Bages, fluorita d'Osor, "
     "plom de Bellmunt del Priorat."),
    ("As Pontes de García Rodríguez",
     "Cardona",
     "Explotació tancada i recuperada per a un ús nou: la mina de sal de Cardona va "
     "tancar el 1990 i avui és el Parc Cultural de la Muntanya de Sal."),

    # --- Atribución institucional. Se marca como decisión, no como sustitución.
    ("Xunta de Galicia",
     "[PENDIENTE: atribución institucional de la edición catalana]",
     "Quién figura en una edición catalana es una decisión de Sarela, no técnica."),
]


def main() -> int:
    bd.crear_esquema()
    nuevas = repetidas = 0
    for concepto, propuesta, motivo in EQUIVALENCIAS:
        if memoria.guardar_equivalencia(concepto, propuesta, ORIGEN, DESTINO,
                                        motivo=motivo, aprobada=True,
                                        procedencia="sembrar_cataluna.py"):
            nuevas += 1
        else:
            repetidas += 1
        print(f"  {concepto[:46]:<46} → {propuesta[:46]}")

    print(f"\nEquivalencias nuevas: {nuevas}   actualizadas: {repetidas}")
    inventario = memoria.resumen()
    print(f"Memoria: {inventario['equivalencias']} equivalencias "
          f"{inventario['equivalencias_por_territorio']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
