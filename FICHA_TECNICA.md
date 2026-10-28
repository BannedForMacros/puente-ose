# Ficha técnica — Puente TXT → Sovos (Perú)

| | |
|---|---|
| Cliente | Hogar Clínica San Juan de Dios Chiclayo — RUC 20437310841 |
| Función | Enviar a Sovos Compliance Network los comprobantes que el sistema Java genera como TXT |
| Comprobantes | Factura (01), boleta (03), nota de crédito (07), nota de débito (08). Baja: interfaz preparada |
| Tecnología | Python 3.12 · httpx · lxml · pydantic · python-dotenv · SQLite |
| Plataformas | Windows (servicio NSSM) y Linux (systemd) |
| Versión | 1.0.0 |

## 1. Arquitectura

```
 Sistema Java (no se modifica)
        │  RUC-tipo-serie-número.txt
        ▼
 ┌──────────────── sovos_bridge ────────────────────────────────────────┐
 │  Hilo "bandeja" (cada POLL_SECONDS)                                  │
 │   watcher ─► pipeline: parser ─► validation ─► control duplicados    │
 │                        ─► ubl_builder ─► sbd_builder ─► XSD ─► POST  │
 │                                                                      │
 │  Hilo "estados" (cada STATUS_POLL_SECONDS)                           │
 │   status poller: GET notificaciones ─► AP/RE ─► XML/PDF ─► PUT read  │
 │                                                                      │
 │  SQLite (estado único por serie-número)   Logs diarios enmascarados  │
 └──────────────────────────────┬───────────────────────────────────────┘
                                │ HTTPS (Bearer, x-correlationId)
                                ▼
 Sovos Compliance Network ──► mapeo SCI→UBL legal, firma, OSE/SUNAT, PDF
```

| Módulo | Responsabilidad |
|---|---|
| `parser/` | Lee el TXT: detecta encoding (UTF-8, si falla cp1252), separa por `\|` respetando `[...]`, aplica el layout por registro (`layout.py`) |
| `validation/` | Controles mínimos y catálogos SUNAT |
| `sbd/ubl_builder.py` | Arma el UBL 2.1 igual al del puente anterior (sin firma) |
| `sbd/sbd_builder.py` | Sobre SBD de Sovos (encabezado + SovosDocument) |
| `sbd/xsd.py` | Valida el SBD contra los XSD oficiales (`schemas/sovos/`) |
| `sovos/auth.py` | Token intercambiable: OAuth2 client credentials (caché y renovación) o token fijo |
| `sovos/client.py` | Envío, consultas y confirmación con política de reintentos |
| `sovos/responses.py` | Lectura de notificaciones y del ApplicationResponse (Base64) |
| `storage/db.py` | Estados, historial de eventos y notificaciones |
| `service/` | Pipeline, consulta de estados, bandeja, servicio y operaciones de soporte |

## 2. Flujo

```
 TXT en INBOX_DIR
   │ tamaño estable ≥ FILE_STABLE_SECONDS
   ▼
 processing/ ──► ¿nombre válido? ──no──► error/ + .log
   │
   ▼
 Reserva en SQLite (BEGIN IMMEDIATE)
   ├─ ya APROBADO / ENVIADO / EN_PROCESO ───────────► duplicates/ + .log  (no se reenvía)
   ├─ envío anterior incierto ──────────────────────► error/ + .log       (revisión manual)
   ▼
 Parser + controles ──falla──► VALIDACION_ERROR ► error/ + .log
   ▼
 UBL + SBD + XSD ─────falla──► VALIDACION_ERROR ► error/ + .log
   ▼
 POST /v1/documents ──4xx────► VALIDACION_ERROR ► error/ + .log
   │                 ──red/5xx► ERROR_CONEXION  ► error/ + .log (incierto si pudo llegar)
   ▼ 202 documentId
 ENVIADO (TXT queda en processing/)
   ▼ consulta de estado
 EN_PROCESO (IP)
   ├─ AP / CA ─► descarga XML y PDF a output/AAAA/MM/ ─► APROBADO  ► processed/
   ├─ RE ──────► código y mensaje SUNAT ───────────────► RECHAZADO ► error/ + .log
   ├─ RE 404/406 (Sovos ya lo tenía) ──────────────────► DUPLICADO ► duplicates/
   └─ sin respuesta en MAX_STATUS_MINUTES ─────────────► ERROR_CONEXION ► error/ + .log
 PUT /v1/notifications/PE  (confirmación en lote de lo procesado)
```

