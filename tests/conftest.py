from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from sovos_bridge.config import Settings
from sovos_bridge.sovos import SovosClient, StaticTokenProvider
from sovos_bridge.storage import Database

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
DATA = Path(__file__).resolve().parent / "data"
RUC = "20437310841"

BOLETA_TXT = SAMPLES / "txt" / "20437310841-03-BT05-00125304.txt"
BOLETA_XML = SAMPLES / "xml" / "20437310841-03-BT06-00129974.xml"
BOLETA_MULTI_TXT = DATA / "20437310841-03-BA01-00014481.txt"
BOLETA_MULTI_XML = SAMPLES / "xml" / "20437310841-03-BA01-00014481.xml"
FACTURA_TXT = DATA / "20437310841-01-FT01-00000758.txt"
FACTURA_XML = SAMPLES / "xml" / "20437310841-01-FT01-00000758.xml"


def note_text(tipo: str = "07", serie: str = "BC01", numero: str = "00000001",
              ref: str = "REF|BT05-00125304|03|01|ANULACION DE LA OPERACION") -> str:
    """Nota de crédito/débito sintética a partir de la boleta de muestra."""
    txt = BOLETA_TXT.read_text(encoding="utf-8")
    txt = txt.replace("|BT05-00125304|", f"|{serie}-{numero}|").replace("|16:22:53|03|", f"|16:22:53|{tipo}|")
    return txt + (ref + "\r\n" if ref else "")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        sovos_base_url="https://sovos.test",
        sovos_auth_mode="static",
        inbox_dir=tmp_path / "inbox",
        work_dir=tmp_path / "work",
        file_stable_seconds=0,
        max_status_minutes=60,
    )
    s.ensure_dirs()
    return s


@pytest.fixture
def db(settings: Settings):
    d = Database(settings.db_path)
    yield d
    d.close()


class FakeSovos:
    """Servidor Sovos simulado sobre httpx.MockTransport.

    `queue(method, path_fragment, *responses)` encola respuestas; cada una es
    un httpx.Response, una excepción a lanzar o una función (request)->Response.
    """

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self._routes: list[tuple[str, str, list]] = []
        self.sleeps: list[float] = []

    def queue(self, method: str, fragment: str, *responses) -> None:
        self._routes.append((method, fragment, list(responses)))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, fragment, responses in self._routes:
            if request.method == method and fragment in str(request.url) and responses:
                r = responses.pop(0) if len(responses) > 1 else responses[0]
                if isinstance(r, Exception):
                    raise r
                if callable(r):
                    return r(request)
                return r
        return httpx.Response(404, json={"error": "ruta no simulada"})

    def client(self, max_retries: int = 3) -> SovosClient:
        http = httpx.Client(transport=httpx.MockTransport(self.handler))
        return SovosClient("https://sovos.test", StaticTokenProvider("token-secreto"), tax_id=RUC,
                           polling_system_id="CN_SystemERP_Polling", max_retries=max_retries,
                           http=http, sleep=self.sleeps.append)

    def sent(self, fragment: str, method: str | None = None) -> list[httpx.Request]:
        return [r for r in self.requests if fragment in str(r.url) and (method is None or r.method == method)]


@pytest.fixture
def fake() -> FakeSovos:
    return FakeSovos()


def accepted(document_id: str = "doc-123") -> httpx.Response:
    return httpx.Response(202, json={"success": True, "data": {"documentId": document_id}})


def application_response(code: str = "AP", cloud: str = "200", govt: str = "0", govt_msg: str = "La Boleta ha sido aceptada",
                         attachments: list[tuple[str, str, bytes]] = ()) -> str:
    """ApplicationResponse UBL en Base64 con estados y adjuntos embebidos."""
    atts = "".join(
        f'<cac:DocumentReference><cbc:ID>{i}</cbc:ID><cac:Attachment>'
        f'<cbc:EmbeddedDocumentBinaryObject mimeCode="{mime}" filename="{name}">'
        f'{base64.b64encode(data).decode()}</cbc:EmbeddedDocumentBinaryObject></cac:Attachment></cac:DocumentReference>'
        for i, (name, mime, data) in enumerate(attachments, 1)
    )
    xml = (
        '<ApplicationResponse xmlns="urn:oasis:names:specification:ubl:schema:xsd:ApplicationResponse-2" '
        'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" '
        'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">'
        '<cac:DocumentResponse><cac:Response>'
        f'<cbc:ResponseCode listID="SCIResponseCode">{code}</cbc:ResponseCode>'
        f'<cac:Status><cbc:StatusReasonCode listID="SCICloudStatusCode">{cloud}</cbc:StatusReasonCode>'
        '<cbc:StatusReason>status</cbc:StatusReason></cac:Status>'
        f'<cac:Status><cbc:StatusReasonCode listID="SCIGovtStatusCode">{govt}</cbc:StatusReasonCode>'
        f'<cbc:StatusReason>{govt_msg}</cbc:StatusReason></cac:Status>'
        '</cac:Response></cac:DocumentResponse>' + atts + '</ApplicationResponse>'
    )
    return base64.b64encode(xml.encode()).decode()


def notification(nid: str, code: str, document_id: str = "doc-123", content: str | None = None,
                 govt: str = "", govt_msg: str = "", cloud: str = "") -> dict:
    meta = {"sciResponseCode": code, "documentId": document_id}
    if govt:
        meta["sciGovtStatusCode"] = govt
    if govt_msg:
        meta["sciGovtStatusMessage"] = govt_msg
    if cloud:
        meta["sciCloudStatusCode"] = cloud
    n = {"notificationId": nid, "metadata": meta}
    if content:
        n["content"] = content
    return n


def notifications_response(*items: dict) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": list(items)})


def decode_sbd(request: httpx.Request) -> bytes:
    return base64.b64decode(json.loads(request.content)["data"])
