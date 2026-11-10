#!/usr/bin/env bash
# Despliega el último commit en la plataforma de demos y reconstruye la app. Desde el PC,
# desde la raíz del repositorio:
#
#   ./code/despliegue/subir.sh            # al alias demos-vps-app de ~/.ssh/config
#   ./code/despliegue/subir.sh otro-alias
#
# Viaja `git archive HEAD:code` (la carpeta code/ del repositorio, sin docs/ ni la
# configuración corporativa): exactamente lo versionado, nunca un fichero suelto del PC (ni
# .env, ni unidades, ni api/). Lo que no esté en un commit no se despliega. No toca los
# .env ni los datos del servidor.
#
# Reinicia la app: lo que un editor esté haciendo en ese momento se pierde.
set -euo pipefail

destino="${1:-demos-vps-app}"
cd "$(dirname "$0")/.."

[ -z "$(git status --porcelain)" ] || echo "AVISO: hay cambios sin commit; no se despliegan." >&2
commit="$(git rev-parse --short HEAD)"
# Lo desplegado debería estar también en GitHub (netexknowledge/smart-adaptation-tool).
if [ -z "$(git branch -r --contains HEAD 2>/dev/null)" ]; then
  echo "AVISO: $commit no está en GitHub todavía; conviene hacer push." >&2
fi

git archive --format=tar HEAD:code | ssh -o BatchMode=yes "$destino" "
    set -e
    mkdir -p ~/sarela-adaptacion && cd ~/sarela-adaptacion
    tar xf -
    echo $commit > DESPLEGADO
    docker compose up -d --build app
    docker compose ps --format '{{.Name}}  {{.Status}}'"
echo "Desplegado $commit."
