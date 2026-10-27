# Prompt: Servicio de integración TXT a Sovos Compliance Network (Perú)

Actúa como desarrollador senior especializado en facturación electrónica peruana. Construye desde cero un servicio de integración listo para producción, con el contexto y requisitos de abajo. Trabaja por etapas, muéstrame el plan antes de programar y pregúntame solo si algo bloquea el avance.

## 1. Contexto

- Cliente: Hogar Clínica San Juan de Dios Chiclayo, RUC 20437310841.
- Un sistema Java (que NO se modifica) genera un archivo TXT por cada comprobante de venta en una carpeta.
- El servicio debe tomar cada TXT, convertirlo al formato de Sovos y enviarlo a Sovos Compliance Network (Indirect Tax API, REST).
- Sovos arma el XML legal, lo firma con el certificado de la clínica, lo envía al OSE y genera el PDF.
- Este servicio reemplaza a un puente anterior que enviaba a Nubefact.
- Comprobantes: factura (01), boleta (03), nota de crédito (07), nota de débito (08) y comunicación de baja.
- En `samples/` están: un TXT real de boleta, un XML UBL 2.1 real de boleta BT06 aceptada y CDR de respuesta (todo anonimizado); ver sección 4.2, que sirve como referencia exacta de nodos y catálogos SUNAT.

## 2. Stack

- Python 3.12, `httpx`, `lxml`, `pydantic`, `python-dotenv`, SQLite.
- Debe correr en Windows como servicio (documentar instalación con NSSM) y también en Linux.
- Sin dependencias pesadas ni frameworks web.

## 3. API de Sovos (según su documentación oficial para Perú)

**Envío**
- `POST {SOVOS_BASE_URL}/v1/documents`
- Headers: `Content-Type: application/json`, `Authorization: Bearer <token>`, `x-correlationId: <uuid único por request>`.
- Body: `{"data": "<SBD en Base64>", "dataEncoding": "base64"}`.
- Respuesta 202 con `data.documentId`. Es asíncrono.

**Consulta de estado**
- `GET {SOVOS_BASE_URL}/v1/documents/PE/{documentId}/notifications?includeAcknowledged=true`
- o `GET {SOVOS_BASE_URL}/v1/notifications/PE?taxId=20437310841&sourceSystemId=CN_SystemERP_Polling&page=1&perPage=50`
- Cada notificación trae `metadata.sciResponseCode`: `IP` en proceso, `AP` aprobado, `RE` rechazado. El proceso termina solo en `AP` o `RE`.
- Errores de SUNAT/OSE vienen en `sciGovtStatusCode` y `sciGovtStatusMessage`.
- `content` viene en Base64 (application response); decodificarlo y extraer los links o el contenido del XML y PDF.

**Confirmación**
- `PUT {SOVOS_BASE_URL}/v1/notifications/PE` con body `[{"status": "read", "notificationId": "<id>"}, ...]`. Confirmar en lote tras procesar.

**Manejo de errores HTTP**
- 4xx: error del cliente, no reintentar sin corregir. Excepto 408 y 429: esperar al menos 60 segundos y reintentar.
- 5xx: reintentar con backoff exponencial.

**Autenticación**
- El método para obtener el token está pendiente de confirmar con Sovos. Implementa un módulo `auth` intercambiable (por ejemplo OAuth2 client credentials con `SOVOS_CLIENT_ID` y `SOVOS_CLIENT_SECRET`), con caché del token y renovación automática al expirar.

**Duplicados**
- Sovos NO detecta duplicados: si se reenvía un comprobante, lo toma como nuevo. El servicio debe garantizar que cada serie-número se envíe una sola vez.

## 4. Estructura del SBD (Standard Business Document)

XML con `sbd:StandardBusinessDocumentHeader` + `svs:SovosDocument` > `sci:SovosCanonicalInvoice` > nodos UBL (sin firma). Namespaces exactos según la documentación de Sovos.

Valores del header:

| Nodo | Valor |
| --- | --- |
| Sender.Identifier (Authority="PE") | 20437310841 |
| Receiver.Identifier (Authority="PE") | Documento del cliente (se puede dejar vacío, pero el nodo debe existir) |
| DocumentIdentification.Standard | urn:oasis:names:specification:ubl:schema:xsd:Invoice-2 |
| DocumentIdentification.TypeVersion | 2.1 |
| DocumentIdentification.Type | Invoice |
| Scope Country | PE |
| Scope ProcessType | Outbound |
| Scope CompanyCode | 20437310841 |
| Scope SenderDocumentId | ID único (usar RUC-tipo-serie-número) |
| Scope SenderSystemId | DefaultSystemERP (configurable) |
| Scope BusinessProcess > BusinessServiceName | Default |
| Scope BusinessCategory | BusinessToConsumer para boletas, BusinessToBusiness para facturas |