Al arrancar, el servicio revisa `processing/`. Lo que quedó a medias antes del
envío vuelve a la bandeja. Lo que quedó *durante* el envío se marca como
**envío incierto**.

## 3. Mapeo TXT → UBL (ingeniería inversa)

El mapeo se obtuvo comparando los TXT con los XML que generaba el puente
anterior y que SUNAT aceptó. Los valores se **copian tal cual** del TXT, con
sus decimales y sin recalcular. La prueba `tests/test_ubl_reverse.py` genera
el UBL desde el TXT y verifica que sea igual nodo por nodo al XML aceptado.

| Registro / pos. | Nodo UBL |
|---|---|
| IDE 1 | `cbc:ID` |
| IDE 2 / 3 / 7 | `IssueDate` / `IssueTime` / `DueDate` |
| IDE 4 | `InvoiceTypeCode` (cat. 01) |
| IDE 5 | `DocumentCurrencyCode` y todos los `currencyID` |
| IDE 6 | sin confirmar (vacío en boletas): se advierte en el log si trae valor |
| EMI 1, 2 | Emisor `PartyIdentification/ID@schemeID` |
| EMI 3 / 4 | `RegistrationName` / `PartyName/Name` |
| EMI 5 / 14 | `RegistrationAddress/ID` (ubigeo) / `AddressTypeCode` (anexo) |
| EMI 6 / 7 | `AddressLine/Line` / `CitySubdivisionName` (si trae valor) |
| EMI 8 / 9 / 10 / 11 | `CityName` / `CountrySubentity` / `District` / `Country` |
| REC 1, 2 | Cliente `PartyIdentification/ID@schemeID` (cat. 06) |
| REC 3 / 4 | `RegistrationName` / `AddressLine/Line` |
| REC 5 / 6 / 7 / 8 | `CityName` / `CountrySubentity` / `District` / `Country` |
| REC 9 / 10 | teléfono / correo (no van al UBL) |
| CAB 1-10 | pares código\|monto (cat. 14) → base de cada `TaxSubtotal` |
| CAB 16 `[código\|monto…]` | `TaxTotal/TaxSubtotal` (monto por tributo) |
| CAB 20 | `TaxInclusiveAmount` y `PayableAmount` |
| CAB 23 | `InvoiceTypeCode@listID` (cat. 51) |
| CAB 24 `[código\|texto…]` | `cbc:Note@languageLocaleID` (leyendas) |
| CAB 25 | `TaxTotal/TaxAmount` |
| DET 1 | `InvoiceLine/ID` (sin ceros a la izquierda) |
| DET 2 / 3 | `SellersItemIdentification/ID` / `Item/Description` |
| DET 4 + 5 | `InvoicedQuantity@unitCode` (cat. 03) |
| DET 6 | `Price/PriceAmount` (valor unitario sin IGV) |
| DET 7 | `PricingReference/.../PriceAmount`, PriceTypeCode 01 (02 si es gratuito) |
| DET 8 `[trib\|monto\|afect\|base\|%]` | `InvoiceLine/TaxTotal/TaxSubtotal` (cat. 05 y 07) |
| DET 9 / 14 | `LineExtensionAmount` / `TaxTotal/TaxAmount` del ítem |
| — (factura) | `PaymentTerms` FormaPago = `FORMA_PAGO_DEFECTO` (Contado) |

