# Guía de operación — Puente Sovos (para el personal de TI)

Esta guía explica cómo saber si los comprobantes están llegando a SUNAT, qué
hacer cuando algo falla y cómo leer los logs. No se necesita saber programar.

> En los ejemplos, `WORK_DIR` es la carpeta de trabajo configurada en el
> archivo `.env` (por ejemplo `C:\sovos_bridge`). Los comandos se ejecutan en
> PowerShell, desde la carpeta del programa:
>
> ```powershell
> cd C:\sovos_bridge\app
> .\.venv\Scripts\python -m sovos_bridge <comando>
> ```

## 1. Cómo funciona, en simple

1. El sistema de facturación deja un TXT por comprobante en la **bandeja**.
2. El servicio lo toma, lo convierte y lo envía a **Sovos**.
3. Sovos lo firma, lo envía a **SUNAT** y responde *aprobado* o *rechazado*.
4. El servicio guarda el **XML y el PDF** y archiva el TXT.

Cada comprobante se envía **una sola vez**. Si el mismo TXT vuelve a aparecer,
no se reenvía: va a `duplicates/`.

## 2. Revisión diaria (5 minutos)

1. **El servicio está corriendo**: abrir *Servicios* de Windows y buscar
   "Puente Sovos", o ejecutar `nssm status SovosBridge` (debe decir
   `SERVICE_RUNNING`).
2. **La bandeja está vacía o casi**: los TXT no deben acumularse en la carpeta
   de entrada.
3. **Revisar `WORK_DIR\error\`**: cada TXT trae al lado un `.log` que explica
   el problema. Si está vacía, todo está bien.
4. **Reporte del día anterior**:

   ```powershell
   .\.venv\Scripts\python -m sovos_bridge report --date 2026-10-27
   ```

   Muestra cuántos comprobantes hay en cada estado y genera
   `WORK_DIR\reports\reporte_2026-10-27.csv`, que se abre con Excel.

## 3. Qué significa cada carpeta

| Carpeta | Qué hay | ¿Hay que hacer algo? |
|---|---|---|
| bandeja (`INBOX_DIR`) | TXT recién generados | No, el servicio los toma solo |
| `processing\` | Enviados, esperando respuesta de SUNAT | No, salvo que queden más de 1 hora |
| `processed\` | **Aprobados** | No |
| `output\AAAA\MM\` | XML firmado y PDF de cada aprobado | Es lo que se entrega al cliente |
| `error\` | Con problema; ver el `.log` de al lado | **Sí**, ver § 5 |
| `duplicates\` | Repetidos, no se reenviaron | Solo revisar de vez en cuando |
| `logs\` | Registro del servicio, uno por día | Para investigar |

## 4. Consultar un comprobante

```powershell
.\.venv\Scripts\python -m sovos_bridge status BT05-00125304
```

Muestra el estado, el código y mensaje de SUNAT, dónde quedaron el TXT, el XML
y el PDF, y el historial completo con fecha y hora. Si está pendiente, con
`--consultar` se pregunta a Sovos en ese momento:

```powershell
.\.venv\Scripts\python -m sovos_bridge status BT05-00125304 --consultar
```

| Estado | Qué significa |
|---|---|
| ENVIADO / EN_PROCESO | Sovos lo tiene; SUNAT aún no responde. Normal por unos minutos |
| **APROBADO** | Aceptado por SUNAT. XML y PDF en `output\` |
| **RECHAZADO** | SUNAT lo rechazó. El motivo está en el `.log` y en `status` |
| VALIDACION_ERROR | No se envió: el TXT tiene un problema (ver `.log`) |
| ERROR_CONEXION | No se pudo hablar con Sovos, o Sovos no respondió a tiempo |
| DUPLICADO | Sovos ya lo tenía |

## 5. Qué hacer con los archivos en `error\`

Abrir el `.log` que está al lado del TXT (con el Bloc de notas). La línea
**Motivo** dice el tipo de problema:

### a) "El comprobante no pasó los controles y NO se envió a Sovos"
El TXT tiene un dato faltante o incoherente, y el detalle dice cuál. Sovos no
lo recibió, así que no hay riesgo de duplicado.
1. Corregir el comprobante en el sistema de facturación y regenerar el TXT, o
   avisar al área responsable.
2. Una vez corregido, dejar el TXT nuevo en la bandeja.
3. Borrar el TXT viejo y su `.log` de `error\`.

### b) "Comprobante RECHAZADO"
SUNAT lo rechazó. El `.log` trae el **código SUNAT** (por ejemplo 2800) y el
mensaje.
1. Corregir el dato en el sistema de facturación.
2. Dejar el TXT corregido en la bandeja: se puede reenviar con el mismo número,
   porque SUNAT no lo registró. Si el TXT corregido quedó en `error\`, usar
   `retry-errors --todos`.
3. Si el `.log` dice *"Sovos indica que se debe emitir un comprobante nuevo"*,
   emitir un comprobante con **otro número**.

### c) "Error de conexión con Sovos" (Sovos no recibió el comprobante)
Pasa cuando falta internet o Sovos está caído. Una vez resuelto:

```powershell
.\.venv\Scripts\python -m sovos_bridge retry-errors
```

Devuelve esos TXT a la bandeja y se envían solos.

### d) "Sovos no dio respuesta final a tiempo"
Sovos **sí** tiene el comprobante, pero SUNAT no respondió en el tiempo
configurado. **No reenviarlo como nuevo.** Ejecutar `retry-errors`: el servicio
retoma la consulta de estado, no reenvía.

### e) "ENVÍO INCIERTO"
La conexión se cortó justo mientras se enviaba, y no se sabe si Sovos lo
recibió. Es el único caso que requiere revisar a mano:
1. Entrar al portal de Sovos y buscar el comprobante por serie-número.
2. **Si existe en Sovos: no hacer nada más** y avisar a desarrollo para
   registrar el documentId.
3. **Si NO existe**: ejecutar

   ```powershell
   .\.venv\Scripts\python -m sovos_bridge retry-errors --forzar
   ```

> Nunca usar `--forzar` sin revisar antes el portal: si Sovos ya tenía el
> comprobante, se crearía uno duplicado.

### Resumen de `retry-errors`

| Comando | Qué reintenta |
|---|---|
| `retry-errors` | Errores de conexión (no se envió) y comprobantes sin respuesta a tiempo |
| `retry-errors --todos` | Lo anterior + VALIDACION_ERROR y RECHAZADO (después de corregir el TXT en `error\`) |
| `retry-errors --forzar` | Lo anterior + envíos inciertos (solo después de verificar en el portal) |

## 6. Si Sovos no responde

Síntomas: muchos TXT en `error\` con "Error de conexión", o en el log
mensajes como `Sin conexión con Sovos` o `HTTP 503`.

1. **No reiniciar ni borrar nada.** El servicio ya reintenta solo: espera 60 s
   si Sovos dice que está saturado (429), y espera cada vez más (2, 4, 8…
   segundos) si Sovos falla.
2. Verificar internet desde el servidor: abrir en el navegador la URL de
   `SOVOS_BASE_URL` del `.env`.
3. Consultar con soporte de Sovos si hay una caída. El `x-correlationId` que
   aparece en el log identifica cada solicitud.
4. Cuando Sovos vuelva, ejecutar `retry-errors`.
5. Mientras tanto, los TXT nuevos que lleguen se intentan enviar normalmente.
   Si fallan quedan en `error\` y se recuperan con el mismo `retry-errors`.

SUNAT da unos días de plazo para enviar los comprobantes (confirmar el plazo
vigente con contabilidad). Una caída de horas no es un problema si después se
reintenta.

## 7. Leer los logs

Están en `WORK_DIR\logs\`. El del día es `sovos_bridge.log` y los anteriores
`sovos_bridge.log.AAAA-MM-DD`. Se guardan 90 días. Cada línea tiene este
formato:

```
2026-10-28 14:41:33 WARNING [bandeja] sovos_bridge.service.pipeline: 20437310841-03-BA01-00014481: Redondeo: ...
   fecha y hora       nivel   hilo      parte del programa             comprobante: mensaje
