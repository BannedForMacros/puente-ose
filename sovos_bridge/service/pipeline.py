"""Procesamiento de un TXT: lectura, controles, SBD y envío a Sovos."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sovos_bridge.config import Settings
from sovos_bridge.logging_setup import mask_doc, register_sensitive
from sovos_bridge.parser import ParseError, parse_file, parse_filename
from sovos_bridge.sbd import SbdOptions, SchemaError, build_sbd, serialize, to_base64, validate_sbd
from sovos_bridge.service import files
from sovos_bridge.sovos import SovosAuthError, SovosClient, SovosClientError, SovosConnectionError
from sovos_bridge.storage import Database, Estado
from sovos_bridge.validation import validate

log = logging.getLogger(__name__)


@dataclass
class Result:
    estado: str
    clave: str = ""
    mensaje: str = ""
    path: Path | None = None


class Pipeline:
    def __init__(self, settings: Settings, db: Database, client: SovosClient | None):
        self.s = settings
        self.db = db
        self.client = client
        self.opts = SbdOptions(
            sender_system_id=settings.sovos_sender_system_id,
            note_root=settings.sovos_note_root,
            forma_pago_defecto=settings.forma_pago_defecto,
        )

    # ------------------------------------------------------------------------
    def process(self, src: Path) -> Result:
        """Procesa un TXT (de la bandeja o indicado por el comando `send`)."""
        work = files.move(src, self.s.processing_dir)
        log.info("Procesando %s", work.name)

        try:
            info = parse_filename(src.name)
        except ParseError as exc:
            return self._reject_file(work, "Nombre de archivo inválido", [str(exc)])
        if info.es_baja:
            return self._reject_file(work, "Comunicación de baja / resumen sin formato confirmado",
                                     ["El formato de baja para Sovos aún no está confirmado. "
                                      "Gestionar la baja manualmente en el portal de Sovos."])
        clave = info.clave

        # --- control de duplicados -------------------------------------------
        if self.s.dry_run:
            prev = self.db.get(clave)
            if prev:
                log.warning("[DRY_RUN] %s ya existe en la base con estado %s", clave, prev["estado"])
        else:
            reserva = self.db.reserve(clave, tipo=info.tipo, serie_numero=info.serie_numero, archivo=str(work))
            if reserva.accion == "duplicado":
                dest = files.move(work, self.s.duplicates_dir)
                files.write_error_log(dest, "Comprobante ya enviado a Sovos (duplicado)", [
                    f"{info.serie_numero} ya figura con estado {reserva.estado_previo}"
                    + (f" (documentId {reserva.document_id})" if reserva.document_id else ""),
                    "No se reenvía: Sovos no detecta duplicados y lo registraría como un comprobante nuevo.",
                ])
                log.warning("%s duplicado (estado %s); movido a duplicates/", clave, reserva.estado_previo)
                return Result(Estado.DUPLICADO, clave, "duplicado", dest)
            if reserva.accion == "incierto":
                dest = files.move(work, self.s.error_dir)
                files.write_error_log(dest, "Envío anterior incierto: revisar en Sovos antes de reenviar", [
                    "Un envío anterior de este comprobante se cortó sin confirmación de Sovos.",
                    "Verificar en el portal de Sovos si el comprobante existe.",
                    "Si NO existe: ejecutar 'retry-errors --forzar'. Si existe: no hacer nada.",
                ])
                log.error("%s tiene un envío previo incierto; no se reenvía", clave)
                return Result(Estado.ERROR_CONEXION, clave, "envío incierto", dest)
            if reserva.accion == "reintento":
                log.info("%s se reprocesa (estado previo %s)", clave, reserva.estado_previo)

        # --- lectura y controles ---------------------------------------------
        try:
            doc = parse_file(work, info)
        except ParseError as exc:
            return self._validation_error(clave, work, [str(exc)])
        except Exception as exc:  # TXT corrupto o inesperado
            log.exception("Error leyendo %s", work.name)
            return self._validation_error(clave, work, [f"No se pudo leer el TXT: {exc}"])

        register_sensitive(doc.cliente.razon_social, doc.cliente.direccion, doc.cliente.telefono,
                           doc.cliente.correo, doc.cliente.numero_documento)
        if doc.encoding != "utf-8":
            log.info("%s leído como %s", clave, doc.encoding)

        result = validate(doc, info, self.s.emisor_ruc)
        for w in result.advertencias:
            log.warning("%s: %s", clave, w)
        if not result.ok:
            return self._validation_error(clave, work, result.errores)

        if not self.s.dry_run:
            self.db.update_fields(clave, fecha_emision=doc.fecha_emision, moneda=doc.moneda,
                                  importe_total=doc.importe_total,
                                  cliente_doc=mask_doc(doc.cliente.numero_documento))

        # --- SBD -------------------------------------------------------------
        try:
            sbd = build_sbd(doc, self.opts)
            validate_sbd(sbd)
        except SchemaError as exc:
            return self._validation_error(clave, work, [str(exc)])
        xml = serialize(sbd)

        if self.s.dry_run:
            out = files.unique_path(self.s.dry_run_dir, f"{clave}.sbd.xml")
            out.write_bytes(xml)
            dest = files.move(work, self.s.dry_run_dir)
            log.info("[DRY_RUN] SBD generado en %s (no se envió)", out)
            return Result("DRY_RUN", clave, str(out), dest)

        sbd_path = files.month_dir(self.s.sbd_dir) / f"{clave}.sbd.xml"
        sbd_path.write_bytes(xml)
        self.db.update_fields(clave, sbd_path=str(sbd_path))

        # --- envío -------------------------------------------------------------
        if self.client is None:
            raise RuntimeError("Cliente Sovos no configurado")
        self.db.mark_sending(clave)
        try:
            document_id = self.client.send_document(to_base64(xml))
        except SovosConnectionError as exc:
            return self._connection_error(clave, work, str(exc), transmitted=exc.transmitted)
        except SovosAuthError as exc:
            return self._connection_error(clave, work, f"Autenticación con Sovos: {exc}", transmitted=False)
        except SovosClientError as exc:
            return self._validation_error(clave, work, [
                f"Sovos rechazó la solicitud (HTTP {exc.status}). El comprobante NO fue registrado.",
                f"Respuesta de Sovos: {exc.body[:800]}",
            ], clear_sending=True)

        self.db.mark_sent(clave, document_id)
        log.info("%s enviado a Sovos, documentId=%s", clave, document_id)
        return Result(Estado.ENVIADO, clave, document_id, work)

    # ------------------------------------------------------------------------
    def _reject_file(self, work: Path, titulo: str, detalle: list[str]) -> Result:
        dest = files.move(work, self.s.error_dir)
        files.write_error_log(dest, titulo, detalle)
        log.error("%s: %s", work.name, titulo)
        return Result(Estado.VALIDACION_ERROR, "", titulo, dest)

    def _validation_error(self, clave: str, work: Path, errores: list[str], clear_sending: bool = False) -> Result:
        dest = files.move(work, self.s.error_dir)
        files.write_error_log(dest, "El comprobante no pasó los controles y NO se envió a Sovos", [
            *errores, "Corregir el origen y volver a dejar el TXT en la carpeta de entrada.",
        ])
        for e in errores:
            log.error("%s: %s", clave, e)
        if not self.s.dry_run:
            extra = {"envio_iniciado_en": None} if clear_sending else {}
            self.db.set_state(clave, Estado.VALIDACION_ERROR, "; ".join(errores)[:2000],
                              mensaje="; ".join(errores)[:2000], **extra)
            self.db.update_fields(clave, archivo=str(dest))
        return Result(Estado.VALIDACION_ERROR, clave, "; ".join(errores), dest)

    def _connection_error(self, clave: str, work: Path, mensaje: str, *, transmitted: bool) -> Result:
        dest = files.move(work, self.s.error_dir)
        if transmitted:
            detalle = [
                mensaje,
                "La solicitud pudo llegar a Sovos: el envío queda INCIERTO y no se reintenta solo.",
                "Verificar en el portal de Sovos si el comprobante existe.",
                "Si NO existe: 'retry-errors --forzar'. Si existe: registrar el documentId manualmente.",
            ]
        else:
            detalle = [mensaje, "Sovos no recibió el comprobante. Se puede reintentar con 'retry-errors'."]
        files.write_error_log(dest, "Error de conexión con Sovos", detalle)
        self.db.set_state(clave, Estado.ERROR_CONEXION, mensaje, mensaje=mensaje,
                          envio_incierto=1 if transmitted else 0,
                          **({} if transmitted else {"envio_iniciado_en": None}))
        self.db.update_fields(clave, archivo=str(dest))
        log.error("%s: %s (incierto=%s)", clave, mensaje, transmitted)
        return Result(Estado.ERROR_CONEXION, clave, mensaje, dest)

    # ------------------------------------------------------------------------
    def recover(self) -> None:
        """Al arrancar: resuelve archivos que quedaron a medias en processing/."""
        known: set[str] = set()
        for row in self.db.by_states(Estado.RECIBIDO):
            path = Path(row["archivo"] or "")
            known.add(path.name)
            if row["envio_iniciado_en"]:
                msg = "El servicio se detuvo durante el envío a Sovos; no se sabe si llegó"
                self.db.set_state(row["clave"], Estado.ERROR_CONEXION, msg, mensaje=msg, envio_incierto=1)
                if path.exists():
                    dest = files.move(path, self.s.error_dir)
                    files.write_error_log(dest, "Envío interrumpido (incierto)", [
                        msg, "Verificar en el portal de Sovos; si NO existe usar 'retry-errors --forzar'."])
                    self.db.update_fields(row["clave"], archivo=str(dest))
                log.error("%s: %s", row["clave"], msg)
            elif path.exists() and path.parent == self.s.processing_dir:
                files.move(path, self.s.inbox_dir)
                log.info("%s devuelto a la bandeja para reprocesar", path.name)

        pending = {Path(r["archivo"]).name for r in self.db.by_states(*(Estado.ENVIADO, Estado.EN_PROCESO))
                   if r["archivo"]}
        for f in self.s.processing_dir.glob("*.txt"):
            if f.name not in pending and f.name not in known:
                files.move(f, self.s.inbox_dir)
                log.info("%s sin registro en la base; devuelto a la bandeja", f.name)
