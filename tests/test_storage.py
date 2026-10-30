from sovos_bridge.storage import Estado

K = "20437310841-03-BT05-00125304"


def reserve(db):
    return db.reserve(K, tipo="03", serie_numero="BT05-00125304", archivo="a.txt")


def test_primera_vez_es_nuevo(db):
    assert reserve(db).accion == "nuevo"
    assert db.get(K)["estado"] == Estado.RECIBIDO


def test_aprobado_es_duplicado(db):
    reserve(db)
    db.mark_sending(K)
    db.mark_sent(K, "doc-1")
    db.set_state(K, Estado.APROBADO, "ok")
    r = reserve(db)
    assert r.accion == "duplicado" and r.estado_previo == Estado.APROBADO
    assert db.get(K)["estado"] == Estado.APROBADO   # el estado no cambia


def test_pendiente_es_duplicado(db):
    reserve(db)
    db.mark_sending(K)
    db.mark_sent(K, "doc-1")
    assert reserve(db).accion == "duplicado"


def test_error_conexion_con_document_id_no_se_reenvia(db):
    reserve(db)
    db.mark_sending(K)
    db.mark_sent(K, "doc-1")
    db.set_state(K, Estado.ERROR_CONEXION, "timeout de estado")
    assert reserve(db).accion == "duplicado"


def test_rechazado_se_puede_reenviar_corregido(db):
    reserve(db)
    db.mark_sending(K)
    db.mark_sent(K, "doc-1")
    db.set_state(K, Estado.RECHAZADO, "error 2335")
    r = reserve(db)
    assert r.accion == "reintento"
    row = db.get(K)
    assert row["estado"] == Estado.RECIBIDO and row["document_id"] is None


def test_envio_interrumpido_es_incierto(db):
    reserve(db)
    db.mark_sending(K)          # el proceso murió durante el POST
    assert reserve(db).accion == "incierto"


def test_error_incierto_bloquea(db):
    reserve(db)
    db.mark_sending(K)
    db.set_state(K, Estado.ERROR_CONEXION, "timeout", envio_incierto=1)
    assert reserve(db).accion == "incierto"


def test_eventos_y_busqueda(db):
    reserve(db)
    assert db.find("bt05-00125304")[0]["clave"] == K
    assert db.events(K)[0]["estado"] == Estado.RECIBIDO


def test_notificaciones(db):
    assert db.save_notification("n1", clave=K, document_id="d", response_code="AP") is True
    assert db.save_notification("n1", clave=K, document_id="d", response_code="AP") is False
    assert db.unacked_notifications() == ["n1"]
    db.mark_acked(["n1"])
    assert db.unacked_notifications() == []


def test_reubicar_rutas_de_otra_pc(tmp_path):
    from sovos_bridge.storage import Database
    db = Database(tmp_path / "db.sqlite")
    db.reserve(K, tipo="03", serie_numero="BT05-00125304", archivo="/Users/x/work/processed/a.txt")
    db.update_fields(K, xml_path="/Users/x/work/output/2026/10/a.xml", pdf_path="/otro/lugar/a.pdf")
    nuevo = tmp_path / "srv" / "work"
    assert db.relocate("/Users/x/work", nuevo) == 2
    row = db.get(K)
    assert row["archivo"] == str(nuevo / "processed" / "a.txt")
    assert row["xml_path"] == str(nuevo / "output" / "2026" / "10" / "a.xml")
    assert row["pdf_path"] == "/otro/lugar/a.pdf"           # fuera de la carpeta de trabajo: no se toca
    assert db.relocate("/Users/x/work", nuevo) == 0          # repetirlo no cambia nada