```

| Nivel | Significado |
|---|---|
| INFO | Funcionamiento normal (procesado, enviado, aprobado) |
| WARNING | Aviso, **no detiene** el envío (p. ej. redondeos de céntimos, que SUNAT acepta) |
| ERROR | El comprobante no avanzó; revisar `error\` |

Para buscar un comprobante en el log, abrirlo con el Bloc de notas y buscar
(Ctrl+B) la serie-número, por ejemplo `BT05-00125304`.

Los DNI y nombres de pacientes aparecen ocultos (`******78`, `J. P. D.`) por
tratarse de datos de salud. Para ver los datos completos, usar el comprobante
en `output\` o el sistema de facturación.

Mensajes frecuentes:

| Mensaje | Qué hacer |
|---|---|
| `Redondeo: ... diferencia 0.01` | Nada, es normal |
| `posición N trae un valor no mapeado` | Avisar a desarrollo: el sistema empezó a enviar un dato nuevo |
| `aún está en uso` | Nada, el sistema Java está escribiendo el TXT |
| `parece tener caracteres mal codificados` | Revisar que la Ñ y las tildes salgan bien en el PDF; avisar a desarrollo |
| `Sovos respondió 401; se renueva el token` | Nada si es ocasional; si se repite, revisar credenciales |

## 8. Reiniciar o detener el servicio

```powershell
nssm restart SovosBridge
nssm stop SovosBridge
nssm start SovosBridge
```

Detener el servicio es seguro: lo pendiente se retoma al arrancar. Si se
apaga justo durante un envío, ese comprobante queda como *envío incierto*
(ver § 5e).

## 9. Probar sin enviar (modo de prueba)

Para revisar cómo quedaría un comprobante sin mandarlo a Sovos:

```powershell
.\.venv\Scripts\python -m sovos_bridge send C:\ruta\comprobante.txt --dry-run
```

El resultado queda en `WORK_DIR\dry_run\`. El archivo original no se mueve.

## 10. Lo que nunca hay que hacer

- Borrar o editar `WORK_DIR\db\sovos_bridge.db`: es el control que evita
  duplicados.
- Copiar a la bandeja un TXT de `processed\` "para reenviarlo": irá a
  `duplicates\` y no se enviará, y así debe ser.
- Usar `retry-errors --forzar` sin revisar antes el portal de Sovos.
- Compartir el archivo `.env`: contiene las credenciales de Sovos.
