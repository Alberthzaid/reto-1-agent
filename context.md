# No-API Agent — contexto y prompts

## 1. El reto

Agente que opera apps legacy **sin API, solo por pantalla (VNC)** y ejecuta tareas en lenguaje natural en apps que nunca vio. Se evalúa sobre **otra app**, escritorio limpio por tarea, **5 min por tarea**. Entrega: servicio público desde las 20:00.

**Reglas duras**
1. Nada de DOM (ni Playwright, ni Selenium, ni HTML). Solo píxeles por VNC.
2. Nada de agentes de terceros ni la herramienta empaquetada de "computer use". Bucle, percepción y acciones son nuestros; el LLM solo decide con herramientas propias.
3. Nunca loguear, persistir ni reportar la contraseña del escritorio ni el token.
4. Valores del contrato en español y literales.
5. Ante la duda, `fallida`. Falso éxito y cambios de más son los peores errores.

## 2. Contrato

Todas las llamadas con `Authorization: Bearer <TEAM_TOKEN>`.

| Ruta | Respuesta |
|---|---|
| `GET /health` | 200 cuando está listo |
| `POST /tasks` | `{"tarea": "...", "escritorio": {"url": "wss://.../websockify", "password": "..."}}` → `{"task_id": "..."}` en < 2 s |
| `GET /tasks/{id}` | `{"estado": "en_curso" \| "terminada" \| "fallida", "resumen": "..."}` |

El evaluador consulta cada 5 s, corta a los 5 min y resetea. **Al reportar, se deja de operar y se cierra el VNC.**

**Resumen**
- Pregunta → `respuesta: <valor>`; número primero, sin separador de miles. Dos datos → `respuesta: A; B`.
- Pregunta imposible → sin línea `respuesta:`.
- Tarea que falla → nombra los datos que explican por qué.

## 3. El escritorio

- Kiosko 1280×800, RFB por `wss://<escritorio>/websockify` (auth VNC con DES). `/` = noVNC para mirar. `/__reset` y `/__check` con el token del escritorio.
- Repinta hasta 1 s después de un clic · clic < 100 ms se ignora (usar 150 ms) · escribir concatena (Ctrl+A antes).
- "Hoy" = la fecha que muestra la app.
- `probe_vnc.py` ya valida conexión, auth, capturas, clic y escritura con tildes.

## 4. Lo que enseñan las pruebas

| Prueba | Instrucción | Esperado | Lección |
|---|---|---|---|
| E1 | Registra al cliente Papelería Los Andes, NIT…, teléfono…, ciudad Pereira | `terminada`, 4 campos | Solo los campos pedidos; valores literales con tildes; buscar duplicado antes |
| E2 | Corrige la dirección de Distribuciones El Faro: Calle 80 # 24-15 | `terminada`, `CL-0007.direccion` | Ubicar el registro exacto; Ctrl+A; no tocar otros campos |
| E3 | ¿Cuántas facturas vencidas tiene Ferretería Ruiz? | `terminada`, `respuesta: 45` | Contar bien (paginación, filtros); número primero |
| E4 | Exporta pedidos entregados en septiembre de 2026 | `terminada`, export con estado y rango 01–30/09 | Aplicar todos los filtros antes de exportar |
| E5 | Cambia el teléfono de Ana Torres a 3014445566 | `fallida`, nombrar 52345678 y 1019876543 | Homónimos: no adivinar; fallar nombrando los candidatos |

## 5. Diseño

**Estrategia:** precisión antes que cobertura. Guion por tarea: orientarse (fecha, módulo) → clasificar → investigar en solo lectura → decidir (imposible, ambigua o peligrosa = `fallida` sin tocar nada) → escribir una sola vez → verificar en pantalla → reportar.

**Arquitectura:** monolito modular asíncrono con puertos y adaptadores. El dominio solo habla con puertos.

```text
Evaluador → API (FastAPI) → Gestor de tareas → Worker (asyncio.Task)
Worker: Política LLM → Guardia de escritura → Pantalla VNC → Escritorio
        Asentador + OCR (percepción) · Verificador · Reportero · Memoria · Falco (opcional)
```

