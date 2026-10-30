"""Línea de comandos.

  python -m sovos_bridge run                     servicio continuo
  python -m sovos_bridge send <archivo.txt>      procesa un TXT
  python -m sovos_bridge status <serie-numero>   estado de un comprobante
  python -m sovos_bridge retry-errors            reintenta errores de conexión
  python -m sovos_bridge report --date AAAA-MM-DD   resumen del día en CSV
  python -m sovos_bridge ui                      solo el panel web (sin procesar la bandeja)
  python -m sovos_bridge certificado <archivo.pfx>  datos del certificado (pide la clave sin mostrarla)
  python -m sovos_bridge reubicar --desde <ruta>  ajusta las rutas de la base traída de otra PC
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import date
from pathlib import Path

from sovos_bridge import __version__
from sovos_bridge.config import Settings, load_settings
from sovos_bridge.logging_setup import setup_logging
from sovos_bridge.service.operations import daily_report, retry_errors, status_text
from sovos_bridge.service.pipeline import Pipeline
from sovos_bridge.service.runner import Service
from sovos_bridge.service.status import StatusPoller
from sovos_bridge.sovos import SovosClient, build_token_provider
from sovos_bridge.storage import Database
from sovos_bridge.web import PanelApp, start_panel

log = logging.getLogger("sovos_bridge")


def _client(s: Settings) -> SovosClient:
    return SovosClient(
        s.sovos_base_url, build_token_provider(s), tax_id=s.emisor_ruc,
        polling_system_id=s.sovos_polling_system_id, timeout=s.http_timeout_seconds,
        max_retries=s.http_max_retries,
    )


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sovos_bridge", description="Puente TXT -> Sovos (Perú)")
    p.add_argument("--env", help="ruta del archivo .env (por defecto ./.env)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="ejecutar el servicio (bandeja + consulta de estados)")
    sp = sub.add_parser("send", help="procesar un solo TXT")
    sp.add_argument("archivo", type=Path)
    sp.add_argument("--dry-run", action="store_true", help="solo generar el SBD, sin enviar")
    st = sub.add_parser("status", help="estado de un comprobante (ej. BT05-00125304)")
    st.add_argument("serie_numero")
    st.add_argument("--consultar", action="store_true", help="consultar ahora a Sovos si está pendiente")
    rt = sub.add_parser("retry-errors", help="reintentar comprobantes con error")
    rt.add_argument("--forzar", action="store_true", help="incluir envíos inciertos (verificados en Sovos)")
    rt.add_argument("--todos", action="store_true", help="incluir VALIDACION_ERROR y RECHAZADO ya corregidos")
    rp = sub.add_parser("report", help="resumen del día en CSV")
    rp.add_argument("--date", default=date.today().isoformat(), help="AAAA-MM-DD (por defecto hoy)")
    sub.add_parser("ui", help="abrir solo el panel web (estados, reintentos, consulta a Sovos)")
    ru = sub.add_parser("reubicar", help="ajustar las rutas guardadas en la base al trasladarla a otra PC")
    ru.add_argument("--desde", required=True, help="WORK_DIR que tenía la PC de origen")
    ce = sub.add_parser("certificado", help="mostrar serie y vigencia de un certificado .pfx/.p12")
    ce.add_argument("archivo", type=Path)
    ce.add_argument("--legacy", type=Path, metavar="SALIDA.pfx",
                    help="reexportar con cifrado clásico (3DES/SHA1) y la misma contraseña")
    return p


def _cert_info(path: Path, legacy_out: Path | None = None) -> int:
    """Muestra los datos para compararlos con SOL (Registro de certificado digital)."""
    import getpass

    from sovos_bridge.sbd import Signer, SigningError
    password = getpass.getpass("Contraseña del certificado (no se muestra): ")
    try:
        signer = Signer.from_pkcs12(path, password)
    except SigningError as exc:
        print(exc)
        return 2
    i = signer.info
    print(f"Serie      : {i.serial_hex}")
    print(f"Titular    : {i.subject}")
    print(f"Vigencia   : {i.not_before:%d/%m/%Y} al {i.not_after:%d/%m/%Y}")
    print("Comparar la serie con SOL > Registro y mantenimiento de certificados digitales.")
    if legacy_out:
        legacy_out.write_bytes(signer.export_legacy(password))
        print(f"Reexportado con cifrado clásico (misma contraseña): {legacy_out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "certificado":
        return _cert_info(args.archivo, args.legacy)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    s = load_settings(args.env)
    if getattr(args, "dry_run", False):
        s = s.model_copy(update={"dry_run": True})
    s.ensure_dirs()
    setup_logging(s.logs_dir, s.log_level, s.log_retention_days, console=True)
    db = Database(s.db_path)
    try:
        return _dispatch(args, s, db)
    finally:
        db.close()


def _dispatch(args, s: Settings, db: Database) -> int:
    if args.cmd == "reubicar":
        n = db.relocate(args.desde, s.work_dir.resolve())
        print(f"Rutas actualizadas: {n} (de {args.desde} a {s.work_dir.resolve()})")
        return 0

    if args.cmd == "status":
        print(status_text(db, args.serie_numero))
        if args.consultar:
            client = _client(s)
            StatusPoller(s, db, client).run_once()
            print("--- después de consultar ---")
            print(status_text(db, args.serie_numero))
        return 0

    if args.cmd == "report":
        path, counts = daily_report(s, db, args.date)
        print(f"Reporte: {path}")
        for estado, n in sorted(counts.items()):
            print(f"  {estado:<18} {n}")
        print(f"  {'TOTAL':<18} {sum(counts.values())}")
        return 0

    if args.cmd == "retry-errors":
        acciones = retry_errors(s, db, forzar=args.forzar, todos=args.todos)
        print("\n".join(acciones) if acciones else "No hay comprobantes para reintentar")
        return 0

    client = None if s.dry_run else _client(s)
    pipeline = Pipeline(s, db, client)

    if args.cmd == "send":
        if not args.archivo.exists():
            print(f"No existe el archivo {args.archivo}")
            return 2
        # Se trabaja sobre una copia: el archivo original del operador no se mueve.
        manual = s.work_dir / "manual"
        manual.mkdir(parents=True, exist_ok=True)
        copia = manual / args.archivo.name
        shutil.copy2(args.archivo, copia)
        r = pipeline.process(copia)
        print(f"{r.estado}: {r.clave} {r.mensaje}")
        return 0 if r.estado in ("ENVIADO", "DRY_RUN") else 1

    if args.cmd == "run":
        poller = None if s.dry_run else StatusPoller(s, db, client)
        Service(s, pipeline, poller, PanelApp(s, db, client, poller)).run()
        return 0

    if args.cmd == "ui":
        panel = start_panel(PanelApp(s, db, client))
        if panel is None:
            return 2
        print(f"Panel web en http://{'localhost' if s.ui_host in ('127.0.0.1', 'localhost') else s.ui_host}"
              f":{panel[0].server_address[1]}  (Ctrl+C para salir)")
        try:
            panel[1].join()
        except KeyboardInterrupt:
            panel[0].shutdown()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