`LegalMonetaryTotal/LineExtensionAmount` = gravadas + inafectas + exoneradas +
exportación (CAB). Si solo uno tiene monto, se copia su texto.

Base de cada subtotal de cabecera: IGV 1000 ← 1001, INA 9998 ← 1002,
EXO 9997 ← 1003, GRA 9996 ← 1004, EXP 9995 ← 1000.

Diferencias intencionales con el puente anterior:
- Sin `ext:UBLExtensions` (la firma la pone Sovos).
- Texto en UTF-8 escapado una sola vez: `NIÑOS`, no `NI&amp;#209;OS`.
- Se recortan espacios al inicio y final de cada campo.

**Notas de crédito y débito**: UBL nativo `CreditNote` / `DebitNote` con
`DiscrepancyResponse` (motivo cat. 09/10) y `BillingReference`. Los datos del
comprobante afectado vienen del registro **REF provisional**
(`REF|serie-número|tipo|motivo|descripción`), que se ajustará al recibir el TXT
real de una nota. `SOVOS_NOTE_ROOT=Invoice` permite la variante con raíz
Invoice si Sovos la pidiera.

## 4. SBD (Standard Business Document)

Según los XSD oficiales de Sovos (`schemas/sovos/`, v4.1) y la guía de Perú:

```xml
<sbd:StandardBusinessDocument xmlns:sbd="http://www.unece.org/cefact/namespaces/StandardBusinessDocumentHeader"
    xmlns:svs="http://www.sovos.com/namespaces/sovosDocument"
    xmlns:sci="http://www.sovos.com/namespaces/sovosCanonicalInvoice">
  <sbd:StandardBusinessDocumentHeader>…</sbd:StandardBusinessDocumentHeader>
  <svs:SovosDocument><sci:SovosCanonicalInvoice>
      <inv:Invoice>…UBL…</inv:Invoice>
  </sci:SovosCanonicalInvoice></svs:SovosDocument>
</sbd:StandardBusinessDocument>
```

| Nodo | Valor |
|---|---|
| HeaderVersion | 1.0 |
| Sender/Identifier @Authority=PE | 20437310841 (+ ContactInformation/Contact = RUC) |
| Receiver/Identifier @Authority=PE | Documento del cliente (puede ir vacío) |
| DocumentIdentification | Standard `…:Invoice-2`, TypeVersion 2.1, InstanceIdentifier = clave, Type `Invoice`, MultipleType false, CreationDateAndTime (UTC) |
| Scope Country / ProcessType / CompanyCode | PE / Outbound / 20437310841 |
| Scope SenderDocumentId | `RUC-tipo-serie-número` |
| Scope SenderSystemId | `SOVOS_SENDER_SYSTEM_ID` (DefaultSystemERP) |
| Scope BusinessCategory | Serie **B** → BusinessToConsumer · serie **F** → BusinessToBusiness |
| Scope Mapping.OutputSchema / TransformDocument | 01/03: UBLInvoice / SCI-TO-LEGAL_INVOICE · 07: UBLCreditNote / SCI-TO-LEGAL_CREDIT_NOTE · 08: UBLDebitNote / SCI-TO-LEGAL_DEBIT_NOTE |
| Scope BusinessProcess | BusinessServiceName = Default |

La BusinessCategory se decide por la letra de la serie y no por el documento
del cliente, porque hay boletas emitidas a clientes con RUC (BA01-00014480)
que siguen siendo B2C.

Antes de cada envío el SBD se valida contra el XSD. El SBD enviado se guarda
en `WORK_DIR/sbd/AAAA/MM/`.

## 5. Configuración (.env)

