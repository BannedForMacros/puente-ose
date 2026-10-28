"""Consulta de estado en Sovos hasta AP o RE, descarga de XML/PDF y confirmación."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from sovos_bridge.config import Settings
from sovos_bridge.service import files
from sovos_bridge.sovos import (
    Attachment, Notification, SovosClient, SovosError, is_cdr, parse_notifications,
)
from sovos_bridge.sovos.responses import WORKFLOW_COMPLETED
from sovos_bridge.storage import PENDIENTES, Database, Estado

log = logging.getLogger(__name__)

ACK_BATCH = 50


class StatusPoller:
    def __init__(self, settings: Settings, db: Database, client: SovosClient):
        self.s = settings
        self.db = db
        self.client = client

    # ------------------------------------------------------------------------
    def run_once(self) -> None:
        if self.s.sovos_status_mode == "global":
            self._poll_global()
        else:
            for row in self.db.by_states(*PENDIENTES):
                try:
                    payload = self.client.document_notifications(row["document_id"])
                except SovosError as exc:
                    log.warning("%s: no se pudo consultar el estado: %s", row["clave"], exc)
                    self._check_timeout(self.db.get(row["clave"]))
                    continue
                self.db.touch_status_poll(row["clave"])
                self.handle(row["clave"], parse_notifications(payload))
        self.flush_acks()

    def _poll_global(self) -> None:
        page = 1
        while True:
            try:
                notifs = parse_notifications(self.client.list_notifications(page=page, per_page=50))
            except SovosError as exc:
                log.warning("No se pudo consultar notificaciones: %s", exc)
                break
            by_doc: dict[str, list[Notification]] = {}
            for n in notifs:
                by_doc.setdefault(n.document_id, []).append(n)
            for doc_id, group in by_doc.items():
                row = self.db.by_document_id(doc_id) if doc_id else None
                if row is None:
                    log.info("Notificación de documentId %s desconocido; se confirma sin procesar", doc_id)
                    for n in group:
                        self.db.save_notification(n.notification_id, clave=None, document_id=doc_id,
                                                  response_code=n.response_code)
                    continue
                self.handle(row["clave"], group)
            if len(notifs) < 50:
                break
            page += 1
        for row in self.db.by_states(*PENDIENTES):
            self._check_timeout(row)

    # ------------------------------------------------------------------------
    def handle(self, clave: str, notifs: list[Notification]) -> None:
        row = self.db.get(clave)
        if row is None or row["estado"] not in PENDIENTES:
            for n in notifs:
                self.db.save_notification(n.notification_id, clave=clave, document_id=n.document_id,
                                          response_code=n.response_code)
            return

        when = _issue_month(row)
        xml_path, pdf_path = row["xml_path"], row["pdf_path"]
        approved = rejected = None
        completed = False
        for n in notifs:
            nueva = self.db.save_notification(n.notification_id, clave=clave, document_id=n.document_id,
                                              response_code=n.response_code)
            # Con includeAcknowledged=true llegan también las ya vistas: sus
            # adjuntos ya se guardaron, pero sus códigos cuentan para decidir.
            if nueva or not (xml_path and pdf_path):
                for att in n.attachments:
                    saved = self._save_attachment(clave, att, when)
                    if saved and saved.suffix == ".pdf":
                        pdf_path = str(saved)
                    elif saved and saved.suffix == ".xml" and not saved.name.startswith("R-"):
                        xml_path = str(saved)
            if nueva and n.content:
                (files.month_dir(self.s.output_dir, when) / f"{clave}-respuesta-sovos-{n.notification_id}.xml"
                 ).write_bytes(n.content)
            if n.cloud_code == WORKFLOW_COMPLETED:
                completed = True
            if n.is_rejected:
                rejected = n
            elif n.is_approved:
                approved = n

        if xml_path != row["xml_path"] or pdf_path != row["pdf_path"]:
            self.db.update_fields(clave, xml_path=xml_path, pdf_path=pdf_path)

        if rejected is not None:
            self._rejected(row, rejected)
        elif approved is not None:
            if (xml_path and pdf_path) or completed:
                self._approved(row, approved, xml_path, pdf_path)
            else:
                if row["codigo_sovos"] != approved.response_code:
                    self.db.set_state(clave, Estado.EN_PROCESO, "Aprobado; esperando XML/PDF",
                                      codigo_sovos=approved.response_code, codigo_sunat=approved.govt_code)
                self._check_timeout(self.db.get(clave))
        else:
            if row["estado"] == Estado.ENVIADO and notifs:
                last = notifs[-1]
                self.db.set_state(clave, Estado.EN_PROCESO, f"Sovos: {last.response_code} {last.cloud_code}".strip(),
                                  codigo_sovos=last.response_code or None)
            self._check_timeout(self.db.get(clave))

    # ------------------------------------------------------------------------
    def _approved(self, row: sqlite3.Row, n: Notification, xml_path: str | None, pdf_path: str | None) -> None:
        clave = row["clave"]
        notas = []
        if not xml_path:
            notas.append("Sovos no envió el XML firmado")
        if not pdf_path:
            notas.append("Sovos no envió el PDF")
        msg = n.govt_message or ("Aceptado con observaciones" if n.response_code == "CA" else "Aceptado")
        if notas:
            msg += " (" + "; ".join(notas) + ")"
        self.db.set_state(clave, Estado.APROBADO, msg, codigo_sovos=n.response_code,
                          codigo_sunat=n.govt_code or None, mensaje=msg)
        self._move_txt(row, self.s.processed_dir, remove_log=True)
        log.info("%s APROBADO (%s)", clave, msg)

    def _rejected(self, row: sqlite3.Row, n: Notification) -> None:
        clave = row["clave"]
        if n.is_duplicate:
            msg = f"Sovos indica que el documento ya fue procesado (código {n.cloud_code})"
            self.db.set_state(clave, Estado.DUPLICADO, msg, codigo_sovos=n.response_code, mensaje=msg)
            dest = self._move_txt(row, self.s.duplicates_dir)
            if dest:
                files.write_error_log(dest, "Duplicado según Sovos", [msg])
            log.warning("%s: %s", clave, msg)
            return
        motivo = n.govt_message or "Sin mensaje de SUNAT/OSE"
        msg = f"[{n.govt_code or n.cloud_code}] {motivo}"
        self.db.set_state(clave, Estado.RECHAZADO, msg, codigo_sovos=n.response_code,
                          codigo_sunat=n.govt_code or None, mensaje=msg)
        dest = self._move_txt(row, self.s.error_dir)
        if dest:
            detalle = [
                f"Código SUNAT/OSE: {n.govt_code or '(no informado)'}",
                f"Mensaje: {motivo}",
                f"Código Sovos: {n.response_code} / {n.cloud_code or '-'}",
            ]
            if n.action == "NIN":
                detalle.append("Sovos indica que se debe emitir un comprobante nuevo.")
            detalle.append("Corregir en el sistema de facturación y volver a dejar el TXT en la bandeja.")
            files.write_error_log(dest, "Comprobante RECHAZADO", detalle,
                                  {"documentId": row["document_id"] or ""})
        log.error("%s RECHAZADO: %s", clave, msg)

    def _check_timeout(self, row: sqlite3.Row | None) -> None:
        if row is None or row["estado"] not in PENDIENTES or not row["enviado_en"]:
            return
        elapsed = (datetime.now().astimezone() - datetime.fromisoformat(row["enviado_en"])).total_seconds() / 60
        if elapsed < self.s.max_status_minutes:
            return
        clave = row["clave"]
        if row["codigo_sovos"] in ("AP", "CA", "PD"):
            msg = f"Aprobado; XML/PDF no recibidos después de {self.s.max_status_minutes:.0f} min"
            self.db.set_state(clave, Estado.APROBADO, msg, mensaje=msg)
            self._move_txt(row, self.s.processed_dir)
            log.warning("%s: %s", clave, msg)
            return
        msg = (f"Sin respuesta final de Sovos después de {self.s.max_status_minutes:.0f} min "
               f"(documentId {row['document_id']})")
        self.db.set_state(clave, Estado.ERROR_CONEXION, msg, mensaje=msg)
        dest = self._move_txt(row, self.s.error_dir)
        if dest:
            files.write_error_log(dest, "Sovos no dio respuesta final a tiempo", [
                msg,
                "El comprobante SÍ fue recibido por Sovos: NO reenviarlo como nuevo.",
                "Ejecutar 'retry-errors' para seguir consultando su estado.",
            ])
        log.error("%s: %s", clave, msg)

    # ------------------------------------------------------------------------
    def _save_attachment(self, clave: str, att: Attachment, when: datetime) -> Path | None:
        data = att.data
        if data is None and att.uri:
            try:
                data = self.client.download(att.uri)
            except SovosError as exc:
                log.warning("%s: no se pudo descargar %s: %s", clave, att.kind, exc)
                return None
            att = Attachment(att.kind if att.kind != "otro" else _sniff(data), att.filename, att.mime, data)
        if not data:
            return None
        out = files.month_dir(self.s.output_dir, when)
        if att.kind == "pdf":
            path = out / f"{clave}.pdf"
        elif att.kind == "xml":
            path = out / (f"R-{clave}.xml" if is_cdr(data) else f"{clave}.xml")
        else:
            path = out / f"{clave}-{Path(att.filename).name or 'adjunto.bin'}"
        path.write_bytes(data)
        log.info("%s: guardado %s", clave, path.name)
        return path

    def _move_txt(self, row: sqlite3.Row, dest_dir: Path, remove_log: bool = False) -> Path | None:
        src = Path(row["archivo"] or "")
        if not src.exists():
            log.warning("%s: no se encontró el TXT %s para moverlo", row["clave"], src)
            return None
        if remove_log:
            files.remove_error_log(src)
        dest = files.move(src, dest_dir)
        self.db.update_fields(row["clave"], archivo=str(dest))
        return dest

    def flush_acks(self) -> None:
        """Confirma en lote (PUT) las notificaciones ya procesadas."""
        while True:
            ids = self.db.unacked_notifications(ACK_BATCH)
            if not ids:
                return
            try:
                self.client.acknowledge(ids)
            except SovosError as exc:
                log.warning("No se pudieron confirmar %d notificaciones; se reintentará: %s", len(ids), exc)
                return
            self.db.mark_acked(ids)
            log.debug("Confirmadas %d notificaciones", len(ids))


def _issue_month(row: sqlite3.Row) -> datetime:
    try:
        return datetime.fromisoformat(row["fecha_emision"])
    except (TypeError, ValueError):
        return datetime.now()


def _sniff(data: bytes) -> str:
    if data[:4] == b"%PDF":
        return "pdf"
    if data.lstrip()[:1] == b"<":
        return "xml"
    return "otro"