- **Workers:** `asyncio.Task` en el mismo proceso; una tarea por escritorio (la nueva cancela la vieja); reloj de 255 s; OCR en `asyncio.to_thread`; uvicorn `--workers 1`.
- **Guardia:** detecta commits por el texto del botón, limita los commits al plan, bloquea teclas peligrosas (F5, Ctrl+W, Alt+F4…).
- **Falco:** clasificador propio que da **pistas**, nunca decide. Timeout 300 ms, umbral 0.8, interruptor de circuito; solo puede sumar cautela.

**Stack:** Python 3.13 · FastAPI + uvicorn · websockets + pycryptodome · numpy + Pillow · Tesseract (`spa`) · Claude por Messages API · SQLite · Docker Compose + Caddy.

**Estructura**

```text
agent/
  api/            app.py, auth.py, schemas.py
  application/    manager.py, worker.py
  domain/         models, policy, tools, guard, verifier, reporter, settler, dates, prompts/*.md
  ports/          interfaces.py (Screen, Reader, Model, Memory, Classifier)
  infra/          vnc/ (rfb, keys, vnc_screen), tesseract_ocr, claude_model, sqlite_memory, falco_classifier
  observability/  traces.py, log_setup.py
dev_tools/        run_tests.py, record_screens.py, mock_vnc_server.py, view_trace.py
pruebas/          E1–E5 (no modificar) · custom_tests/ · tests/ · data/ (fuera de git)
```

**Variables:** `ANTHROPIC_API_KEY`, `TEAM_TOKEN`, `MODEL_FAST`, `MODEL_STRONG`, `TIME_LIMIT_SEC=255`, `DATA_DIR`, `TESSERACT_CMD`, `FALCO_ENABLED`, `FALCO_MODEL_DIR`. Pruebas: `DESKTOP_URL`, `DESKTOP_PASSWORD`, `DESKTOP_TOKEN`.

## 6. Plan

| # | Etapa | Hora | Listo cuando |
|---|---|---|---|
| 1 | VNC y actuador | ya | Captura, clic y "Papelería" funcionan en el escritorio real |
| 2 | API, workers, despliegue | ya | URL pública responde el contrato |
| 3 | Percepción | 16:30 | Captura asentada + OCR con coordenadas + trazas |
| 4 | Cerebro del agente | 17:00 | E1 y E2 pasan `/__check` |
| 5 | Arnés de pruebas | 17:00 | Un comando corre E1–E5 con informe |
| 6 | Falco integrado | 18:00 | Arnés igual o mejor con Falco encendido |
| 7 | Endurecer y congelar | 18:30–19:15 | Prueba de humo desde internet |

Una etapa por sesión de Claude Code: `pytest -q`, commit y `/clear` entre etapas. Este archivo va en la raíz del repo y cada prompt empieza pidiendo leerlo.

---

## 7. Prompts para Claude Code

### Etapa 1 — VNC y actuador

```text
Lee PROYECTO.md. Implementa el acceso al escritorio usando probe_vnc.py como base (refactoriza, no
reinventes):
- agent/infra/vnc/rfb.py: RFBClient async (handshake 3.8 con fallback 3.3, auth DES, Raw + CopyRect,
  lectora que reensambla frames, last_change con time.monotonic, close que cancela la lectora).
- agent/infra/vnc/keys.py: keysym (Latin-1 directo, resto 0x01000000+cp), parse_combo("ctrl+a",
  "Enter", "shift+tab"...) y BLOCKED_COMBOS que lanza BlockedKeyError.
- agent/infra/vnc/vnc_screen.py: puerto Screen; clic con 150 ms, doble clic, botón derecho, scroll con
  botones 4/5, type_text tecla por tecla, screenshot RGB 1280x800, coordenadas validadas.
- dev_tools/mock_vnc_server.py (repinta 600 ms tras un clic) y dev_tools/record_screens.py.
- tests contra el mock: handshake, contraseña mala, clic con repintado retrasado, frames partidos,
  tildes y ñ. Nunca la contraseña en logs ni errores.
```

### Etapa 2 — API, workers y despliegue