| Variable | Defecto | Descripción |
|---|---|---|
| `SOVOS_BASE_URL` | — | URL base de la API de Sovos |
| `SOVOS_AUTH_MODE` | oauth2 | `oauth2` (client credentials) o `static` (token fijo) |
| `SOVOS_TOKEN_URL` | — | Endpoint de token OAuth2 |
| `SOVOS_CLIENT_ID` / `SOVOS_CLIENT_SECRET` | — | Credenciales OAuth2 |
| `SOVOS_SCOPE` | — | Scope OAuth2 (si Sovos lo exige) |
| `SOVOS_STATIC_TOKEN` | — | Token fijo (modo static) |
| `SOVOS_SENDER_SYSTEM_ID` | DefaultSystemERP | Scope SenderSystemId |
| `SOVOS_POLLING_SYSTEM_ID` | CN_SystemERP_Polling | sourceSystemId de la consulta global |
| `SOVOS_STATUS_MODE` | document | `document` (por documentId) o `global` (lista de notificaciones) |
| `SOVOS_NOTE_ROOT` | CreditNote | Raíz UBL de las notas: `CreditNote` o `Invoice` |
| `HTTP_TIMEOUT_SECONDS` | 60 | Timeout por solicitud |
| `HTTP_MAX_RETRIES` | 5 | Reintentos (408/429/5xx/red) |
| `EMISOR_RUC` | 20437310841 | Se exige que el TXT sea de este RUC |
| `FORMA_PAGO_DEFECTO` | Contado | Forma de pago en facturas (hasta mapear el campo del TXT) |
| `INBOX_DIR` | — | Carpeta donde el sistema Java deja los TXT |
| `WORK_DIR` | — | Carpeta de trabajo (ver § 6) |
| `POLL_SECONDS` | 10 | Revisión de la bandeja |
| `STATUS_POLL_SECONDS` | 30 | Consulta de pendientes en Sovos |
| `MAX_STATUS_MINUTES` | 60 | Tiempo máximo esperando AP/RE |
| `FILE_STABLE_SECONDS` | 3 | Tiempo con tamaño estable antes de tomar un TXT |
| `DRY_RUN` | false | `true`: genera el SBD en `dry_run/` sin enviar |
| `LOG_LEVEL` / `LOG_RETENTION_DAYS` | INFO / 90 | Nivel y días de logs |

## 6. Carpetas (WORK_DIR)

| Carpeta | Contenido |
|---|---|
| `processing/` | TXT tomados de la bandeja; quedan aquí mientras Sovos procesa |
| `processed/` | TXT de comprobantes APROBADOS |
| `error/` | TXT con error + `<archivo>.txt.log` con el motivo en español |
| `duplicates/` | TXT repetidos que no se reenviaron + `.log` |
| `output/AAAA/MM/` | `clave.xml` (firmado/legal), `clave.pdf`, `R-clave.xml` (constancia), respuestas de Sovos |
| `sbd/AAAA/MM/` | SBD enviado de cada comprobante |
| `dry_run/` | SBD generados con DRY_RUN |
| `manual/` | Copias de los TXT enviados con el comando `send` |
| `logs/` | `sovos_bridge.log` rotado a medianoche (`sovos_bridge.log.AAAA-MM-DD`) |
| `reports/` | `reporte_AAAA-MM-DD.csv` |
| `db/` | `sovos_bridge.db` (SQLite) |

## 7. Estados

| Estado | Significado | Siguiente paso |
|---|---|---|
| RECIBIDO | Tomado de la bandeja, en proceso local | Automático |
| VALIDACION_ERROR | No pasó los controles, o Sovos rechazó la solicitud (4xx). **No se registró en Sovos** | Corregir y volver a dejar el TXT |
| ENVIADO | Sovos lo recibió (documentId) | Automático |
| EN_PROCESO | Sovos/SUNAT procesando (IP) | Automático |
| APROBADO | Aceptado (AP/CA); XML y PDF en output | Fin |
| RECHAZADO | SUNAT/OSE lo rechazó (RE); código y mensaje guardados | Corregir y volver a dejar el TXT |
| ERROR_CONEXION | Falla de red/Sovos o sin respuesta final | `retry-errors` |
| DUPLICADO | Sovos indica que ya lo tenía | Revisar |

