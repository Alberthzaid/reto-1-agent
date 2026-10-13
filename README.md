# Agent-QA

**Agentic QA Automation Service powered by Falco-Swift**

Agent-QA es un servicio HTTP diseñado para ejecutar tareas automatizadas de QA de escritorio sobre entornos remotos accesibles mediante VNC. Integra **Falco-Swift**, un modelo ligero desarrollado por Salto Ángel, dentro de una arquitectura agéntica propia, con una interfaz de usuario construida en Gradio.

La solución combina inferencia de modelos, orquestación de tareas y ejecución sobre entornos de escritorio para facilitar la automatización de flujos de validación.

## Arquitectura y componentes

- **Falco-Swift:** modelo propio basado en un enfoque de *knowledge distillation*, en el que un encoder compacto aprende a reproducir las distribuciones de salida de un modelo *teacher*. Está orientado a ofrecer inferencia ligera y eficiente.
- **Servicio agéntico:** capa de orquestación responsable de gestionar la ejecución de tareas de QA y la interacción con el entorno remoto.
- **Automatización de escritorio:** ejecución de tareas sobre un destino VNC para evaluar interfaces y flujos de trabajo.
- **Interfaz de usuario:** panel desarrollado con Gradio para interactuar con el agente y gestionar las ejecuciones.
- **API HTTP:** expone los endpoints del servicio para facilitar su integración con otros componentes.
- **Persistencia y trazabilidad:** SQLite y registros de ejecución almacenados en el directorio `./data`.
- **Infraestructura:** Docker Compose para el despliegue de servicios y Caddy como proxy inverso con gestión automática de certificados TLS.

## Despliegue

### 1. Requisitos

- Una máquina virtual con Docker y el complemento Docker Compose.
- Un dominio configurado para apuntar a la máquina virtual.
- Un valor seguro para `TEAM_TOKEN`.

### 2. Clonar el repositorio

```bash
git clone https://github.com/Alberthzaid/reto-1-agent.git agent-qa
cd agent-qa
cp .env.example .env
```

### 3. Configurar las variables de entorno

Edita el archivo `.env` y configura, como mínimo, las siguientes variables:

- `TEAM_TOKEN`: token de autenticación utilizado para proteger los endpoints.
- `DOMAIN`: dominio utilizado por Caddy para configurar el acceso HTTPS y emitir certificados TLS automáticamente.

### 4. Iniciar los servicios

```bash
docker compose up -d --build
docker compose ps
```

### 5. Verificar el estado del servicio

```bash
curl -H "Authorization: Bearer $TEAM_TOKEN" \
  "https://$DOMAIN/health"
```

### 6. Consultar los registros

Los datos de SQLite y las trazas se conservan en el directorio `./data`.

Para inspeccionar los registros del agente y de Caddy:

```bash
docker compose logs -f agent caddy
```

## Seguridad y persistencia

- Los endpoints están protegidos mediante autenticación con `TEAM_TOKEN`.
- Caddy gestiona el acceso HTTPS y la emisión automática de certificados TLS, siempre que el dominio y la infraestructura estén correctamente configurados.
- Los datos persistentes y las trazas se almacenan en `./data`, fuera del ciclo de vida de los contenedores.

## Objetivo del proyecto

Desarrollar un sistema de QA agéntico capaz de interactuar con entornos de escritorio remotos y ejecutar tareas automatizadas, combinando un modelo propio, un servicio de orquestación y una interfaz de usuario accesible.

El proyecto fue desarrollado para participar en FRONTIER, el reto de evaluación de agentes de Lab10.

## Repositorios

- **Agent-QA:** https://github.com/Alberthzaid/reto-1-agent
- **Falco-Swift:** https://github.com/Salto-Angel-Tech/Falco-Swift
