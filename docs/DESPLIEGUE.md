# Despliegue — plataforma de demos (adaptacion.sarela.tech)

Desde el 26/10/2026 la app vive en la plataforma de demos de `sarela.tech`
(repositorio `netexknowledge/smart-sarelatech-demos-platform`): el VPS `srv2086571`, usuario `demos`, en
`~/sarela-adaptacion`, junto a `~/plataforma`. Acceso con Keycloak (roles `admin` y
`editor` del cliente `adaptacion`), base de datos en el Postgres de la plataforma y
ficheros en el volumen `sarela-adaptacion_datos`.

## Alta (una vez)

```bash
~/plataforma/scripts/nueva_app.sh adaptacion ~/sarela-adaptacion admin editor
```

Escribe `.env.plataforma` (Keycloak y `DATABASE_URL`) y `DEMOS_HOST` en `.env`. En
`.env` falta `ANTHROPIC_API_KEY` (ver `.env.ejemplo`).

## Actualizar

```bash
./code/despliegue/subir.sh
```

Despliega la carpeta `code/` del último commit (`git archive HEAD:code`): lo que no esté
en un commit no viaja, y en el servidor queda igual que antes de la estructura corporativa.
Avisa si el commit no está todavía en GitHub. Reinicia la app: antes, comprobar que nadie
la está usando.

## Repositorio

`netexknowledge/smart-adaptation-tool` desde el 09/11/2026 (antes
`joserra-mosteiro-netex/sarela-adaptacion`). Gitflow: rama por defecto `develop`, con PR
obligatorio en `develop`, `main` y `release`. Desde el PC se trabaja con la clave de
despliegue «PC Joserra» (con escritura), alias SSH `github-smart-adaptation-tool`. El
servidor no clona el repositorio: recibe el código por `subir.sh`.

## Dar acceso

Consola de Keycloak (túnel `ssh -N -L 8081:127.0.0.1:8081 demos-vps-app`) > realm
`demos` > Users > usuario > Role mapping > rol `admin` o `editor` del cliente
`adaptacion`.

## Desarrollo en local

```bash
cd code && SARELA_LOCAL=1 PYTHONPATH=../../demos-plataforma/code/kit python -m uvicorn web.app:aplicacion --port 8800
```

Sin `DEMOS_APP` ni `SARELA_LOCAL=1` la app no arranca: nunca queda abierta por
descuido.