Por tipo de documento:

| Tipo | Mapping.OutputSchema | Mapping.TransformDocument |
| --- | --- | --- |
| Factura y boleta | UBLInvoice | SCI-TO-LEGAL_INVOICE |
| Nota de crédito | UBLCreditNote | SCI-TO-LEGAL_CREDIT_NOTE |
| Nota de débito | UBLDebitNote | SCI-TO-LEGAL_DEBIT_NOTE |

La comunicación de baja: deja una interfaz preparada; el formato se confirmará con Sovos.

## 4.1 Formato del TXT de la clínica (analizado de un ejemplo real de boleta)

Un archivo por comprobante. Una línea por registro, campos separados por `|`, primer campo = tipo de registro. Los valores entre corchetes `[a|b|c]` son grupos o listas dentro de un campo: el parser debe respetar los corchetes al separar por `|`. Puede haber varias líneas `DET` (una por ítem).

Ejemplo (datos del paciente anonimizados):

```
IDE|BT05-00125304|2026-10-22|16:22:53|03|PEN||2026-10-22|
EMI|6|20437310841|HOGAR CLINICA SAN JUAN DE DIOS CHICLAYO|HOGAR CLINICA SAN JUAN DE DIOS CHICLAYO|140112|NRO. 10 CAS. PIMENTEL ||CHICLAYO|LAMBAYEQUE|PIMENTEL|PE|||0000
REC|1|00000000|PACIENTE DE PRUEBA|CALLE EJEMPLO 123|LAMBAYEQUE|LAMBAYEQUE|TUCUME|PE|999999999|
CAB|1001|25.42|1002|0.00|1003|0.00|1004|0.00|1000|0.00||||||[1000|4.58]||||30.00||0.00|0101|[1000|TREINTA CON 0/100]|4.58|
DET|001|305073|TERAPIA FISICA NIÑOS - TRAUMATOLOGICA|1.0000000000|NIU|25.4200000000|29.9956000000|[1000|4.58|10|25.42|18.00]|25.42|||||4.58|
```

Mapeo propuesto (validar contra el XML de referencia en `samples/` y confirmar los campos marcados con (?)):

**IDE (identificación)**
| Pos | Valor ejemplo | Significado | UBL |
| --- | --- | --- | --- |
| 1 | BT05-00125304 | Serie-correlativo | cbc:ID |
| 2 | 2026-10-22 | Fecha de emisión | cbc:IssueDate |
| 3 | 16:22:53 | Hora de emisión | cbc:IssueTime |
| 4 | 03 | Tipo de comprobante (cat. 01) | cbc:InvoiceTypeCode |
| 5 | PEN | Moneda | cbc:DocumentCurrencyCode |
| 6 | (vacío) | (?) posible orden de compra u observación | |
| 7 | 2026-10-22 | Fecha de vencimiento | cbc:DueDate |

**EMI (emisor)**
| Pos | Valor ejemplo | Significado | UBL |
| --- | --- | --- | --- |
| 1 | 6 | Tipo de documento (RUC) | schemeID |
| 2 | 20437310841 | RUC | PartyIdentification/ID |
| 3 | HOGAR CLINICA... | Razón social | RegistrationName |
| 4 | HOGAR CLINICA... | Nombre comercial | PartyName/Name |
| 5 | 140112 | Ubigeo | RegistrationAddress/ID |
| 6 | NRO. 10 CAS. PIMENTEL | Dirección | AddressLine/Line |
| 7 | (vacío) | (?) urbanización | |
| 8 | CHICLAYO | Provincia | CityName |
| 9 | LAMBAYEQUE | Departamento | CountrySubentity |
| 10 | PIMENTEL | Distrito | District |
| 11 | PE | País | Country/IdentificationCode |
| 12-13 | (vacíos) | (?) teléfono y correo | |
| 14 | 0000 | Código de establecimiento anexo | AddressTypeCode |

**REC (receptor / cliente)**
| Pos | Valor ejemplo | Significado | UBL |
| --- | --- | --- | --- |
| 1 | 1 | Tipo de documento (cat. 06: 1 DNI, 6 RUC, etc.) | schemeID |
| 2 | 00000000 | Número de documento | PartyIdentification/ID |
| 3 | PACIENTE DE PRUEBA | Nombre o razón social | RegistrationName |
| 4 | CALLE EJEMPLO 123 | Dirección | AddressLine/Line |
| 5 | LAMBAYEQUE | Provincia | CityName |
| 6 | LAMBAYEQUE | Departamento | CountrySubentity |
| 7 | TUCUME | Distrito | District |
| 8 | PE | País | Country/IdentificationCode |
| 9 | 999999999 | Teléfono (no va al UBL) | |
| 10 | (vacío) | (?) correo | |