```text
Lee PROYECTO.md. Implementa el contrato exacto con un DummyWorker inyectable:
- api/schemas.py (password como SecretStr), api/auth.py (Bearer con hmac.compare_digest), api/app.py.
- application/manager.py: create() sin await de red (< 2 s); dict url→tarea activa, la nueva cancela
  la previa y espera a que suelte; asyncio.timeout(TIME_LIMIT_SEC); TimeoutError, CancelledError y
  Exception → fallida con resumen parcial sin trazas; finally worker.close(); semáforo; estado en SQLite;
  al arrancar, las en_curso viejas pasan a fallida.
- Dockerfile (python:3.13-slim + tesseract-ocr-spa, uvicorn --workers 1), docker-compose.yml con Caddy
  y restart: always, Caddyfile con {$DOMAIN}.
- tests: 401, POST < 2 s, en_curso→terminada, cancelación por mismo escritorio, timeout, 404.
```

### Etapa 3 — Percepción

```text
Lee PROYECTO.md. Implementa cómo ve el agente:
- domain/settler.py: espera un cambio tras la acción y 300 ms de quietud, tope 2 s; dice si hubo cambio.
- infra/tesseract_ocr.py: Reader con image_to_data spa+eng en to_thread, escala 2x, agrupa por línea,
  coordenadas en píxeles de pantalla, confianza ≥ 30, soporte de recorte; TESSERACT_CMD opcional.
- domain/diff.py: regiones que cambiaron entre dos capturas.
- observability/traces.py (PNG + actions.jsonl por paso, sin secretos) y dev_tools/view_trace.py (HTML).
- tests con Screen falso e imágenes sintéticas (skip si no hay tesseract).
```

### Etapa 4 — Cerebro del agente (modo plan)

```text
Lee PROYECTO.md, sobre todo las secciones 4 y 5. Primero dame el plan y espera mi OK.
Objetivo: E1 y E2 pasan /__check con estado correcto, sin romper E3–E5.
- infra/claude_model.py: AsyncAnthropic compartido, tool use con herramientas propias, backoff en
  429/5xx, timeout 40 s, imágenes PNG a resolución nativa.
- domain/tools.py: click, type_into(x, y, text, replace=True: clic + Ctrl+A + escribir + releer con OCR),
  key, scroll, wait(≤3 s), zoom, fill_form, finish(estado, resumen, respuesta?, datos_razon?, aprendizaje?).
- domain/guard.py: commit = clic sobre guardar/grabar/aceptar/confirmar/sí/eliminar/anular/crear/
  registrar/exportar/actualizar; máximo planned_commits; nunca repetir un commit sin verificar.
- domain/policy.py por fases ORIENT → PLAN → EXECUTE → VERIFY, prompts en domain/prompts/*.md:
  leer la fecha de la app; buscar antes de crear (duplicados); coincidencia exacta del registro;
  si hay homónimos o datos ambiguos → fallida nombrando los identificadores de los candidatos (E5);
  aplicar todos los filtros antes de exportar (E4); contar revisando paginación (E3); escribir solo
  los campos pedidos. Contexto: última captura + OCR compacto + cambios + tiempo restante; solo las
  3 últimas imágenes; con < 40 s, forzar finish.
- domain/verifier.py: VERIFY con MODEL_STRONG y captura fresca; sin confirmación → fallida.
- domain/reporter.py: plantilla determinista, normalización de números, "; " para dos datos, redacción
  de secretos.
- application/worker.py: AgentWorker real con trazas; main.py lo usa.
- tests con Screen y Model falsos: guardia, reporter, fallida sin escritura, corte por tiempo.
```

### Etapa 5 — Arnés de pruebas

```text
Lee PROYECTO.md. Crea dev_tools/run_tests.py: por cada JSON de pruebas/ (o --dir custom_tests,
--only E1,E2): /__reset → POST /tasks → consulta cada 5 s hasta 300 s → /__check. Además compara
estado con estado_esperado, la línea respuesta: con respuesta_esperada (primer número), que aparezcan
todas las claves_razon y que el resumen no tenga secretos. Tabla final con aciertos, falsos éxitos y
tiempo medio; informe JSON en data/reports/. Crea en custom_tests/ variantes de E1–E5 con datos que
existan en la app de pruebas (pídeme capturas si hace falta).
```