**Garantía de envío único.** La clave `RUC-tipo-serie-número` es única en
SQLite y se reserva antes del POST. No se reenvía nada que esté en ENVIADO,
EN_PROCESO, APROBADO o DUPLICADO, ni en ERROR_CONEXION con documentId. Se puede
reenviar lo que esté en RECHAZADO (SUNAT no lo registró), VALIDACION_ERROR o
ERROR_CONEXION ocurrido antes de transmitir.

**Envío incierto.** Si la conexión se corta después de enviar el POST (timeout
de lectura, HTTP 500/504, caída del servicio durante el envío), no se sabe si
Sovos registró el comprobante. Como Sovos no detecta duplicados, no se
reintenta solo. TI verifica en el portal y usa `retry-errors --forzar` solo si
Sovos no lo tiene.

## 8. Política HTTP

| Respuesta | Consultas / PUT | POST de documento |
|---|---|---|
| 2xx | OK | OK (requiere documentId) |
| 401 | Renueva token y reintenta 1 vez | Igual |
| 403 / 401 persistente | Error de credenciales | ERROR_CONEXION (no incierto) |
| 408, 429 | Espera max(60 s, Retry-After) y reintenta | Igual |
| 502, 503 | Backoff 2, 4, 8… s (máx. 300) | Igual |
| 500, 504 | Backoff | **Incierto**, no reintenta |
| Otros 4xx | Error, no reintenta | VALIDACION_ERROR con la respuesta de Sovos |
| Error de conexión (antes de enviar) | Backoff | Backoff |
| Timeout de lectura / conexión cortada | Backoff | **Incierto**, no reintenta |

Cada solicitud lleva un `x-correlationId` único. Ese ID aparece en el log
cuando hay reintentos, para cruzarlo con soporte de Sovos.

## 9. Respuestas de Sovos

- Estados finales (`sciResponseCode`): **AP**, **CA** (aceptado con
  observación) y **PD** → APROBADO. **RE** → RECHAZADO. IP, AB y UQ son estados
  intermedios.
- `sciCloudStatusCode` 404/406 (procesado previamente / en curso) → DUPLICADO.
  209 = fin del flujo.
- `sciGovtStatusCode` / `sciGovtStatusMessage`: código y mensaje de SUNAT/OSE.
  Se guardan y se escriben en el `.log`.
- `content` (Base64) = ApplicationResponse. Se buscan adjuntos
  `EmbeddedDocumentBinaryObject` (XML legal, PDF; también ZIP) o enlaces `URI`.
  Si solo llega el XML, se espera el PDF hasta `MAX_STATUS_MINUTES`.
- Las notificaciones procesadas se confirman en lote con
  `PUT /v1/notifications/PE`. Si el PUT falla, se reintenta en el siguiente
  ciclo.

## 10. Catálogos SUNAT usados

| Catálogo | Uso |
|---|---|
| 01 Tipo de comprobante | 01, 03, 07, 08 |
| 03 Unidad de medida | Advertencia si no está en la lista habitual (NIU, ZZ, …) |
| 05 Tributos | 1000 IGV, 1016 IVAP, 2000 ISC, 7152 ICBPER, 9995–9999 |
| 06 Documento de identidad | 0, 1, 4, 6, 7, A, B, - |
| 07 Afectación IGV | 10–17, 20, 21, 30–37, 40 (gratuitos → PriceTypeCode 02) |
| 09 / 10 Motivos NC / ND | DiscrepancyResponse/ResponseCode |
| 14 Totales (TXT) | 1001 gravadas, 1002 inafectas, 1003 exoneradas, 1004 gratuitas, 1000 exportación |
| 16 Tipo de precio | 01 / 02 |
| 51 Tipo de operación | `InvoiceTypeCode@listID` (0101 venta interna) |

