# Agent-QA

Servicio HTTP para ejecutar tareas de QA de escritorio contra un destino VNC.

## Desplegar

En la VM, instala Docker con el complemento Compose, clona el repositorio y prepara la configuración:

```bash
git clone https://github.com/Alberthzaid/reto-1-agent.git agent-qa
cd agent-qa
cp .env.example .env
```

Configura en `.env` al menos `TEAM_TOKEN` y `DOMAIN`. `TEAM_TOKEN` protege todos los endpoints;
`DOMAIN` se usa en el entorno de Caddy para emitir TLS automáticamente. Después inicia el servicio:

```bash
docker compose up -d --build
docker compose ps
curl -H "Authorization: Bearer $TEAM_TOKEN" https://"$DOMAIN"/health
```

Los datos SQLite y las trazas se conservan en `./data`. Para revisar registros del despliegue:

```bash
docker compose logs -f agent caddy
```