### Etapa 6 — Falco integrado

```text
Lee PROYECTO.md. Integra Falco (modelo en FALCO_MODEL_DIR, preguntas en questions_agente.json):
- infra/falco_classifier.py: import perezoso de torch; si falta o FALCO_ENABLED=false, devuelve None;
  carga en segundo plano; classify() en to_thread con wait_for(0.3); descarta respuestas con
  confianza < 0.8; interruptor de circuito (3 fallos → 10 min apagado); nunca propaga excepciones.
- El worker lo llama en paralelo a la conexión VNC. Las pistas solo suman cautela:
  identifica_por_nombre → buscar homónimos antes de escribir; destructiva → confirmar alcance;
  con_filtros/fecha_relativa → leer fecha y aplicar filtros; dos_datos/respuesta_numerica → formato.
  Nunca saltan precondiciones ni la verificación.
- tests: Falco ausente, lento, con error, con baja confianza y con circuito abierto → mismo resultado.
```

### Etapa 7 — Endurecer y congelar

```text
Lee PROYECTO.md. Sin funcionalidades nuevas: auditoría de secretos con tests; confirmar que nada envía
eventos VNC después de reportar o de TIME_LIMIT_SEC; reconexión VNC una vez al inicio; /health real;
dev_tools/smoke_test.py contra la URL pública (401 sin token, /health, POST < 2 s, una pregunta
completa); README con correr, probar, desplegar y ver trazas; pytest en verde.
```

### Depurar una prueba que falla

```text
La prueba <ID> falló. Revisa data/traces/<task_id>/ y data/reports/. Dime en qué paso se desvió
(percepción, LLM, guardia, verificación o reporte) y por qué. Haz el cambio mínimo con un test que
lo reproduzca y corre solo esa prueba con dev_tools/run_tests.py --only <ID>.
```

### Entrenar Falco (en el repo Falco-Swift, con E1–E5 copiadas en pruebas/)

```text
Lee README.md, tag.py y train.py de este repo (Falco). En la carpeta pruebas/ están las 5 tareas
de práctica del reto (E1 a E5). Léelas: son el patrón de las instrucciones que recibirá el agente.
No modifiques el código existente.

1. Crea questions_agente.json con estas preguntas sobre la instrucción:
   - tipo (choice): crear un registro, modificar un registro, consultar un dato (pregunta),
     exportar un reporte, eliminar o anular
   - respuesta_numerica (noul): pregunta cuya respuesta es un número o un conteo (como E3)
   - dos_datos (noul): pregunta que pide dos datos distintos
   - con_filtros (noul): pide filtrar por estado, fecha, mes o rango de fechas (como E4)
   - fecha_relativa (noul): usa fechas relativas a hoy (hoy, ayer, el sábado, la semana pasada)
   - identifica_por_nombre (noul): identifica el registro solo por un nombre de persona o empresa,
     sin código, NIT ni cédula, así que puede haber homónimos (como E5 y E2)
   - destructiva (noul): borra o cambia muchos registros a la vez, o es irreversible
   - incompleta (noul): le falta el registro, el campo o el valor para ejecutarla

2. Crea states_agente.txt, una instrucción por línea: las 5 de pruebas/ tal cual; 8 variantes a mano
   de cada una en el mismo estilo (otros nombres, NIT, teléfonos, ciudades colombianas, direcciones,
   meses, estados y entidades); y 10 más para dos datos, fechas relativas, eliminaciones, masivas,
   incompletas e informales con errores de tipeo.

3. Genera y etiqueta con tag.py:
   --questions questions_agente.json --states states_agente.txt --generate 500
   --output data_agente.jsonl --domain "instrucciones en español que un usuario da para operar
   un sistema de gestión empresarial legacy (clientes, pedidos, facturas, inventario, proveedores,
   empleados), en el estilo de las tareas de pruebas/"
   Antes de la corrida completa, prueba con --generate 20 y muéstrame 5 filas.

4. Entrena: train.py --data data_agente.jsonl --output model_agente

Estoy en Windows con PowerShell. La llave del modelo profesor ya está en variables de entorno.
```