**Controles.** Detienen el envío: nombre de archivo distinto del contenido,
RUC emisor distinto, serie inválida (4 caracteres, F/B según el tipo),
correlativo no numérico, fecha inválida, cliente sin nombre, sin ítems, sin
total, sin tributos, tributo fuera del catálogo 05, nota sin referencia.

Solo advierten en el log (los montos van tal cual, como en el puente anterior):
diferencias de redondeo o cuadre, DNI/RUC del cliente con formato raro,
unidades o afectaciones fuera de la lista, campos no mapeados que traen valor.

## 11. Seguridad

- Los logs enmascaran el DNI (`******78`), el RUC de persona natural, y el
  nombre, dirección, teléfono y correo del cliente (iniciales `J. P. D.`).
  También tokens Bearer y `client_secret`.
- El token y los secretos nunca se registran. El cliente HTTP no registra
  cabeceras, y el error del servidor de tokens no incluye su cuerpo.
- Los enlaces de descarga a otro dominio se piden sin token.
- La base guarda el documento del cliente enmascarado. Los XML/PDF de `output/`
  sí contienen los datos (son el comprobante legal), así que hay que restringir
  el acceso a `WORK_DIR`.
- `.env` debe ser legible solo por el servicio.

## 12. Pendientes por confirmar

| Tema | Estado actual |
|---|---|
| Método de autenticación de Sovos | OAuth2 client credentials o token fijo, intercambiable |
| TXT real de factura: campo de forma de pago (Contado/Crédito y cuotas) | Constante `FORMA_PAGO_DEFECTO`; IDE 6 candidato |
| TXT real de nota de crédito/débito | Registro REF provisional |
| Raíz UBL de notas dentro del SCI | `CreditNote` (validado con XSD); alternativa `SOVOS_NOTE_ROOT=Invoice` |
| Ruta exacta de XML/PDF en el ApplicationResponse de Perú | Lectura tolerante (embebido, ZIP o enlace) |
| Comunicación de baja | `sbd/baja.py` (interfaz); los TXT RA/RC van a error/ con aviso |
| Campos (?) del TXT: IDE 6, EMI 12-13, CAB 11-15/17-19/21-22, DET 10-13 | Se guardan y se advierte si traen valor |
| Código de producto SUNAT (DET 10-13 probable) | No se envía hasta identificar la posición |

## 13. Solución de errores frecuentes

| Síntoma | Causa probable | Acción |
|---|---|---|
| TXT en `error/`, `.log` "nombre de archivo inválido" | Nombre fuera del patrón `RUC-tipo-serie-número.txt` | Renombrar y volver a dejar en la bandeja |
| `.log` "no coincide con el contenido IDE" | El nombre y el IDE del TXT difieren | Regenerar el TXT desde el sistema |
| `Sovos rechazó la solicitud (HTTP 400)` | SBD no aceptado por Sovos | Ver la respuesta en el `.log`; si es del mapeo, avisar a desarrollo |
| `Autenticación con Sovos` / HTTP 401–403 | Credenciales vencidas o erróneas | Revisar `.env`, reiniciar el servicio, `retry-errors` |
| Muchos `ERROR_CONEXION` | Sovos caído o sin internet | Ver GUIA_OPERACION.md § "Si Sovos no responde" |
| `ENVÍO INCIERTO` | Corte justo durante el envío | Verificar en el portal Sovos; `retry-errors --forzar` solo si no existe |
| RECHAZADO con código 2xxx/3xxx | Error de datos según SUNAT | Corregir en el sistema de facturación y reenviar el TXT |
| Ñ o tildes como `Ã'` en el log | El sistema Java cambió de encoding | El servicio advierte "mojibake"; revisar la configuración regional del sistema Java |
| TXT se queda en la bandeja | Archivo aún abierto por el sistema Java o sin permisos | Ver el log ("aún está en uso"); revisar permisos de la carpeta |
| `No se encontró el XSD de Sovos` | Falta `schemas/sovos` | Copiar la carpeta completa del proyecto |
