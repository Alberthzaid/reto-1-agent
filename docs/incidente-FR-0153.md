# Incidente FR-0153: SIGECOM cambió de interfaz (v3.9 → v4.2)

**Prioridad:** alta. **Estado:** agente adaptado; faltan las corridas de validación (ver [Resultados](#resultados)).

El cliente actualizó SIGECOM a un tema nuevo sin aviso previo. Hay un modal nuevo al abrir la aplicación, la barra de acciones cambió de lugar y los botones tienen otros nombres. Este documento resume qué cambió, cómo lo detectamos, por qué falló el agente y qué adaptamos. Sirve también de guion para el video.

## Qué cambió

| Elemento | Antes (v3.9) | Ahora (v4.2) |
|---|---|---|
| Al abrir la app | Sin modal | Modal "Novedades de la versión 4.2" con un solo botón **"Entendido"** |
| Texto del modal | — | "Los formularios ahora se aplican sin cerrarse, para que pueda seguir trabajando sobre el mismo registro. La barra de acciones pasó a la derecha. Nuevos nombres en los botones." |
| Lista de clientes | Botón "Nuevo cliente" | Barra a la derecha: **"Exportar" \| "Crear cliente"**. "Crear cliente" solo **abre** el formulario |
| Botones del formulario | "Guardar" / "Volver" | **"Cerrar" \| "Aplicar"** (texto claro sobre botón verde) |
| Efecto de guardar | Guarda y cierra el formulario | "Aplicar" guarda y **deja el formulario abierto**; el título pasa de "Crear cliente" a "Cliente CL-0141" |
| Pulsar guardar dos veces | — | **Cada "Aplicar" en un formulario de alta crea un registro nuevo** (1.º → CL-0141, 2.º → CL-0142, duplicado) |
| Confirmación visual | Aviso "… guardado" | Aviso azul oscuro **"Cambios aplicados"** abajo a la derecha |
| Tiempo de repintado | Inmediato | Lento: a los 0,3 s no cambia nada; a ~1,5 s cambian el título y aparece el aviso |

## Cómo lo detectamos

1. **Reportes de `dev_tools/run_tests`** (`data/reports/2026-10-10_155443.json`, `…155517.json`, `…160916.json`):
   - E1 (crear cliente) quedó `fallida` en las 3 corridas. En 2 de ellas `/__check` marcó "sin registros duplicados" = `false` aunque "cambios esperados" = `true`: el cliente final era **CL-0142**, así que antes se había creado otro.
   - E2 (modificar dirección) quedó `fallida` aunque `/__check` dio `pass: true`. El verificador no encontró evidencia del guardado.
2. **Trazas del agente** (`data/traces/<id>/actions.jsonl` y una captura por paso):
   - `5ddca167…`: el clic en "Crear cliente" se leyó como `'ortar || Crear cliente | |'` (el botón vecino "Exportar" entró en el recorte) y **contó como la única confirmación planeada**. Después, el "Aplicar" real ya no tenía cupo.
   - `45338054…` y `ad27cb22…`: el clic en "Aplicar" se leyó como `'errar |'`, es decir, el botón vecino "Cerrar". La guardia no lo vio como confirmación y no se guardó la evidencia del aviso.
   - En las tres trazas, los menús se leen mezclados (`'chivo Comercial Carte'`): el recorte fijo de 180 × 34 px abarcaba varios elementos.
3. **Exploración guionizada** con `dev_tools/explore_ui.py` (capturas en `data/screens/explore/`): `00_inicio.png` (modal), `04_lista_clientes.png` (barra a la derecha), `05_crear_cliente.png`, `11_aplicar_inmediato.png` (0,3 s: sin cambios), `12_aplicar_asentado.png` (título "Cliente CL-0141" y aviso "Cambios aplicados"), `13_aplicar_otra_vez.png` (segundo "Aplicar" → CL-0142).

## Causas raíz

1. **La guardia leía el botón vecino.** El recorte fijo de 180 × 34 px alrededor del clic incluía el botón de al lado. Por eso contó "Crear cliente" como confirmación y no reconoció "Aplicar".
2. **Abrir se confundía con guardar.** "Crear" era palabra de confirmación, aunque en v4.2 "Crear cliente" solo abre el formulario.
3. **Un commit sin cambio visible se perdonaba.** La regla "un clic de confirmación que no cambia la pantalla no cuenta" chocó con el repintado lento de v4.2: el agente veía la pantalla igual, la guardia no contaba el clic y permitía repetirlo. El segundo "Aplicar" creaba el duplicado.
4. **El verificador no tenía evidencia.** El aviso dice "Cambios aplicados" (no "guardado") y, como "Aplicar" no se detectó como commit, no se guardó la captura posterior.
5. **Memoria de otra versión.** Los procedimientos aprendidos en v3.9 ("Nuevo cliente", "Guardar") se seguían ofreciendo como pistas.

## Qué adaptamos

| # | Cambio | Dónde |
|---|---|---|
| 1 | **Detección del cambio de interfaz.** ORIENT lee en código, del OCR del encabezado, el nombre de la app, la versión (`v4.2`) y las etiquetas del menú. Cada procedimiento guardado lleva la versión en que se aprendió (columna nueva `app_version`, migración automática de la tabla). Si la versión cambió, se registra en el log y en la traza `cambio de interfaz detectado: v3.9 → v4.2`, los procedimientos viejos no se pasan al modelo y lo nuevo se guarda con la versión actual. | `agent/domain/app_profile.py`, `agent/domain/policy.py` (`Orientation.version`), `agent/application/worker.py`, `agent/domain/models.py` (`Procedure.app_version`), `agent/infra/sqlite_memory.py` |
| 2 | **Cerrar un diálogo no es un commit.** "Entendido" y "Cerrar" son palabras de cierre. "Aceptar", "Sí" o "Confirmar" sin un commit previo y sin datos escritos también se tratan como cierre. Después de un commit siguen contando como su confirmación. | `agent/domain/guard.py`, `agent/domain/prompts/system.md` |
| 3 | **Etiqueta del botón exacto.** Se reemplazó el recorte fijo por la detección del rectángulo del botón por color: desde el clic se toma el color de la cara del botón y se expande por píxeles vecinos parecidos (pasa degradés, se detiene en bordes y texto), con tope de 320 × 60 px y mínimo de 24 × 14. Solo ese rectángulo se lee con OCR. Respaldo: recorte angosto de 100 × 28 px; último recurso: el OCR de pantalla completa. | `agent/domain/tools.py` (`button_bounds`, `ToolBox._target_boxes`) |
| 4 | **Abrir no es guardar.** Palabras fuertes: guardar, grabar, aplicar, confirmar, eliminar, borrar, anular, enviar, exportar, actualizar. "Crear", "nuevo" y "registrar" cuentan solo si se escribieron datos desde la última navegación; si el clic solo reemplaza la pantalla sin aviso de éxito, no cuenta. | `agent/domain/guard.py`, `agent/domain/tools.py` |
| 5 | **El commit cuenta al hacer clic, siempre.** Se eliminó la regla del clic sin cambio. Tras un commit se espera más (hasta 3,5 s, silencio de 0,4 s) antes de observar. Los rechazos indican esperar o hacer zoom y revisar el título o el aviso en lugar de repetir. Además se bloquea un segundo commit en un formulario cuyo título ya muestra el código asignado (p. ej. CL-0141) si no se cambiaron datos. | `agent/domain/guard.py`, `agent/domain/tools.py` (`COMMIT_SETTLE`) |
| 6 | **Detección del commit por su resultado.** Tras cualquier clic se leen hasta 3 regiones chicas que cambiaron. Si aparece un aviso de éxito (guardado, creado, actualizado, eliminado, exportado, aplicado(s), registrado…) o un título con un código nuevo, el clic se registra como commit y se guarda la evidencia para el verificador. Un clic repetido sobre ese mismo botón queda sujeto a los límites de commits. | `agent/domain/tools.py` |
| 7 | **Prompts.** `system.md`: cerrar primero los modales informativos; tras guardar el formulario puede quedar abierto; nunca pulsar guardar/aplicar dos veces; confirmar leyendo el título o el aviso; cerrar con "Cerrar". `verify.md`: el título con el código asignado y los valores visibles, o un aviso "Cambios aplicados"/"guardado" capturado tras el commit, cuentan como confirmación. | `agent/domain/prompts/system.md`, `plan.md`, `verify.md` |

Todos los cambios tienen pruebas sin red: `tests/test_app_profile.py`, `tests/test_guard.py`, `tests/test_tools_fr0153.py` (imágenes sintéticas de "Cerrar" | "Aplicar", "Exportar" | "Crear cliente" y un botón azul con texto blanco), `tests/test_sqlite_memory.py`, `tests/test_policy.py` y `tests/test_worker.py`.

## Resultados

Antes: corridas sobre v4.2 del 10/10/2026 (15:54–16:09). Después: pendiente de correr con el agente adaptado.

| Prueba | Antes: estado | Antes: `/__check` | Antes: observación | Después: estado | Después: `/__check` |
|---|---|---|---|---|---|
| E1 (crear cliente) | fallida (3/3) | `false` en 2/3 (duplicado CL-0142) | "Crear cliente" contó como commit; "Aplicar" leído como `'errar \|'` | pendiente | pendiente |
| E2 (modificar dirección) | fallida | `true` | Falso negativo: sin evidencia de "Cambios aplicados" | pendiente | pendiente |
| E3 | sin corrida en v4.2 | — | — | pendiente | pendiente |
| E4 | sin corrida en v4.2 | — | — | pendiente | pendiente |
| E5 | sin corrida en v4.2 | — | — | pendiente | pendiente |

## Cómo reproducir

1. Documentar la interfaz (muta la app con datos de prueba y reinicia con `POST /__reset` antes y después):

   ```bash
   uv run python -m dev_tools.explore_ui --out data/screens/explore
   ```

2. Correr las pruebas contra el agente local (cada prueba arranca con `/__reset`):

   ```bash
   AGENT_URL=http://127.0.0.1:8001 uv run python -m dev_tools.run_tests --only E1 E2 E3 E4 E5
   ```

3. Revisar en `data/traces/<id>/actions.jsonl` que:
   - el modal se cerró sin gastar el cupo de commits;
   - "Crear cliente" no contó como commit;
   - "Aplicar" contó una sola vez y con evidencia;
   - aparece `cambio de interfaz detectado` cuando hay procedimientos de otra versión.
4. Pruebas unitarias: `uv run pytest -q`.