**CAB (totales de cabecera).** Los primeros campos son pares código|monto:
| Pos | Valor ejemplo | Significado |
| --- | --- | --- |
| 1-2 | 1001, 25.42 | Total operaciones gravadas |
| 3-4 | 1002, 0.00 | Total operaciones inafectas |
| 5-6 | 1003, 0.00 | Total operaciones exoneradas |
| 7-8 | 1004, 0.00 | Total operaciones gratuitas |
| 9-10 | 1000, 0.00 | (?) confirmar qué total es |
| 11-15 | (vacíos) | (?) descuentos, anticipos u otros |
| 16 | [1000\|4.58] | Lista de tributos: código (1000 IGV) y monto → TaxTotal/TaxSubtotal |
| 17-19 | (vacíos) | (?) |
| 20 | 30.00 | Importe total → PayableAmount y TaxInclusiveAmount |
| 21 | (vacío) | (?) |
| 22 | 0.00 | (?) descuento global u otros cargos |
| 23 | 0101 | Tipo de operación (cat. 51, venta interna) → listID del InvoiceTypeCode |
| 24 | [1000\|TREINTA CON 0/100] | Leyendas: código 1000 = monto en letras → cbc:Note languageLocaleID="1000" |
| 25 | 4.58 | Total de impuestos → TaxTotal/TaxAmount |

**DET (detalle, una línea por ítem)**
| Pos | Valor ejemplo | Significado | UBL |
| --- | --- | --- | --- |
| 1 | 001 | Número de ítem | InvoiceLine/ID |
| 2 | 305073 | Código interno del servicio | SellersItemIdentification/ID |
| 3 | TERAPIA FISICA NIÑOS... | Descripción | Item/Description |
| 4 | 1.0000000000 | Cantidad | InvoicedQuantity |
| 5 | NIU | Unidad de medida (cat. 03) | unitCode |
| 6 | 25.4200000000 | Valor unitario sin IGV | Price/PriceAmount |
| 7 | 29.9956000000 | Precio unitario con IGV | PricingReference (PriceTypeCode 01) |
| 8 | [1000\|4.58\|10\|25.42\|18.00] | Tributo del ítem: código, monto, afectación IGV (cat. 07), base, porcentaje | InvoiceLine/TaxTotal |
| 9 | 25.42 | Valor de venta del ítem | LineExtensionAmount |
| 10-13 | (vacíos) | (?) descuentos o código de producto SUNAT | |
| 14 | 4.58 | Total de impuestos del ítem | InvoiceLine/TaxTotal/TaxAmount |

Notas para el parser y el armado:
- El TXT trae la Ñ correctamente. El XML que generaba el puente anterior la escapaba dos veces (`NI&amp;#209;OS`): escribir el texto tal cual en UTF-8 y dejar que la librería XML lo escape una sola vez.
- Precio con IGV 29.9956 × 1 da 29.9956 contra un total de 30.00: diferencia de redondeo dentro de la tolerancia de SUNAT, pero registrarla en el log.
- Desde el 2026-08-01 SUNAT exige código de producto en ciertos casos: revisar si alguno de los campos vacíos de DET corresponde a ese dato.
- Los campos marcados con (?) se confirman con los demás ejemplos (factura, notas, detracción, crédito). Diseña el parser por posición pero con un diccionario de layout por tipo de registro, fácil de ajustar.

## 4.2 Contenido de `samples/` y series detectadas

- `samples/txt/`: TXT real de boleta (BT05), anonimizado. Nombre del archivo = `RUC-tipo-serie-correlativo.txt`; usar ese patrón para detectar el tipo antes de parsear.
- `samples/xml/`: XML UBL 2.1 firmado de una boleta BT06 aceptada (referencia de nodos y catálogos). Su firma es del puente anterior: no reutilizarla, Sovos firma.
- `samples/cdr/`: CDR de respuestas aceptadas (código 0) de una boleta y tres notas de crédito. Solo sirven para conocer series y el formato de respuesta; validados por Nubefact, el OSE anterior.

Series vistas en producción:

