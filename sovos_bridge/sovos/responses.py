"""Lectura de notificaciones de Sovos y de su ApplicationResponse.

La documentación de Sovos describe los campos (notificationId,
metadata.sciResponseCode, sciGovtStatusCode, sciGovtStatusMessage, content en
Base64 con un ApplicationResponse UBL) pero no publica un JSON completo para
Perú. Por eso la lectura es tolerante: busca las claves sin distinguir
mayúsculas y acepta la lista en `data`, `data.notifications`, `notifications`
o en la raíz. Si metadata no trae los códigos, se leen del ApplicationResponse.

Los adjuntos (LegalCleared = XML legal con constancia; PDF) se buscan en
cualquier EmbeddedDocumentBinaryObject del ApplicationResponse, o como
enlace (URI http/https). Si el adjunto es un ZIP se extraen sus archivos.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import zipfile
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

log = logging.getLogger(__name__)

APPROVED = {"AP", "CA", "PD"}
REJECTED = {"RE"}
# Códigos SCICloudStatusCode que indican que Sovos ya tenía el documento.
DUPLICATE_CLOUD_CODES = {"404", "406"}
WORKFLOW_COMPLETED = "209"


@dataclass
class Attachment:
    kind: str                  # "xml" | "pdf" | "zip" | "otro"
    filename: str = ""
    mime: str = ""
    data: bytes | None = None
    uri: str | None = None


@dataclass
class Notification:
    notification_id: str
    document_id: str = ""
    response_code: str = ""        # AP / RE / IP / CA / AB ...
    cloud_code: str = ""           # 100..505
    govt_code: str = ""            # código SUNAT/OSE
    govt_message: str = ""
    action: str = ""               # NOA / NIN / OTH
    content: bytes | None = None   # ApplicationResponse XML decodificado
    attachments: list[Attachment] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def is_approved(self) -> bool:
        return self.response_code in APPROVED

    @property
    def is_rejected(self) -> bool:
        return self.response_code in REJECTED

    @property
    def is_duplicate(self) -> bool:
        return self.is_rejected and self.cloud_code in DUPLICATE_CLOUD_CODES


def _get(d: Any, *names: str, default: Any = "") -> Any:
    if not isinstance(d, dict):
        return default
    lower = {k.lower(): v for k, v in d.items()}
    for n in names:
        v = lower.get(n.lower())
        if v not in (None, ""):
            return v
    return default


def _list(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "notifications", "items", "results"):
        v = _get(payload, key, default=None)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
        if isinstance(v, dict):
            inner = _list(v)
            if inner:
                return inner
    if _get(payload, "notificationId", default=None):
        return [payload]
    return []


def _b64(value: str) -> bytes | None:
    try:
        return base64.b64decode("".join(value.split()), validate=False)
    except (binascii.Error, ValueError):
        return None


def parse_notifications(payload: Any) -> list[Notification]:
    out: list[Notification] = []
    for item in _list(payload):
        meta = _get(item, "metadata", default={}) or {}
        n = Notification(
            notification_id=str(_get(item, "notificationId", "id")),
            document_id=str(_get(meta, "documentId") or _get(item, "documentId")),
            response_code=str(_get(meta, "sciResponseCode") or _get(item, "sciResponseCode")).upper(),
            cloud_code=str(_get(meta, "sciCloudStatusCode") or _get(item, "sciCloudStatusCode")),
            govt_code=str(_get(meta, "sciGovtStatusCode") or _get(item, "sciGovtStatusCode")),
            govt_message=str(_get(meta, "sciGovtStatusMessage") or _get(item, "sciGovtStatusMessage")),
            action=str(_get(meta, "sciStatusAction") or _get(item, "sciStatusAction")).upper(),
            raw=item,
        )
        content = _get(item, "content", default="")
        if isinstance(content, str) and content:
            n.content = _b64(content)
        if n.content:
            _fill_from_application_response(n)
        if not n.notification_id:
            log.warning("Notificación sin notificationId; se ignora")
            continue
        out.append(n)
    return out


def _local(el: etree._Element) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _fill_from_application_response(n: Notification) -> None:
    try:
        root = etree.fromstring(n.content, parser=etree.XMLParser(resolve_entities=False, no_network=True,
                                                                  huge_tree=True))
    except etree.XMLSyntaxError:
        log.warning("El content de la notificación %s no es XML válido", n.notification_id)
        return

    for el in root.iter():
        name = _local(el)
        if name == "ResponseCode" and el.get("listID", "").lower() == "sciresponsecode" and not n.response_code:
            n.response_code = (el.text or "").strip().upper()
        elif name == "StatusReasonCode":
            list_id = el.get("listID", "").lower()
            reason = el.getparent().find("{*}StatusReason") if el.getparent() is not None else None
            reason_text = (reason.text or "").strip() if reason is not None else ""
            code = (el.text or "").strip()
            if list_id == "scicloudstatuscode" and not n.cloud_code:
                n.cloud_code = code
            elif list_id == "scigovtstatuscode" and not n.govt_code:
                n.govt_code, n.govt_message = code, n.govt_message or reason_text
            elif list_id == "scistatusaction" and not n.action:
                n.action = code.upper()
        elif name == "EmbeddedDocumentBinaryObject" and el.text:
            data = _b64(el.text)
            if data:
                n.attachments.extend(_classify(data, el.get("mimeCode", ""), el.get("filename", "")))
        elif name == "URI" and (el.text or "").strip().lower().startswith("http"):
            uri = el.text.strip()
            kind = "pdf" if uri.lower().split("?")[0].endswith(".pdf") else "xml" \
                if uri.lower().split("?")[0].endswith(".xml") else "otro"
            n.attachments.append(Attachment(kind=kind, uri=uri))


def _classify(data: bytes, mime: str, filename: str) -> list[Attachment]:
    mime_l, name_l = mime.lower(), filename.lower()
    if "zip" in mime_l or name_l.endswith(".zip") or data[:2] == b"PK":
        out: list[Attachment] = []
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    out.extend(_classify(zf.read(info), "", info.filename))
        except zipfile.BadZipFile:
            return [Attachment("otro", filename, mime, data)]
        return out
    if "pdf" in mime_l or name_l.endswith(".pdf") or data[:4] == b"%PDF":
        return [Attachment("pdf", filename, mime, data)]
    if "xml" in mime_l or name_l.endswith(".xml") or data.lstrip()[:1] == b"<":
        return [Attachment("xml", filename, mime, data)]
    return [Attachment("otro", filename, mime, data)]


def is_cdr(xml: bytes) -> bool:
    """True si el XML es una constancia (ApplicationResponse) y no el comprobante."""
    head = xml[:2000]
    return b"ApplicationResponse" in head and b"<Invoice" not in head and b":Invoice" not in head
