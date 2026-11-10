"""El idioma real del contenido de una unidad, leído en el texto.

No basta con la clave de idioma del paquete. «5, Los Invertebrados» llegó con `es` en
`project.json` y en la copia embebida, `pt` como idioma del reproductor y el contenido
entero en gallego. Fiándose de la clave, una adaptación a Andalucía en castellano no habría
traducido nada: habría salido una unidad en gallego «adaptada».

La detección cuenta palabras que solo son frecuentes en una de las lenguas. Es tosca, pero
entre gallego, castellano, catalán, portugués y euskera las palabras de función bastan.
"""

from __future__ import annotations

import re
from collections import Counter

# Palabras frecuentes y propias de cada lengua frente a las demás de la lista.
_MARCAS = {
    "gl": {"unha", "tamén", "xa", "polo", "pola", "coa", "cos", "moi", "dende", "ademais",
           "máis", "cando", "aínda", "teñen", "ten", "fan", "estes", "estas", "isto", "iso",
           "noso", "nosa", "súa", "seu", "eles", "elas", "xeito", "algúns", "outros", "outras"},
    "es": {"una", "los", "las", "también", "muy", "del", "pero", "cuando", "tiene", "tienen",
           "hacen", "estos", "esto", "eso", "nuestro", "nuestra", "ellos", "ellas", "manera",
           "algunos", "otros", "otras", "además", "hay", "muchos", "puede", "pueden"},
    "ca": {"amb", "també", "molt", "dels", "són", "és", "aquest", "aquesta", "aquests",
           "tenen", "fan", "nostre", "nostra", "ells", "elles", "alguns", "altres", "però",
           "quan", "hi", "poden", "més", "seva", "seu", "pel", "una"},
    "pt": {"uma", "não", "também", "muito", "você", "são", "pelo", "pela", "têm", "fazem",
           "estes", "isso", "nosso", "nossa", "eles", "elas", "alguns", "outros", "mais",
           "quando", "ainda", "onde", "seu", "sua", "há"},
    "eu": {"eta", "dira", "bat", "ez", "ere", "baina", "dute", "daude", "hau", "hori",
           "gure", "beraiek", "batzuk", "beste", "gehiago", "noiz", "non", "du"},
}


def detectar(textos: list[str]) -> tuple[str | None, float]:
    """El idioma del conjunto de textos y la confianza, de 0 a 1.

    La confianza es la parte de las marcas que son de la lengua ganadora. Por debajo de
    0,6, o con muy poco texto, no se decide nada.
    """
    palabras = Counter(p for t in textos for p in re.findall(r"[^\W\d_]+", (t or "").lower()))
    puntos = {l: sum(palabras[m] for m in marcas) for l, marcas in _MARCAS.items()}
    total = sum(puntos.values())
    if total < 30:
        return None, 0.0
    ganador = max(puntos, key=puntos.get)
    return ganador, puntos[ganador] / total