| Serie | Tipo | Nota |
| --- | --- | --- |
| BT05, BT06 | Boleta (03) | Varias series de boleta en paralelo (probablemente por caja o sede) |
| BC01, BC07 | Nota de crédito (07) sobre boleta | Cliente con DNI |
| FC01 | Nota de crédito (07) sobre factura | Cliente con RUC |

Implicancias:
- El servicio debe aceptar cualquier serie; no fijarlas en código. Validar solo el formato: inicia con F (factura y sus notas) o B (boleta y sus notas), 4 caracteres.
- Una nota de crédito sobre boleta (BCxx) y sobre factura (FCxx) usan `UBLCreditNote` en ambos casos; la BusinessCategory se decide por el tipo de documento del cliente (DNI = BusinessToConsumer, RUC = BusinessToBusiness).
- Aún faltan TXT de factura, nota de crédito y nota de débito: el parser de notas debe dejar previsto el bloque del documento de referencia (serie-número del comprobante afectado, su tipo y el motivo del catálogo 09) hasta recibir el ejemplo.

## 5. Flujo del servicio

1. Vigilar `INBOX_DIR` cada N segundos (configurable). Ignorar archivos que aún se están escribiendo (verificar tamaño estable).
2. Mover el TXT a `processing/`.
3. Detectar encoding (el sistema Java puede usar Windows-1252) y leer como texto correcto. Corregir caracteres como Ñ y tildes: el XML anterior tenía el error `NI&amp;#209;OS`; no debe repetirse.
4. Parsear a un modelo Pydantic (cabecera, cliente, ítems, impuestos, totales, documento de referencia para notas).
5. Validar: RUC de 11 dígitos o DNI de 8, serie y correlativo, suma de ítems vs totales, IGV 18% con tolerancia de redondeo de 0.01, catálogos SUNAT (tipo de documento, afectación IGV, unidad de medida). Código de producto si aplica.
6. Verificar en SQLite que la serie-número no se haya enviado antes. Si ya fue aprobado, mover a `duplicates/` y registrar.
7. Construir el SBD, validar que sea XML bien formado, codificar en Base64 y enviar.
8. Guardar `documentId`, estado `ENVIADO` y hora.
9. Un proceso de consulta revisa los pendientes hasta `AP` o `RE`, con límite de intentos y tiempo configurable.
10. En `AP`: descargar XML firmado y PDF a `output/AAAA/MM/`, mover TXT a `processed/`.
11. En `RE`: guardar código y mensaje, mover TXT a `error/` con un `.log` al lado explicando el motivo en español claro.
12. Confirmar las notificaciones procesadas con el PUT.

Estados en base de datos: RECIBIDO, VALIDACION_ERROR, ENVIADO, EN_PROCESO, APROBADO, RECHAZADO, ERROR_CONEXION, DUPLICADO.

## 6. Configuración (.env)

```
SOVOS_BASE_URL=
SOVOS_CLIENT_ID=
SOVOS_CLIENT_SECRET=
SOVOS_SENDER_SYSTEM_ID=DefaultSystemERP
SOVOS_POLLING_SYSTEM_ID=CN_SystemERP_Polling
EMISOR_RUC=20437310841
INBOX_DIR=
WORK_DIR=
POLL_SECONDS=10
STATUS_POLL_SECONDS=30
MAX_STATUS_MINUTES=60
DRY_RUN=false
```

`DRY_RUN=true` genera el SBD en disco sin enviarlo, para revisar el mapeo.

## 7. Calidad y seguridad

- Logs rotativos diarios. Enmascarar DNI y nombres de pacientes en los logs (son datos de salud de una clínica).
- Nunca registrar el token ni secretos.
- Pruebas con `pytest`: parser con los TXT de `samples/`, validaciones, construcción del SBD por tipo, control de duplicados, manejo de 408/429/5xx con un servidor simulado.
- Comando CLI: `run` (servicio), `send <archivo>` (uno solo), `status <serie-numero>`, `retry-errors`, `report --date AAAA-MM-DD` (resumen del día en CSV).

## 8. Entregables

1. Código completo con estructura clara (`parser/`, `validation/`, `sbd/`, `sovos/`, `storage/`, `service/`).
2. `README.md` con instalación en Windows (Python + NSSM) y Linux (systemd).
3. `FICHA_TECNICA.md`: arquitectura, diagrama del flujo, estructura de carpetas, configuración, estados, catálogos usados y solución de errores frecuentes.
4. `GUIA_OPERACION.md` para capacitar al personal de TI: cómo revisar estados, reintentar errores, leer los logs y qué hacer si Sovos no responde.

Empieza revisando `samples/` y proponiéndome el mapeo campo por campo del TXT al UBL antes de escribir el código.
