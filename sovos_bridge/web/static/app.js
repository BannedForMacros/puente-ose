/* Puente Sovos: panel de estados. Sin dependencias ni build. */
"use strict";

const REFRESH_MS = 15000;

const ESTADOS = {
  RECIBIDO:         { label: "Recibido",            tone: "idle" },
  ENVIADO:          { label: "Enviado a Sovos",     tone: "info" },
  EN_PROCESO:       { label: "En proceso",          tone: "info" },
  APROBADO:         { label: "Aprobado",            tone: "ok" },
  RECHAZADO:        { label: "Rechazado",           tone: "bad" },
  ERROR_CONEXION:   { label: "Error de envío",      tone: "bad" },
  VALIDACION_ERROR: { label: "Error de validación", tone: "warn" },
  DUPLICADO:        { label: "Duplicado",           tone: "warn" },
  EN_RESUMEN:       { label: "En resumen diario",   tone: "info" },
};
const SOVOS_CODES = { AP: "AP, aprobado", CA: "CA, aprobado con observaciones", RE: "RE, rechazado", IP: "IP, en proceso", PD: "PD, pendiente" };
const TIPOS = { "01": "Factura", "03": "Boleta", "07": "Nota de crédito", "08": "Nota de débito", RC: "Resumen diario" };

// Etapas del recorrido (secuencia real) y grupos que requieren atención.
const STAGES = [
  { key: "bandeja", name: "En cola", hint: "Saliendo hacia Sovos", estados: null, tone: "idle" },
  { key: "recibido", name: "Recibidos", hint: "Validando y armando", estados: ["RECIBIDO"], tone: "idle" },
  { key: "sovos", name: "En Sovos", hint: "Esperando a SUNAT", estados: ["ENVIADO", "EN_PROCESO", "EN_RESUMEN"], tone: "info" },
  { key: "aprobado", name: "Aprobados", hint: "Aceptados por SUNAT", estados: ["APROBADO"], tone: "ok" },
];
const ALERTS = [
  { key: "rechazado", name: "Rechazados", estados: ["RECHAZADO"], tone: "bad" },
  { key: "conexion", name: "Errores de envío", estados: ["ERROR_CONEXION"], tone: "bad" },
  { key: "validacion", name: "Errores de validación", estados: ["VALIDACION_ERROR"], tone: "warn" },
  { key: "duplicado", name: "Duplicados", estados: ["DUPLICADO"], tone: "warn" },
];

const state = {
  q: "", tipo: "", desde: "", hasta: "", group: "",
  sort: "actualizado_en", dir: "desc", page: 1, perPage: 25,
  total: 0, summary: null, openClave: null, lastLoad: null, firstSummary: true,
};

const $ = (id) => document.getElementById(id);

// --- utilidades -------------------------------------------------------------
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtDate(iso) {
  if (!iso) return "—";
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${d}/${m}/${y}`;
}
function fmtDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("es-PE", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function fmtAgo(iso) {
  if (!iso) return "—";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `hace ${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `hace ${m} min`;
  const h = Math.round(m / 60);
  if (h < 24) return `hace ${h} h`;
  return fmtDate(iso);
}
function fmtMoney(v, cur) {
  const n = Number(v);
  if (!isFinite(n)) return "—";
  return `${cur === "USD" ? "US$" : "S/"} ${n.toLocaleString("es-PE", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function badge(estado) {
  const e = ESTADOS[estado] || { label: estado, tone: "idle" };
  return `<span class="badge" data-tone="${e.tone}">${esc(e.label)}</span>`;
}
function tipoLabel(t) { return TIPOS[t] || `Tipo ${t}`; }
// El mensaje guardado empieza con "[2325] ..."; el código ya se muestra aparte.
function cleanMsg(m) { return String(m || "").replace(/^\[[^\]]*\]\s*/, ""); }
function groupEstados(key) {
  const g = [...STAGES, ...ALERTS].find((x) => x.key === key);
  return g && g.estados ? g.estados : [];
}

async function api(path, opts = {}) {
  const init = { headers: { Accept: "application/json" }, ...opts };
  if (opts.method === "POST") {
    init.headers = { ...init.headers, "Content-Type": "application/json", "X-Sovos-Panel": "1" };
    init.body = JSON.stringify(opts.body || {});
  }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch { /* sin cuerpo JSON */ }
  if (!res.ok) throw new Error((data && data.error) || `El servidor respondió ${res.status}`);
  return data;
}

function toast(msg, tone = "ok") {
  const el = document.createElement("div");
  el.className = "toast";
  el.dataset.tone = tone;
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => { el.classList.add("is-leaving"); setTimeout(() => el.remove(), 200); }, tone === "ok" ? 3500 : 6000);
}

function busy(btn, on) {
  if (!btn) return;
  btn.classList.toggle("is-busy", on);
  btn.disabled = on;
}

// --- contador animado -------------------------------------------------------
function countTo(el, target, animate) {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!animate || reduce || target === 0) { el.textContent = target.toLocaleString("es-PE"); return; }
  const start = performance.now(), dur = 600;
  const step = (t) => {
    const p = Math.min(1, (t - start) / dur);
    el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3))).toLocaleString("es-PE");
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

// --- resumen y franja de flujo ----------------------------------------------
function stageCount(s, g) {
  if (g.key === "bandeja") return s.bandeja;
  return g.estados.reduce((acc, e) => acc + (s.estados[e] || 0), 0);
}

function renderSummary(s) {
  const first = state.firstSummary;
  state.firstSummary = false;

  $("org").textContent = `Hogar Clínica San Juan de Dios, RUC ${s.ruc}`;
  const env = $("env");
  env.hidden = false;
  env.textContent = s.ambiente;
  env.dataset.env = s.ambiente;

  const svc = $("service");
  svc.dataset.alive = String(s.servicio.activo);
  $("service-text").textContent = s.servicio.activo
    ? `Servicio activo, revisó la bandeja ${fmtAgo(s.servicio.ultima_revision)}`
    : s.servicio.ultima_revision
      ? `Servicio detenido desde ${fmtDateTime(s.servicio.ultima_revision)}`
      : "Servicio de envío detenido";

  const track = $("flow-track");
  if (first) {
    track.innerHTML = STAGES.map((g) => `
      <li class="stage" data-tone="${g.tone}" data-key="${g.key}">
        <div class="stage-rail" aria-hidden="true"></div>
        <button class="stage-btn" type="button" data-group="${g.key}" ${g.estados ? "" : "disabled"} aria-pressed="false">
          <span class="stage-count" data-count>0</span>
          <span class="stage-name">${g.name}</span>
          <span class="stage-hint" data-hint>${g.hint}</span>
        </button>
      </li>`).join("");
    $("flow-attention").innerHTML = ALERTS.map((g) => `
      <button class="alert-btn" type="button" data-group="${g.key}" data-tone="${g.tone}" aria-pressed="false">
        <span class="stage-name">${g.name}</span>
        <span class="stage-count" data-count>0</span>
      </button>`).join("");
    const flow = document.querySelector(".flow");
    flow.classList.add("is-entering");
    setTimeout(() => flow.classList.remove("is-entering"), 900);
  }

  STAGES.forEach((g) => {
    const li = track.querySelector(`[data-key="${g.key}"]`);
    const n = stageCount(s, g);
    li.dataset.active = String(n > 0);
    countTo(li.querySelector("[data-count]"), n, first);
    if (g.key === "aprobado") {
      li.querySelector("[data-hint]").textContent = s.hoy.aprobados
        ? `${s.hoy.aprobados} hoy, ${fmtMoney(s.hoy.monto)}` : g.hint;
    }
  });
  ALERTS.forEach((g) => {
    const b = $("flow-attention").querySelector(`[data-group="${g.key}"]`);
    const n = stageCount(s, g);
    b.dataset.has = String(n > 0);
    b.setAttribute("aria-label", `${g.name}: ${n}`);
    countTo(b.querySelector("[data-count]"), n, first);
  });

  renderConfigBanner(s.config_pendiente || []);
  if (first && (s.config_pendiente || []).length) showView("config");
  const tc = $("tab-tray-count");
  tc.hidden = !s.preparacion;
  tc.textContent = (s.preparacion || 0).toLocaleString("es-PE");
  const pendientes = (s.estados.ERROR_CONEXION || 0) + (s.estados.RECHAZADO || 0) + (s.estados.VALIDACION_ERROR || 0);
  $("retry-all").disabled = pendientes === 0;
  syncGroupButtons();
}

function syncGroupButtons() {
  document.querySelectorAll("[data-group]").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.group === state.group));
  });
}

// --- tabla ------------------------------------------------------------------
function queryString(extra = {}) {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  if (state.tipo) p.set("tipo", state.tipo);
  if (state.desde) p.set("desde", state.desde);
  if (state.hasta) p.set("hasta", state.hasta);
  const est = groupEstados(state.group);
  if (est.length) p.set("estado", est.join(","));
  p.set("sort", state.sort);
  p.set("dir", state.dir);
  Object.entries(extra).forEach(([k, v]) => p.set(k, v));
  return p.toString();
}

function skeleton() {
  $("state").hidden = true;
  $("rows").innerHTML = Array.from({ length: 6 }, () => `
    <tr aria-hidden="true">
      <td><span class="sk sk-w1"></span></td><td><span class="sk sk-w2"></span></td>
      <td><span class="sk sk-w2"></span></td><td class="num"><span class="sk sk-w4"></span></td>
      <td><span class="sk sk-w2"></span></td><td><span class="sk sk-w3"></span></td>
      <td><span class="sk sk-w2"></span></td><td></td>
    </tr>`).join("");
}

function retryLabel(kind) {
  return { consulta: "Reanudar consulta", reenvio: "Reenviar", forzado: "Reenviar" }[kind] || "";
}

function rowHtml(d) {
  const code = sunatCode(d);
  const msg = cleanMsg(d.mensaje);
  const retry = d.reintento
    ? `<button class="btn btn-secondary btn-sm" type="button" data-retry="${esc(d.clave)}">${retryLabel(d.reintento)}</button>` : "";
  return `
    <tr tabindex="0" data-clave="${esc(d.clave)}">
      <td class="c-doc"><span class="doc-id">${esc(d.serie_numero)}</span><span class="doc-type">${esc(tipoLabel(d.tipo))}</span></td>
      <td class="tnum" data-label="Emisión">${d.fecha_emision_original
        ? `<span class="date-old" title="Fecha del TXT original">${fmtDate(d.fecha_emision_original)}</span>${fmtDate(d.fecha_emision)}`
        : fmtDate(d.fecha_emision)}</td>
      <td class="tnum" data-label="Cliente">${esc(d.cliente_doc || "—")}${d.cliente_nombre ? `<span class="doc-type">${esc(d.cliente_nombre)}</span>` : ""}</td>
      <td class="num" data-label="Total">${fmtMoney(d.importe_total, d.moneda)}</td>
      <td class="c-state">${badge(d.estado)}</td>
      <td class="c-reply"><div class="reply" title="${esc(msg)}">${code ? `<span class="reply-code">${esc(code)}</span>` : ""}<span class="reply-text">${esc(msg || (code ? "" : "—"))}</span></div></td>
      <td class="when" data-label="Actualizado" title="${esc(fmtDateTime(d.actualizado_en))}">${fmtAgo(d.actualizado_en)}</td>
      <td class="c-actions row-actions">${retry}</td>
    </tr>`;
}

function hasFilters() { return !!(state.q || state.tipo || state.desde || state.hasta || state.group); }

function renderEmpty() {
  const st = $("state");
  $("rows").innerHTML = "";
  st.hidden = false;
  if (hasFilters()) {
    st.innerHTML = `
      <svg class="state-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5M8 11h6"/></svg>
      <h3>Ningún comprobante coincide con estos filtros</h3>
      <p>Prueba con otra serie o amplía las fechas de emisión.</p>
      <button class="btn btn-secondary" type="button" data-action="clear">Limpiar filtros</button>`;
  } else {
    st.innerHTML = `
      <svg class="state-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h10M4 17h7"/></svg>
      <h3>Todavía no hay comprobantes</h3>
      <p>Cuando se envíen comprobantes, aparecerán aquí con su estado en Sovos y SUNAT.</p>`;
  }
}

function renderError(msg) {
  const st = $("state");
  $("rows").innerHTML = "";
  st.hidden = false;
  st.innerHTML = `
    <svg class="state-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16.5v.5"/></svg>
    <h3>No se pudo cargar la lista</h3>
    <p>${esc(msg)}. Verifica que el servicio del puente esté corriendo.</p>
    <button class="btn btn-secondary" type="button" data-action="reload">Volver a cargar</button>`;
}

async function loadRows({ silent = false } = {}) {
  if (!silent) skeleton();
  try {
    const data = await api(`/api/documents?${queryString({ page: state.page, per_page: state.perPage })}`);
    state.total = data.total;
    if (!data.items.length) {
      if (state.page > 1 && data.total) { state.page = 1; return loadRows({ silent }); }
      renderEmpty();
    } else {
      $("state").hidden = true;
      $("rows").innerHTML = data.items.map(rowHtml).join("");
    }
    const from = data.total ? (state.page - 1) * state.perPage + 1 : 0;
    const to = Math.min(state.page * state.perPage, data.total);
    $("range").textContent = data.total ? `${from}–${to} de ${data.total.toLocaleString("es-PE")}` : "Sin resultados";
    $("prev").disabled = state.page <= 1;
    $("next").disabled = to >= data.total;
    $("export").href = `/api/export.csv?${queryString()}`;
    $("export-xlsx").href = `/api/export.xlsx?${queryString()}`;
    state.lastLoad = new Date().toISOString();
    tickRefreshed();
  } catch (err) {
    if (!silent) renderError(err.message);
    else toast(`No se pudo actualizar: ${err.message}`, "bad");
  }
}

async function loadSummary() {
  try { state.summary = await api("/api/summary"); renderSummary(state.summary); }
  catch (err) {
    $("service").dataset.alive = "false";
    $("service-text").textContent = "Sin conexión con el panel";
  }
}

function tickRefreshed() {
  $("refreshed").textContent = state.lastLoad ? `Actualizado ${fmtAgo(state.lastLoad)}` : "";
}

function syncSortHeaders() {
  document.querySelectorAll("#view-docs th").forEach((th) => th.removeAttribute("aria-sort"));
  const btn = document.querySelector(`#view-docs .sort[data-sort="${state.sort}"]`);
  if (btn) btn.closest("th").setAttribute("aria-sort", state.dir === "asc" ? "ascending" : "descending");
}

function syncFilterUi() {
  $("clear").hidden = !hasFilters();
  $("estado").value = state.group;
  syncGroupButtons();
}

function reload({ resetPage = true } = {}) {
  if (resetPage) state.page = 1;
  syncFilterUi();
  syncSortHeaders();
  loadRows();
}

// --- panel lateral ----------------------------------------------------------
let lastFocus = null;

function openDrawer() {
  lastFocus = document.activeElement;
  const d = $("drawer"), sc = $("scrim");
  d.hidden = false; sc.hidden = false;
  requestAnimationFrame(() => { d.classList.add("is-open"); sc.classList.add("is-open"); });
  $("drawer-close").focus();
}
function closeDrawer() {
  const d = $("drawer"), sc = $("scrim");
  d.classList.remove("is-open"); sc.classList.remove("is-open");
  state.openClave = null;
  setTimeout(() => { d.hidden = true; sc.hidden = true; }, 220);
  if (lastFocus) lastFocus.focus();
}

// Sovos informa la aceptación como "Authorized"; para SUNAT un comprobante aceptado es el código 0.
function sunatCode(d) {
  const c = d.codigo_sunat || "";
  return d.estado === "APROBADO" && !/^\d+$/.test(c) ? "0" : c;
}

function replyCallout(d) {
  if (d.estado === "APROBADO") {
    return `<div class="callout" data-tone="ok"><strong>Aceptado por SUNAT (código ${esc(sunatCode(d))})</strong>${d.mensaje ? `<p>${esc(d.mensaje)}</p>` : ""}</div>`;
  }
  if (!d.mensaje && !d.codigo_sunat) {
    const espera = { RECIBIDO: "El servicio lo está validando y armando para Sovos.", ENVIADO: "Sovos lo recibió; esperando la respuesta de SUNAT.", EN_PROCESO: "Sovos lo está procesando con SUNAT." }[d.estado];
    return espera ? `<div class="callout"><p>${espera}</p></div>` : "";
  }
  const tone = ESTADOS[d.estado]?.tone === "bad" ? "bad" : "warn";
  const ayuda = {
    RECHAZADO: ["1079", "2108"].includes(d.codigo_sunat)
      ? "SUNAT lo rechazó por su fecha de emisión. Reenviarlo con la misma fecha no sirve: al reenviarlo pasa a la bandeja de preparación para asignarle una fecha nueva."
      : "SUNAT no lo registró: el mismo número puede reenviarse una vez corregida la causa.",
    ERROR_CONEXION: d.document_id ? "Sovos sí lo recibió. Reanudar la consulta no lo reenvía." : "No llegó a Sovos o no hubo confirmación.",
    VALIDACION_ERROR: "El TXT tiene datos inválidos. Corregirlo en el sistema de facturación antes de reenviar.",
    DUPLICADO: "Sovos ya lo tenía registrado; no se volvió a enviar.",
  }[d.estado] || "";
  return `<div class="callout" data-tone="${tone}">
      ${d.codigo_sunat ? `<strong>Código SUNAT ${esc(d.codigo_sunat)}</strong>` : ""}
      <p>${esc(cleanMsg(d.mensaje) || "Sin mensaje")}</p>
      ${ayuda ? `<p class="hint">${ayuda}</p>` : ""}
      ${d.envio_incierto ? `<p class="hint">Envío incierto: confirmar en el portal de Sovos que no existe antes de reenviarlo.</p>` : ""}
    </div>`;
}

function fileButtons(d) {
  const files = [["pdf", "PDF"], ["xml", "XML firmado"], ["txt", "TXT"], ["sbd", "SBD enviado"]];
  return files.map(([k, label]) => d.archivos[k]
    ? `<a class="btn btn-secondary btn-sm" href="/api/documents/${encodeURIComponent(d.clave)}/archivo/${k}" ${k === "pdf" ? 'target="_blank" rel="noopener"' : "download"}>${label}</a>`
    : `<span class="btn btn-secondary btn-sm" aria-disabled="true" title="Aún no disponible">${label}</span>`).join("");
}

function timelineTone(estado) { return ESTADOS[estado]?.tone || ""; }

function renderDrawer(d, live) {
  $("drawer-title").textContent = d.serie_numero;
  $("drawer-sub").innerHTML = `${badge(d.estado)}<span>${esc(tipoLabel(d.tipo))}</span>`;
  const canConsult = d.document_id && state.summary && state.summary.puede_consultar;
  const retry = d.reintento
    ? `<button class="btn ${d.reintento === "forzado" ? "btn-danger" : "btn-primary"}" type="button" data-retry="${esc(d.clave)}">${retryLabel(d.reintento)}</button>` : "";
  const liveHtml = live ? `
      <section>
        <h3>Respuesta de Sovos, consultada ${fmtAgo(new Date().toISOString())}</h3>
        ${live.notificaciones.length ? `<ul class="live">${live.notificaciones.map((n) => `
          <li><time>${fmtDateTime(n.fecha)}</time><span>${badge(n.codigo_sovos === "AP" || n.codigo_sovos === "CA" ? "APROBADO" : n.codigo_sovos === "RE" ? "RECHAZADO" : "EN_PROCESO")}
          ${n.codigo_sunat ? `<span class="reply-code">${esc(n.codigo_sunat)}</span>` : ""}${esc(n.mensaje || "")}</span></li>`).join("")}</ul>`
          : `<p class="hint">Sovos todavía no tiene notificaciones para este comprobante.</p>`}
      </section>` : "";

  $("drawer-body").innerHTML = `
    ${replyCallout(d)}
    ${(retry || canConsult) ? `<div class="actions">${retry}${canConsult ? `<button class="btn btn-secondary" type="button" data-consult="${esc(d.clave)}">Consultar en Sovos</button>` : ""}</div>` : ""}
    ${liveHtml}
    <section>
      <h3>Datos</h3>
      <dl class="facts">
        <div><dt>Emisión</dt><dd>${fmtDate(d.fecha_emision)}</dd></div>
        ${d.fecha_emision_original ? `<div><dt>Emisión original (TXT)</dt><dd>${fmtDate(d.fecha_emision_original)}</dd></div>` : ""}
        <div><dt>Total</dt><dd>${fmtMoney(d.importe_total, d.moneda)}</dd></div>
        <div><dt>Cliente</dt><dd>${esc(d.cliente_nombre || "—")}<br>${esc({ "1": "DNI", "6": "RUC", "4": "CE", "7": "Pasaporte" }[d.cliente_tipo_doc] || "Doc.")} ${esc(d.cliente_doc || "—")}</dd></div>
        ${d.referencia ? `<div><dt>Modifica a</dt><dd>${esc(d.referencia)}</dd></div>` : ""}
        <div><dt>Intentos de envío</dt><dd>${d.intentos_envio}</dd></div>
        <div><dt>Enviado a Sovos</dt><dd>${fmtDateTime(d.enviado_en)}</dd></div>
        <div><dt>Respuesta Sovos</dt><dd>${esc(SOVOS_CODES[d.codigo_sovos] || d.codigo_sovos || "—")}</dd></div>
        <div class="wide"><dt>documentId en Sovos</dt><dd>${esc(d.document_id || "Sin asignar")}</dd></div>
        <div class="wide"><dt>Clave</dt><dd>${esc(d.clave)}</dd></div>
      </dl>
    </section>
    <section>
      <h3>Archivos</h3>
      <div class="files">${fileButtons(d)}</div>
    </section>
    <section>
      <h3>Historial</h3>
      <ol class="timeline">${[...d.eventos].reverse().map((e) => `
        <li data-tone="${timelineTone(e.estado)}">
          <time>${fmtDateTime(e.ts)}</time>
          <p>${e.estado ? `<span class="ev-state">${esc(ESTADOS[e.estado]?.label || e.estado)}</span>. ` : ""}${esc(e.detalle || "")}</p>
        </li>`).join("")}</ol>
    </section>`;
}

async function showDetail(clave, { keepLive = null } = {}) {
  const wasOpen = !!state.openClave;
  state.openClave = clave;
  if (!wasOpen) {
    $("drawer-title").textContent = "Cargando…";
    $("drawer-sub").textContent = "";
    $("drawer-body").innerHTML = `<span class="sk sk-w3"></span><span class="sk sk-w1"></span><span class="sk sk-w3"></span>`;
    openDrawer();
  }
  try {
    renderDrawer(await api(`/api/documents/${encodeURIComponent(clave)}`), keepLive);
  } catch (err) {
    $("drawer-body").innerHTML = `<div class="callout" data-tone="bad"><p>${esc(err.message)}</p></div>`;
  }
}

// --- confirmación -----------------------------------------------------------
function confirmModal({ title, html, ok, danger = false, check = null }) {
  return new Promise((resolve) => {
    const layer = $("modal"), okBtn = $("modal-ok"), cancel = $("modal-cancel");
    const checkWrap = $("modal-check"), checkInput = $("modal-check-input");
    const prevFocus = document.activeElement;
    $("modal-title").textContent = title;
    $("modal-text").innerHTML = html;
    okBtn.textContent = ok;
    okBtn.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
    checkWrap.hidden = !check;
    checkInput.checked = false;
    if (check) $("modal-check-text").textContent = check.text;
    okBtn.disabled = !!(check && check.required);
    checkInput.onchange = () => { if (check && check.required) okBtn.disabled = !checkInput.checked; };
    layer.hidden = false;
    requestAnimationFrame(() => layer.classList.add("is-open"));
    (check ? checkInput : cancel).focus();

    const done = (value) => {
      layer.classList.remove("is-open");
      setTimeout(() => { layer.hidden = true; }, 160);
      document.removeEventListener("keydown", onKey, true);
      if (prevFocus) prevFocus.focus();
      resolve(value);
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); done(null); }
      if (e.key === "Tab") {   // foco atrapado dentro del modal
        const f = [...layer.querySelectorAll("button:not(:disabled), input:not([hidden])")].filter((x) => x.offsetParent);
        if (!f.length) return;
        if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
        else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
      }
    };
    document.addEventListener("keydown", onKey, true);
    okBtn.onclick = () => done({ checked: checkInput.checked });
    cancel.onclick = () => done(null);
    layer.onclick = (e) => { if (e.target === layer) done(null); };
  });
}

// --- acciones ---------------------------------------------------------------
async function retryDoc(clave, btn) {
  const d = await api(`/api/documents/${encodeURIComponent(clave)}`);
  const copy = {
    consulta: {
      title: `Reanudar la consulta de ${d.serie_numero}`,
      html: `<p>Sovos ya tiene este comprobante (documentId ${esc(d.document_id)}). El servicio volverá a preguntar su estado; no se reenvía.</p>`,
      ok: "Reanudar consulta",
    },
    reenvio: {
      title: `Reenviar ${d.serie_numero} a Sovos`,
      html: `<p>El TXT vuelve a la bandeja y el servicio lo envía en la próxima revisión.</p>
             <p>Si SUNAT ya lo hubiera registrado, lo rechaza como duplicado (código 1033): no se emite dos veces.</p>`,
      ok: "Reenviar",
    },
    forzado: {
      title: `Reenviar ${d.serie_numero} sin confirmación de Sovos`,
      html: `<p>El primer envío quedó incierto: Sovos pudo haberlo recibido sin responder.</p>
             <p>Antes de continuar, busca el comprobante en el portal de Sovos (Perú, Emitidos). Reenvíalo solo si no aparece.</p>`,
      ok: "Reenviar", danger: true,
      check: { text: "Verifiqué en el portal de Sovos que este comprobante no existe", required: true },
    },
  }[d.reintento];
  if (!copy) { toast("Este comprobante no se puede reintentar en su estado actual", "warn"); return; }
  const answer = await confirmModal(copy);
  if (!answer) return;
  busy(btn, true);
  try {
    const r = await api(`/api/documents/${encodeURIComponent(clave)}/retry`, { method: "POST", body: { forzar: d.reintento === "forzado" } });
    const enPrep = r.ok && /fuera de plazo/.test(r.mensaje || "");
    toast(r.ok ? (d.reintento === "consulta" ? "Consulta reanudada"
      : enPrep ? "Fuera de plazo: quedó en la bandeja de preparación para asignarle una fecha nueva"
      : "Devuelto a la bandeja para reenvío") : r.mensaje, r.ok ? (enPrep ? "warn" : "ok") : "warn");
    await Promise.all([loadSummary(), loadRows({ silent: true })]);
    if (state.openClave === clave) showDetail(clave);
  } catch (err) {
    toast(err.message, "bad");
  } finally { busy(btn, false); }
}

async function consultDoc(clave, btn) {
  busy(btn, true);
  try {
    const r = await api(`/api/documents/${encodeURIComponent(clave)}/consultar`, { method: "POST" });
    renderDrawer(r.documento, r);
    toast(r.actualizado ? "Estado actualizado con la respuesta de Sovos" : "Respuesta de Sovos consultada");
    loadSummary(); loadRows({ silent: true });
  } catch (err) {
    toast(err.message, "bad");
    busy(btn, false);
  }
}

async function retryAll(btn) {
  const s = state.summary?.estados || {};
  const answer = await confirmModal({
    title: "Reintentar errores",
    html: `<p>Los errores de envío (${s.ERROR_CONEXION || 0}) vuelven a la bandeja; los que Sovos ya tiene solo reanudan la consulta. Los envíos inciertos se omiten: resuélvelos uno por uno.</p>`,
    ok: "Reintentar errores",
    check: { text: `Incluir también rechazados (${s.RECHAZADO || 0}) y errores de validación (${s.VALIDACION_ERROR || 0}) ya corregidos`, required: false },
  });
  if (!answer) return;
  busy(btn, true);
  try {
    const r = await api("/api/retry-errors", { method: "POST", body: { todos: answer.checked } });
    if (!r.reintentados && !r.omitidos) toast("No había comprobantes para reintentar", "warn");
    else toast(`${r.reintentados} reintentados${r.omitidos ? `, ${r.omitidos} omitidos (ver detalle de cada uno)` : ""}`, r.omitidos ? "warn" : "ok");
    await Promise.all([loadSummary(), loadRows({ silent: true })]);
  } catch (err) { toast(err.message, "bad"); }
  finally { busy(btn, false); }
}

// --- carga masiva de TXT ---------------------------------------------------
// Revisar no toca nada; al confirmar, el servidor deja los válidos en la bandeja
// y guarda una constancia (quién, cuándo, desde qué equipo y la huella de cada archivo).
const UPLOAD_CHUNK = 200;
const MAX_UPLOAD = 5000;
const WHO_KEY = "puente.responsable";
const RESULTADOS = {
  error:       { label: "Con error",        tone: "bad",  order: 0 },
  advertencia: { label: "Listo, con aviso", tone: "warn", order: 1 },
  valido:      { label: "Listo",            tone: "ok",   order: 2 },
  ya_enviado:  { label: "Ya enviado",       tone: "idle", order: 3 },
  en_cola:     { label: "Ya en cola",       tone: "idle", order: 4 },
  en_bandeja:  { label: "Ya en la bandeja", tone: "idle", order: 5 },
};
const upload = { files: [], results: [], done: false, busy: false, cargas: [], sent: new Set(),
                 excluded: new Set(), filter: "todos", q: "", page: 1 };

function pill(label, tone) { return `<span class="badge" data-tone="${tone}">${esc(label)}</span>`; }
function plural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }
function storedWho() { try { return localStorage.getItem(WHO_KEY) || ""; } catch { return ""; } }
function rememberWho(v) { try { localStorage.setItem(WHO_KEY, v); } catch { /* sin almacenamiento */ } }

function openUpload() {
  resetUpload();
  const layer = $("upload");
  layer.hidden = false;
  requestAnimationFrame(() => layer.classList.add("is-open"));
  loadHistory();
  $("upload-input").focus();
}
function closeUpload() {
  if (upload.busy) return;
  const layer = $("upload");
  layer.classList.remove("is-open");
  setTimeout(() => { layer.hidden = true; }, 160);
  $("open-upload").focus();
}
function resetUpload() {
  Object.assign(upload, { files: [], results: [], done: false, cargas: [], sent: new Set(),
                          excluded: new Set(), filter: "todos", q: "", page: 1 });
  $("upload-q").value = "";
  $("upload-input").value = "";
  $("upload-more-input").value = "";
  $("upload-body").hidden = false;
  $("dropzone").classList.remove("is-busy");
  $("dropzone-progress").hidden = true;
  $("upload-results").hidden = true;
  $("receipt").hidden = true;
  $("upload-reset").hidden = true;
  $("upload-more").hidden = true;
  $("upload-who-field").hidden = false;
  $("upload-who").value = $("upload-who").value || storedWho();
  whoInvalid(false);
  $("upload-send").disabled = true;
  $("upload-send").textContent = "Pasar a la bandeja";
  $("upload-separar-wrap").hidden = true;
  $("upload-separar").checked = true;
  $("upload-sub").textContent = "Se revisan y quedan en la bandeja: nada sale a Sovos hasta que los liberes.";
  serviceNote();
}
function serviceNote() {
  const note = $("upload-note");
  const s = state.summary;
  if (s && !s.servicio.activo) {
    note.dataset.tone = "warn";
    note.textContent = "El servicio de envío está detenido: podrás preparar la bandeja, pero nada saldrá hasta que se inicie.";
  } else {
    note.dataset.tone = "";
    note.textContent = "Quedan en la bandeja. Ahí decides qué enviar, en qué orden y con qué fecha.";
  }
}
function whoInvalid(on) {
  $("upload-who-field").classList.toggle("is-invalid", on);
  $("upload-who-error").hidden = !on;
  $("upload-who").setAttribute("aria-invalid", on ? "true" : "false");
}

async function loadHistory() {
  try {
    const { cargas } = await api("/api/cargas");
    const list = cargas.slice(0, 8);
    $("history").hidden = !list.length;
    $("history-list").innerHTML = list.map((c) => `<li>
      <span class="history-num">N.º ${c.id}</span>
      <span class="history-who">${esc(fmtDateTime(c.ts))}, ${esc(c.usuario)}</span>
      <span class="history-count">${c.encolados} de ${plural(c.total, "archivo", "archivos")} a la bandeja</span>
      <a href="/api/cargas/${c.id}.csv" download>Constancia</a>
    </li>`).join("");
    // Escalonado por CSSOM: la CSP del panel no permite atributos style en línea.
    $("history-list").querySelectorAll("li").forEach((li, i) => li.style.setProperty("--i", i));
  } catch { $("history").hidden = true; }
}

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",", 2)[1] || "");
    r.onerror = () => reject(new Error(`No se pudo leer ${file.name}`));
    r.readAsDataURL(file);
  });
}

// Archivos soltados, incluidas carpetas (se recorren completas).
// Las entradas se piden en el mismo evento: después el navegador las descarta.
async function droppedFiles(dt) {
  const entries = [...(dt.items || [])]
    .filter((it) => it.kind === "file")
    .map((it) => (it.webkitGetAsEntry ? it.webkitGetAsEntry() : null));
  if (!entries.length || entries.some((e) => !e)) return [...dt.files];
  const out = [];
  const walk = async (entry) => {
    if (entry.isFile) { out.push(await new Promise((res, rej) => entry.file(res, rej))); return; }
    if (!entry.isDirectory) return;
    const reader = entry.createReader();
    for (;;) {
      const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
      if (!batch.length) break;
      for (const e of batch) await walk(e);
    }
  };
  for (const e of entries) await walk(e);
  return out;
}

async function postChunks(modo, files, extra = {}) {
  const archivos = [], cargas = [];
  for (let i = 0; i < files.length; i += UPLOAD_CHUNK) {
    const r = await api("/api/upload", { method: "POST", body: { modo, ...extra, archivos: files.slice(i, i + UPLOAD_CHUNK) } });
    archivos.push(...r.archivos);
    if (r.carga) cargas.push(r.carga);
  }
  return { archivos, cargas };
}

async function handleFiles(list, { append = false } = {}) {
  if (upload.busy) { toast("Espera a que termine la revisión en curso", "warn"); return; }
  if (upload.done) { resetUpload(); append = false; }
  const all = [...list];
  const picked = all.filter((f) => /\.txt$/i.test(f.name));
  const skipped = all.length - picked.length;
  if (!picked.length) {
    toast(all.length ? "Ninguno es .txt: elige los TXT del sistema de facturación" : "No llegó ningún archivo", "warn");
    return;
  }
  const base = append ? upload.files : [];
  if (base.length + picked.length > MAX_UPLOAD) {
    toast(`Se pueden revisar hasta ${MAX_UPLOAD} archivos por vez (llegaron ${base.length + picked.length}); súbelos en partes`, "warn");
    return;
  }

  upload.busy = true;
  const dz = $("dropzone"), prog = $("dropzone-progress");
  dz.classList.add("is-busy");
  prog.hidden = false;
  $("upload-send").disabled = true;
  try {
    let leidos = 0;
    prog.textContent = `Leyendo 0 de ${picked.length}…`;
    const nuevos = await Promise.all(picked.map(async (f) => {
      const contenido = await readAsBase64(f);
      prog.textContent = `Leyendo ${++leidos} de ${picked.length}…`;
      return { nombre: f.name, contenido };
    }));

    // Mismo nombre: si el contenido es idéntico se ignora; si cambia, se conserva el primero.
    const byName = new Map(base.map((f) => [f.nombre, f]));
    let iguales = 0, choques = 0;
    for (const f of nuevos) {
      const prev = byName.get(f.nombre);
      if (!prev) byName.set(f.nombre, f);
      else if (prev.contenido === f.contenido) iguales += 1;
      else choques += 1;
    }
    upload.files = [...byName.values()];
    prog.textContent = `Revisando ${plural(upload.files.length, "archivo", "archivos")}…`;
    $("upload-sub").textContent = prog.textContent;
    upload.results = markRepeated((await postChunks("revisar", upload.files)).archivos);
    $("upload-body").hidden = true;
    renderUpload();

    const avisos = [];
    if (skipped) avisos.push(`${plural(skipped, "archivo no es", "archivos no son")} .txt y no se incluyeron`);
    if (iguales) avisos.push(`${plural(iguales, "archivo ya estaba", "archivos ya estaban")} en la lista`);
    if (choques) avisos.push(`${plural(choques, "archivo tiene", "archivos tienen")} el mismo nombre que otro pero distinto contenido: se dejó el primero`);
    if (avisos.length) toast(avisos.join(". "), "warn");
  } catch (err) {
    toast(err.message, "bad");
    if (!append) resetUpload();
  } finally {
    upload.busy = false;
    dz.classList.remove("is-busy");
    prog.hidden = true;
    syncUploadButton();
  }
}

// La revisión va en partes de UPLOAD_CHUNK: los repetidos entre partes se marcan aquí.
function markRepeated(results) {
  const seen = new Set();
  results.forEach((r) => {
    if (!r.clave || !r.encolable) return;
    if (seen.has(r.clave)) {
      Object.assign(r, { resultado: "error", encolable: false, mensajes: ["Repetido en esta misma carga", ...(r.mensajes || [])] });
    }
    seen.add(r.clave);
  });
  return results;
}

// Filtros de la revisión: por resultado, y dos atajos útiles antes de pasar a la bandeja.
const UPLOAD_PAGE = 100;
const UPLOAD_EXTRA = {
  fuera_plazo: { label: "Fuera de plazo", tone: "bad", match: (r) => r.encolable && r.fuera_plazo },
  especiales: { label: "Inafecto, exonerado u otros", tone: "warn", match: (r) => r.encolable && r.especiales && r.especiales.length },
};
function uploadMatch(r) {
  const f = upload.filter;
  const okF = f === "todos" || (UPLOAD_EXTRA[f] ? UPLOAD_EXTRA[f].match(r) : r.resultado === f);
  const q = upload.q.toUpperCase();
  return okF && (!q || (r.serie_numero || "").toUpperCase().includes(q) || r.nombre.toUpperCase().includes(q));
}
function uploadFiltered() { return upload.results.filter(uploadMatch); }

function renderUpload() {
  const res = [...upload.results].sort((a, b) => RESULTADOS[a.resultado].order - RESULTADOS[b.resultado].order);
  upload.results = res;
  const counts = { todos: res.length };
  res.forEach((r) => { counts[r.resultado] = (counts[r.resultado] || 0) + 1; });
  const chips = [["todos", "Todos", "idle"], ...Object.keys(RESULTADOS).filter((k) => counts[k]).map((k) => [k, RESULTADOS[k].label, RESULTADOS[k].tone])];
  if (!upload.done) {
    Object.entries(UPLOAD_EXTRA).forEach(([k, x]) => {
      counts[k] = res.filter(x.match).length;
      if (counts[k]) chips.push([k, x.label, x.tone]);
    });
  }
  if (!chips.some(([k]) => k === upload.filter)) upload.filter = "todos";
  $("upload-chips").innerHTML = chips.map(([k, label, tone]) =>
    `<button class="chip" type="button" role="tab" data-upload-chip="${k}" data-tone="${tone}" aria-selected="${upload.filter === k}">
      ${esc(label)} <span class="chip-n">${(counts[k] || 0).toLocaleString("es-PE")}</span></button>`).join("");

  const rows = uploadFiltered();
  const pages = Math.max(1, Math.ceil(rows.length / UPLOAD_PAGE));
  upload.page = Math.min(Math.max(upload.page, 1), pages);
  const start = (upload.page - 1) * UPLOAD_PAGE;
  const page = rows.slice(start, start + UPLOAD_PAGE);
  $("upload-rows").innerHTML = page.map((r) => {
    const meta = RESULTADOS[r.resultado];
    const check = r.encolable && !upload.done
      ? `<input class="row-check" type="checkbox" data-upload="${esc(r.nombre)}" ${upload.excluded.has(r.nombre) ? "" : "checked"} aria-label="Incluir ${esc(r.nombre)}">` : "";
    const emision = r.fecha_emision
      ? `${fmtDate(r.fecha_emision)}${r.dias_desde_emision > 0 ? `<span class="file-sub">hace ${r.dias_desde_emision} días</span>` : ""}` : "—";
    const enviado = upload.done && upload.sent.has(r.nombre) && r.encolable;
    const separado = enviado && (r.mensajes[0] || "").startsWith("Separado");
    return `<tr>
      <td>${check}</td>
      <td><span class="file-name">${esc(r.serie_numero || r.nombre)}</span><span class="file-sub">${esc(r.serie_numero ? `${tipoLabel(r.tipo)}, ${r.nombre}` : "")}</span></td>
      <td class="tnum">${emision}</td>
      <td class="num">${r.importe_total ? fmtMoney(r.importe_total, r.moneda) : "—"}</td>
      <td>${enviado ? pill(separado ? "Separado" : "En la bandeja", separado ? "warn" : "info") : pill(meta.label, meta.tone)}</td>
      <td><ul class="msgs">${(r.mensajes || []).map((m) => `<li>${esc(m)}</li>`).join("") || "<li>Sin observaciones</li>"}</ul></td>
    </tr>`;
  }).join("");
  $("upload-empty").hidden = rows.length > 0;
  $("upload-range").textContent = rows.length
    ? `${(start + 1).toLocaleString("es-PE")}–${(start + page.length).toLocaleString("es-PE")} de ${rows.length.toLocaleString("es-PE")}` : "";
  $("upload-prev").disabled = upload.page <= 1;
  $("upload-next").disabled = upload.page >= pages;
  $("upload-prev").closest(".upload-pager").hidden = rows.length <= UPLOAD_PAGE;
  $("upload-all").hidden = upload.done || !rows.some((r) => r.encolable);
  $("upload-results").hidden = false;
  $("upload-reset").hidden = false;
  $("upload-more").hidden = upload.done;
  $("upload-who-field").hidden = upload.done;
  $("upload-sub").textContent = upload.done
    ? "Carga terminada. La constancia queda guardada; sigue en la bandeja para decidir qué enviar."
    : "Revisa el resultado. Desmarca los que no quieras pasar todavía; puedes soltar más archivos aquí.";
  renderReceipt();
  syncUploadButton();
}

function renderReceipt() {
  const el = $("receipt");
  if (!upload.done || !upload.cargas.length) { el.hidden = true; return; }
  const c0 = upload.cargas[0];
  const enc = upload.cargas.reduce((n, c) => n + c.encolados, 0);
  const tot = upload.cargas.reduce((n, c) => n + c.total, 0);
  const nums = upload.cargas.map((c) => `N.º ${c.id}`).join(", ");
  el.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="m8.5 12.5 2.5 2.5 4.5-5"/></svg>
    <div class="receipt-text">
      <strong>Carga registrada: constancia ${esc(nums)}</strong>
      <span>${esc(fmtDateTime(c0.ts))}, ${esc(c0.usuario)}, desde ${esc(c0.origen || "este equipo")}. ${enc} de ${plural(tot, "archivo", "archivos")} dejados en la bandeja.</span>
    </div>
    ${upload.cargas.map((c) => `<a class="btn btn-secondary" href="/api/cargas/${c.id}.csv" download>Descargar constancia${upload.cargas.length > 1 ? ` N.º ${c.id}` : ""}</a>`).join("")}`;
  el.hidden = false;
}

function selectedUploads() {
  return upload.results.filter((r) => r.encolable && !upload.excluded.has(r.nombre));
}
function syncUploadButton() {
  const btn = $("upload-send");
  const wrap = $("upload-separar-wrap");
  if (upload.done) { btn.disabled = false; btn.textContent = "Ir a la bandeja"; wrap.hidden = true; return; }
  const chosen = selectedUploads();
  const n = chosen.length;
  const esp = chosen.filter((r) => r.especiales && r.especiales.length).length;
  wrap.hidden = esp === 0;
  $("upload-separar-text").textContent = `Separar ${esp === 1 ? "el que tiene" : `los ${esp} que tienen`} inafecto, exonerado, gratuito o exportación: no se envían hasta que lo indiques`;
  // La casilla del encabezado refleja lo que se ve con el filtro actual
  const visibles = uploadFiltered().filter((r) => r.encolable);
  const marcados = visibles.filter((r) => !upload.excluded.has(r.nombre)).length;
  const all = $("upload-all");
  all.checked = visibles.length > 0 && marcados === visibles.length;
  all.indeterminate = marcados > 0 && marcados < visibles.length;
  btn.disabled = n === 0 || upload.busy;
  btn.textContent = n ? `Pasar ${n.toLocaleString("es-PE")} a la bandeja` : "Nada para pasar";
}

async function sendUploads(btn) {
  if (upload.done) { closeUpload(); showView("tray"); return; }
  const who = $("upload-who").value.trim().replace(/\s+/g, " ");
  if (!who) { whoInvalid(true); $("upload-who").focus(); return; }
  rememberWho(who);
  const chosen = new Set(selectedUploads().map((r) => r.nombre));
  const files = upload.files.filter((f) => chosen.has(f.nombre));
  if (!files.length) return;
  busy(btn, true);
  upload.busy = true;
  try {
    const { archivos, cargas } = await postChunks("encolar", files, { responsable: who, separar_especiales: $("upload-separar").checked });
    const ok = archivos.filter((r) => r.encolable).length;
    const byName = new Map(archivos.map((r) => [r.nombre, r]));
    upload.results = upload.results.map((r) => byName.get(r.nombre) || r);
    Object.assign(upload, { done: true, sent: chosen, cargas });
    renderUpload();
    toast(ok === files.length ? `${plural(ok, "archivo quedó", "archivos quedaron")} en la bandeja; constancia guardada`
      : `${ok} de ${files.length} quedaron en la bandeja; revisa el detalle`, ok === files.length ? "ok" : "warn");
    loadSummary();
    if (currentView() === "tray") loadTray({ silent: true });
  } catch (err) {
    toast(err.message, "bad");
  } finally { upload.busy = false; busy(btn, false); syncUploadButton(); }
}

function bindUpload() {
  $("open-upload").addEventListener("click", openUpload);
  $("upload-close").addEventListener("click", closeUpload);
  $("upload-reset").addEventListener("click", () => { resetUpload(); loadHistory(); });
  $("upload-send").addEventListener("click", (e) => sendUploads(e.currentTarget));
  $("upload-input").addEventListener("change", (e) => handleFiles(e.target.files));
  $("upload-more-input").addEventListener("change", (e) => { handleFiles(e.target.files, { append: true }); e.target.value = ""; });
  $("upload-who").addEventListener("input", () => whoInvalid(false));
  $("upload-rows").addEventListener("change", (e) => {
    const c = e.target.closest("[data-upload]");
    if (!c) return;
    if (c.checked) upload.excluded.delete(c.dataset.upload); else upload.excluded.add(c.dataset.upload);
    syncUploadButton();
  });
  $("upload-all").addEventListener("change", (e) => {
    uploadFiltered().filter((r) => r.encolable).forEach((r) => {
      if (e.target.checked) upload.excluded.delete(r.nombre); else upload.excluded.add(r.nombre);
    });
    renderUpload();
  });
  $("upload-chips").addEventListener("click", (e) => {
    const c = e.target.closest("[data-upload-chip]");
    if (!c) return;
    upload.filter = c.dataset.uploadChip; upload.page = 1;
    renderUpload();
  });
  $("upload-q").addEventListener("input", debounce((e) => { upload.q = e.target.value.trim(); upload.page = 1; renderUpload(); }, 200));
  $("upload-prev").addEventListener("click", () => { upload.page -= 1; renderUpload(); });
  $("upload-next").addEventListener("click", () => { upload.page += 1; renderUpload(); });
  $("upload").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); closeUpload(); } });
  bindGlobalDrop();
}

// Soltar archivos o carpetas en cualquier parte del panel abre la carga.
function bindGlobalDrop() {
  const overlay = $("drop-overlay");
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  const reviewing = () => !$("upload").hidden && upload.results.length > 0 && !upload.done;
  const show = (on) => {
    if (on) {
      overlay.querySelector("strong").textContent = currentView() === "config" && $("upload").hidden
        ? "Suelta el certificado (.pfx o .p12)"
        : reviewing() ? "Suelta para agregarlos a la revisión" : "Suelta para revisar los TXT";
      overlay.hidden = false;
      requestAnimationFrame(() => overlay.classList.add("is-on"));
    } else {
      overlay.classList.remove("is-on");
      setTimeout(() => { if (!overlay.classList.contains("is-on")) overlay.hidden = true; }, 160);
    }
  };
  window.addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    if (depth++ === 0) show(true);
  });
  window.addEventListener("dragover", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = upload.busy || !$("modal").hidden ? "none" : "copy";
  });
  window.addEventListener("dragleave", (e) => {
    if (!hasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) show(false);
  });
  window.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();             // sin esto el navegador abriría el TXT y se saldría del panel
    depth = 0;
    show(false);
    if (upload.busy || !$("modal").hidden || !$("date-modal").hidden) return;
    if (currentView() === "config" && $("upload").hidden) {
      const f = [...e.dataTransfer.files].find((x) => /\.(pfx|p12)$/i.test(x.name));
      if (f) { setCertFile(f); return; }
    }
    const append = reviewing();
    const pending = droppedFiles(e.dataTransfer);
    if ($("upload").hidden) openUpload();
    pending.then((files) => handleFiles(files, { append }))
      .catch(() => toast("No se pudieron leer los archivos soltados", "bad"));
  });
}

// --- vistas -----------------------------------------------------------------
const VIEW_KEY = "puente.vista";
const VIEWS = ["tray", "docs", "config"];
function currentView() { return VIEWS.find((v) => !$(`view-${v}`).hidden) || "docs"; }
function showView(view) {
  if (!VIEWS.includes(view)) view = "docs";
  VIEWS.forEach((v) => {
    $(`view-${v}`).hidden = v !== view;
    $(`tab-${v}`).setAttribute("aria-selected", String(v === view));
  });
  const page = $(`view-${view}`);
  page.classList.add("is-entering");
  setTimeout(() => page.classList.remove("is-entering"), 400);
  try { localStorage.setItem(VIEW_KEY, view); } catch { /* sin almacenamiento */ }
  if (state.summary) renderConfigBanner(state.summary.config_pendiente || []);
  if (view === "tray") { $("tray-who").value = $("tray-who").value || storedWho(); loadTray(); }
  else if (view === "config") { $("config-who").value = $("config-who").value || storedWho(); loadConfig(); }
  else loadRows({ silent: true });
}

// --- bandeja de preparación -------------------------------------------------
// Lo subido espera aquí: se separan especiales, se corrige la fecha de lo vencido
// y se libera por tipo. Las notas esperan solas a que su comprobante esté aprobado.
const tray = {
  rows: [], fechas: {}, plazos: {}, hoy: "", rechazados: 0, loaded: false,
  chip: "todos", tipo: "", q: "", desde: "", hasta: "", page: 1, perPage: 50,
  sort: "fecha_emision", dir: "asc", selected: new Set(),
};
// Orden: por defecto el mismo en que se envían (fecha efectiva y serie).
const TRAY_SORT = {
  serie_numero: (r) => r.serie_numero,
  fecha_emision: (r) => `${r.fecha_efectiva}|${r.serie_numero}`,
  dias_restantes: (r) => r.dias_restantes,
  importe_total: (r) => Number(r.importe_total) || 0,
};
const CHIPS = [
  { key: "todos", label: "Todos" },
  { key: "listo", label: "Listos" },
  { key: "fuera_plazo", label: "Fuera de plazo" },
  { key: "separado", label: "Separados" },
  { key: "esperando", label: "Notas esperando" },
];
const ESPECIAL = { inafecto: "Inafecto", exonerado: "Exonerado", gratuito: "Gratuito", exportacion: "Exportación" };
const REF_ESTADO = {
  APROBADO: "aprobado", EN_BANDEJA: "en la bandeja", EN_COLA: "en cola de envío", DESCONOCIDA: "no pasó por el puente",
  ENVIADO: "en Sovos", EN_PROCESO: "en Sovos", RECHAZADO: "rechazado", VALIDACION_ERROR: "con error",
  ERROR_CONEXION: "con error de envío", RECIBIDO: "procesándose", DUPLICADO: "duplicado",
};
const STEP_DEFS = [
  { key: "03", name: "Boletas", one: "boleta", many: "boletas", match: (r) => r.tipo === "03" },
  { key: "01", name: "Facturas", one: "factura", many: "facturas", match: (r) => r.tipo === "01" },
  { key: "nota", name: "Notas", one: "nota", many: "notas", match: (r) => isNota(r) },
];
const WEEKDAYS = ["domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"];

function isNota(r) { return r.tipo === "07" || r.tipo === "08"; }
function addDays(iso, n) {
  const d = new Date(`${iso}T12:00:00`);
  d.setDate(d.getDate() + n);
  return d.toISOString().slice(0, 10);
}
function dayLabel(iso) {
  const d = new Date(`${iso}T12:00:00`);
  const rel = iso === tray.hoy ? "Hoy, " : iso === addDays(tray.hoy, -1) ? "Ayer, " : "";
  return `${rel}${WEEKDAYS[d.getDay()]} ${fmtDate(iso)}`;
}
function fmtCount(n, one, many) { return `${n.toLocaleString("es-PE")} ${n === 1 ? one : many}`; }

function trayWho() {
  const v = $("tray-who").value.trim().replace(/\s+/g, " ");
  $("tray-who-field").classList.toggle("is-invalid", !v);
  if (!v) { $("tray-who").focus(); toast("Escribe quién opera: queda en el historial de cada comprobante", "warn"); return null; }
  rememberWho(v);
  return v;
}

async function loadTray({ silent = false } = {}) {
  if (!tray.loaded && !silent) traySkeleton();
  try {
    const d = await api("/api/bandeja");
    Object.assign(tray, { rows: d.filas, fechas: d.fechas, plazos: d.plazos, hoy: d.hoy,
                          rechazados: d.rechazados_por_fecha, recuperables: d.recuperables || [],
                          porResumen: !!d.boletas_por_resumen, loaded: true });
    const present = new Set(tray.rows.map((r) => r.clave));
    tray.selected = new Set([...tray.selected].filter((c) => present.has(c)));
    renderTray();
  } catch (err) {
    if (!silent) trayError(err.message);
  }
}

function traySkeleton() {
  $("tray-state").hidden = true;
  $("tray-rows").innerHTML = Array.from({ length: 6 }, () => `
    <tr aria-hidden="true"><td></td><td><span class="sk sk-w1"></span></td><td><span class="sk sk-w2"></span></td>
    <td><span class="sk sk-w2"></span></td><td class="num"><span class="sk sk-w4"></span></td><td><span class="sk sk-w3"></span></td></tr>`).join("");
}
function trayError(msg) {
  $("tray-rows").innerHTML = "";
  const st = $("tray-state");
  st.hidden = false;
  st.innerHTML = `
    <svg class="state-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16.5v.5"/></svg>
    <h3>No se pudo cargar la bandeja</h3><p>${esc(msg)}. Verifica que el puente esté corriendo.</p>
    <button class="btn btn-secondary" type="button" data-tray-action="reload">Volver a cargar</button>`;
}

function trayHasFilters() { return !!(tray.chip !== "todos" || tray.tipo || tray.q || tray.desde || tray.hasta); }

function trayFiltered() {
  const q = tray.q.toUpperCase();
  const key = TRAY_SORT[tray.sort] || TRAY_SORT.fecha_emision;
  const sign = tray.dir === "desc" ? -1 : 1;
  return tray.rows.filter((r) => (tray.chip === "todos" || r.grupo === tray.chip)
    && (!tray.tipo || (tray.tipo === "nota" ? isNota(r) : r.tipo === tray.tipo))
    && (!q || r.serie_numero.includes(q))
    && (!tray.desde || r.fecha_emision >= tray.desde)
    && (!tray.hasta || r.fecha_emision <= tray.hasta))
    .sort((a, b) => {
      const x = key(a), y = key(b);
      return (x < y ? -1 : x > y ? 1 : 0) * sign;
    });
}

function syncTrayFilterUi() {
  $("tray-clear").hidden = !trayHasFilters();
  $("tray-tipo").value = tray.tipo;
  $("tray-desde").value = tray.desde;
  $("tray-hasta").value = tray.hasta;
  document.querySelectorAll("#view-tray th").forEach((th) => th.removeAttribute("aria-sort"));
  const b = document.querySelector(`[data-tray-sort="${tray.sort}"]`);
  if (b) b.closest("th").setAttribute("aria-sort", tray.dir === "asc" ? "ascending" : "descending");
}

function renderTray() {
  renderTrayAlerts();
  renderTraySteps();
  renderTrayChips();
  renderTrayTable();
}

function renderTrayAlerts() {
  const R = tray.rows;
  const by = (g) => R.filter((r) => r.grupo === g);
  const fuera = by("fuera_plazo"), sep = by("separado"), esp = by("esperando");
  const hoy = R.filter((r) => r.grupo === "listo" && r.dias_restantes === 0);
  const sinRef = R.filter((r) => r.grupo === "listo" && r.referencia && r.referencia.estado === "DESCONOCIDA");
  const cards = [];
  if (fuera.length) {
    const fac = fuera.filter((r) => r.familia === "factura").length;
    const partes = [fac && fmtCount(fac, "factura o nota de factura", "facturas o notas de factura"),
                    fuera.length - fac && fmtCount(fuera.length - fac, "boleta o nota de boleta", "boletas o notas de boleta")].filter(Boolean);
    const f = fuera.map((r) => r.fecha_efectiva).sort();
    cards.push({ tone: "bad", n: fuera.length, title: "Fuera de plazo",
      text: `${partes.join(" y ")}, emitidas del ${fmtDate(f[0])} al ${fmtDate(f[f.length - 1])}. Con su fecha original SUNAT las rechazaría. El puente les propone una fecha nueva a cada uno, sin desordenar la numeración.`,
      actions: [["Asignar fechas automáticamente", "date-auto", true], ["Elegir una fecha a mano", "date-fuera"], ["Ver cuáles", "chip:fuera_plazo"]] });
  }
  if (hoy.length) {
    cards.push({ tone: "warn", n: hoy.length, title: "Vencen hoy",
      text: "Si no salen hoy quedarán fuera de plazo. Conviene enviarlos antes que el resto.",
      actions: [["Enviar ahora", "send-hoy", true]] });
  }
  if (sep.length) {
    const kinds = {};
    sep.forEach((r) => r.especiales.forEach((e) => { kinds[e] = (kinds[e] || 0) + 1; }));
    const detalle = Object.entries(kinds).map(([k, n]) => `${n} con ${ESPECIAL[k].toLowerCase()}`).join(", ");
    cards.push({ tone: "warn", n: sep.length, title: "Separados",
      text: `${detalle || "Separados a mano"}. No se envían hasta que tú lo indiques.`,
      actions: [["Ver cuáles", "chip:separado"]] });
  }
  if (sinRef.length) {
    cards.push({ tone: "warn", n: sinRef.length, title: "Notas sin su comprobante aquí",
      text: "Modifican comprobantes que no se subieron al puente. Si se emitieron con el sistema anterior se pueden enviar; si no, sube primero el comprobante.",
      actions: [["Ver cuáles", "sinref"]] });
  }
  if (esp.length) {
    cards.push({ tone: "info", n: esp.length, title: "Notas esperando a su comprobante",
      text: "Salen solas en cuanto SUNAT apruebe el comprobante que modifican.",
      actions: [["Ver cuáles", "chip:esperando"]] });
  }
  if (tray.rechazados) {
    cards.push({ tone: "bad", n: tray.rechazados, title: "Rechazados para corregir",
      text: "SUNAT los rechazó por su fecha (2108 en facturas, 1079 en boletas). Elige el rango de fechas y los tipos que quieres corregir: vuelven a la bandeja con una fecha nueva propuesta.",
      actions: [["Elegir cuáles traer", "bring-back", true]] });
  }
  if (!cards.length && R.length) {
    cards.push({ tone: "ok", n: R.length, title: "Todo en orden",
      text: "Nada fuera de plazo ni separado. Puedes enviar por tipo.", actions: [] });
  }
  $("tray-alerts").innerHTML = cards.map((c) => `
    <article class="tray-alert" data-tone="${c.tone}">
      <span class="tray-alert-num">${c.n.toLocaleString("es-PE")}</span>
      <span class="tray-alert-title">${esc(c.title)}</span>
      <p>${esc(c.text)}</p>
      ${c.actions.length ? `<div class="tray-alert-actions">${c.actions.map(([label, act, primary]) =>
        `<button class="btn ${primary ? "btn-primary" : "btn-secondary"} btn-sm" type="button" data-tray-action="${act}">${esc(label)}</button>`).join("")}</div>` : ""}
    </article>`).join("");
  $("tray-alerts").querySelectorAll(".tray-alert").forEach((el, i) => el.style.setProperty("--i", i));
}

function renderTraySteps() {
  $("tray-steps").innerHTML = STEP_DEFS.map((s) => {
    const all = tray.rows.filter(s.match);
    const listos = all.filter((r) => r.grupo === "listo");
    const otros = all.length - listos.length;
    const sub = all.length
      ? `${fmtCount(listos.length, "lista", "listas")}${otros ? `, ${otros.toLocaleString("es-PE")} por revisar` : ""}`
      : "Ninguna en la bandeja";
    const nombre = s.key === "03" && tray.porResumen ? `${s.name} (resumen diario)` : s.name;
    return `<li class="tray-step" data-empty="${listos.length === 0}">
      <span class="tray-step-name">${nombre}</span>
      <span class="tray-step-sub">${sub}</span>
      <button class="btn ${listos.length ? "btn-primary" : "btn-secondary"} btn-sm" type="button" data-tray-action="send-${s.key}" ${listos.length ? "" : "disabled"}>
        ${listos.length ? `Enviar ${fmtCount(listos.length, s.one, s.many)}` : "Nada para enviar"}</button>
    </li>`;
  }).join("");
}

function renderTrayChips() {
  const counts = { todos: tray.rows.length };
  tray.rows.forEach((r) => { counts[r.grupo] = (counts[r.grupo] || 0) + 1; });
  $("tray-chips").innerHTML = CHIPS.filter((c) => c.key === "todos" || counts[c.key] || tray.chip === c.key).map((c) =>
    `<button class="chip" type="button" role="tab" data-chip="${c.key}" aria-selected="${tray.chip === c.key}">
      ${c.label} <span class="chip-n">${(counts[c.key] || 0).toLocaleString("es-PE")}</span></button>`).join("");
}

function plazoHtml(r) {
  const hasta = r.vence ? `hasta el ${fmtDate(r.vence)}` : "";
  if (r.dias_restantes < 0) return `<span class="plazo" data-tone="bad">Venció hace ${fmtCount(-r.dias_restantes, "día", "días")}<span class="plazo-sub">SUNAT lo aceptaba ${hasta}</span></span>`;
  if (r.dias_restantes === 0) return `<span class="plazo" data-tone="warn">Vence hoy<span class="plazo-sub">${r.plazo_dias} días de plazo</span></span>`;
  if (r.dias_restantes === 1) return `<span class="plazo" data-tone="warn">Vence mañana<span class="plazo-sub">${hasta}</span></span>`;
  return `<span class="plazo">Quedan ${r.dias_restantes} días<span class="plazo-sub">${hasta}</span></span>`;
}

function situacionHtml(r) {
  const head = {
    listo: pill("Listo para enviar", "ok"), fuera_plazo: pill("Fuera de plazo", "bad"),
    separado: pill("Separado", "warn"), esperando: pill("Esperando su comprobante", "info"),
  }[r.grupo];
  const tags = r.especiales.length ? `<span class="tags">${r.especiales.map((e) => `<span class="tag">${ESPECIAL[e]}</span>`).join("")}</span>` : "";
  const ref = r.referencia ? `<small>Modifica ${esc(r.referencia.serie_numero)}, ${esc(REF_ESTADO[r.referencia.estado] || r.referencia.estado)}</small>` : "";
  const msg = r.mensaje && !r.mensaje.startsWith("Separado por") ? `<small>${esc(r.mensaje)}</small>` : "";
  return `<div class="situacion">${head}${tags}${ref}${msg}</div>`;
}

function renderTrayTable() {
  syncTrayFilterUi();
  const rows = trayFiltered();
  const pages = Math.max(1, Math.ceil(rows.length / tray.perPage));
  tray.page = Math.min(Math.max(tray.page, 1), pages);
  const start = (tray.page - 1) * tray.perPage;
  const page = rows.slice(start, start + tray.perPage);
  const st = $("tray-state");
  if (!rows.length) {
    $("tray-rows").innerHTML = "";
    st.hidden = false;
    st.innerHTML = tray.rows.length
      ? `<svg class="state-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5M8 11h6"/></svg>
         <h3>Nada coincide con este filtro</h3><p>Prueba con otro filtro o búsqueda.</p>
         <button class="btn btn-secondary" type="button" data-tray-action="clear">Ver todos</button>`
      : `<svg class="state-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 13h4l2 3h4l2-3h4M5 13 7 5h10l2 8v6H5z"/></svg>
         <h3>La bandeja está vacía</h3>
         <p>Sube los TXT pendientes, o suelta la carpeta completa en cualquier parte. Se revisan y esperan aquí hasta que decidas enviarlos.</p>
         <button class="btn btn-primary" type="button" data-tray-action="upload">Subir TXT</button>`;
  } else {
    st.hidden = true;
    $("tray-rows").innerHTML = page.map((r) => {
      const sel = tray.selected.has(r.clave);
      const emision = r.fecha_nueva
        ? `<span class="date-old">${fmtDate(r.fecha_emision)}</span>${fmtDate(r.fecha_nueva)}<span class="plazo-sub">fecha nueva</span>`
        : fmtDate(r.fecha_emision);
      return `<tr class="${sel ? "is-selected" : ""}">
        <td class="c-check"><input class="row-check" type="checkbox" data-tray="${esc(r.clave)}" ${sel ? "checked" : ""} aria-label="Seleccionar ${esc(r.serie_numero)}"></td>
        <td class="c-doc"><span class="doc-id">${esc(r.serie_numero)}</span><span class="doc-type">${esc(tipoLabel(r.tipo))}</span></td>
        <td class="tnum" data-label="Emisión">${emision}</td>
        <td data-label="Plazo">${plazoHtml(r)}</td>
        <td class="num" data-label="Total">${fmtMoney(r.importe_total, r.moneda)}</td>
        <td class="c-reply">${situacionHtml(r)}</td>
      </tr>`;
    }).join("");
  }
  $("tray-range").textContent = rows.length
    ? `${(start + 1).toLocaleString("es-PE")}–${(start + page.length).toLocaleString("es-PE")} de ${rows.length.toLocaleString("es-PE")}` : "Sin resultados";
  $("tray-prev").disabled = tray.page <= 1;
  $("tray-next").disabled = tray.page >= pages;
  syncTraySelection();
}

function syncTraySelection() {
  const n = tray.selected.size;
  $("tray-bulk").hidden = n === 0;
  if (n) {
    const sel = tray.rows.filter((r) => tray.selected.has(r.clave));
    const f = sel.map((r) => r.fecha_efectiva).sort();
    $("tray-bulk-count").textContent = `${fmtCount(n, "seleccionado", "seleccionados")}`
      + (f.length ? `, emitidos del ${fmtDate(f[0])} al ${fmtDate(f[f.length - 1])}` : "");
  }
  const visible = trayFiltered();
  const marked = visible.filter((r) => tray.selected.has(r.clave)).length;
  const all = $("tray-all");
  all.checked = visible.length > 0 && marked === visible.length;
  all.indeterminate = marked > 0 && marked < visible.length;
}

// --- acciones de la bandeja --------------------------------------------------
async function trayPost(action, body) {
  const who = trayWho();
  if (!who) return null;
  return api(`/api/bandeja/${action}`, { method: "POST", body: { ...body, responsable: who } });
}

function omitidosResumen(omitidos) {
  const tipos = [["Fuera de plazo", "fuera de plazo"], ["Separado", "separados"], ["no pasó por el puente", "sin su comprobante en el puente"],
                 ["Ya no está", "ya no estaban en la bandeja"], ["no está dentro del plazo", "con fecha fuera del plazo"],
                 ["anterior a la original", "con fecha anterior a la original"]];
  const out = {};
  omitidos.forEach((o) => {
    const t = tipos.find(([k]) => o.motivo.includes(k));
    const key = t ? t[1] : o.motivo;
    out[key] = (out[key] || 0) + 1;
  });
  return Object.entries(out).map(([k, n]) => `${n} ${k}`).join(", ");
}

async function afterTrayAction(r, okMsg) {
  if (!r) return;
  const omit = r.omitidos.length ? `. Sin cambios: ${omitidosResumen(r.omitidos)}` : "";
  toast(`${okMsg(r.hechos)}${omit}`, r.omitidos.length ? "warn" : "ok");
  tray.selected.clear();
  await Promise.all([loadTray({ silent: true }), loadSummary()]);
}

async function releaseRows(rows, { title, btn } = {}) {
  if (!rows.length) return;
  if (!trayWho()) return;
  const sep = rows.filter((r) => r.grupo === "separado");
  const fuera = rows.filter((r) => r.grupo === "fuera_plazo");
  const sinRef = rows.filter((r) => r.grupo === "listo" && r.referencia && r.referencia.estado === "DESCONOCIDA");
  const esperan = rows.filter((r) => r.referencia && !["APROBADO", "DESCONOCIDA"].includes(r.referencia.estado)).length;
  const s = state.summary;
  const boletas = rows.filter((r) => r.familia === "boleta" && r.grupo !== "fuera_plazo" && r.grupo !== "separado").length;
  const fechasRes = new Set(rows.filter((r) => r.familia === "boleta").map((r) => r.fecha_efectiva)).size;
  const notas = [
    boletas && tray.porResumen
      ? `<p>${fmtCount(boletas, "boleta o nota de boleta va", "boletas y notas de boleta van")} a SUNAT en <strong>resúmenes diarios</strong>, uno por fecha de emisión (${fmtCount(fechasRes, "fecha", "fechas")}), y salen de inmediato${s ? ` (${esc(s.ambiente)})` : ""}.</p>`
      : `<p>Pasan a la cola de envío de la más antigua a la más reciente y el servicio los envía a Sovos${s ? ` (${esc(s.ambiente)})` : ""}.</p>`,
    boletas && tray.porResumen && rows.length - boletas - fuera.length - sep.length > 0 ? `<p>Las facturas pasan a la cola de envío, de la más antigua a la más reciente.</p>` : "",
    esperan ? `<p>${fmtCount(esperan, "nota esperará", "notas esperarán")} a que su comprobante esté aprobado y saldrán solas.</p>` : "",
    fuera.length ? `<p>${fmtCount(fuera.length, "está fuera de plazo y no se incluye", "están fuera de plazo y no se incluyen")}: primero asígnales una fecha nueva.</p>` : "",
  ].join("");
  const check = sep.length
    ? { text: `Enviar también ${fmtCount(sep.length, "separado", "separados")} (inafecto, exonerado u otros)` }
    : sinRef.length ? { text: `Incluir ${fmtCount(sinRef.length, "nota", "notas")} cuyo comprobante no pasó por el puente (emitido con el sistema anterior)` } : null;
  const answer = await confirmModal({
    title: title || `¿Enviar ${fmtCount(rows.length - fuera.length, "comprobante", "comprobantes")}?`,
    html: notas, ok: "Enviar", check,
  });
  if (!answer) return;
  let incluirSinRef = !sep.length && answer.checked;
  const incluirSep = sep.length > 0 && answer.checked;
  if (sep.length && sinRef.length) {
    const second = await confirmModal({
      title: "Notas sin su comprobante en el puente",
      html: `<p>${fmtCount(sinRef.length, "nota modifica", "notas modifican")} comprobantes que no se subieron aquí. Si se emitieron con el sistema anterior, SUNAT los tiene y la nota pasará; si no, la rechazará.</p>`,
      ok: "Incluirlas", check: null,
    });
    incluirSinRef = !!second;
  }
  busy(btn, true);
  try {
    const r = await trayPost("liberar", { claves: rows.map((x) => x.clave), incluir_separados: incluirSep, incluir_sin_referencia: incluirSinRef });
    if (r && r.error) {
      await confirmModal({ title: "No se pudo enviar el resumen diario", ok: "Entendido",
        html: `<p>${esc(r.error)}</p><p>Las boletas siguen en la bandeja; no se envió nada de ellas.</p>` });
    }
    const res = (r && r.resumenes) || [];
    await afterTrayAction(r, (h) => res.length
      ? `${fmtCount(res.length, "resumen diario enviado", "resúmenes diarios enviados")} (${res.map((x) => `${x.id}: ${x.comprobantes} del ${fmtDate(x.fecha)}`).join("; ")})`
        + (h.length > res.reduce((n, x) => n + x.comprobantes, 0) ? `, más ${h.length - res.reduce((n, x) => n + x.comprobantes, 0)} a la cola` : "")
      : `${fmtCount(h.length, "comprobante liberado", "comprobantes liberados")}`);
  } catch (err) { toast(err.message, "bad"); }
  finally { busy(btn, false); }
}

function openDateModal(rows) {
  rows = rows.filter((r) => r.grupo !== "esperando");
  if (!rows.length) { toast("Elige comprobantes que estén en la bandeja", "warn"); return; }
  if (!trayWho()) return;
  const hasFac = rows.some((r) => r.familia === "factura");
  const mixed = hasFac && rows.some((r) => r.familia === "boleta");
  const fechas = tray.fechas[hasFac ? "factura" : "boleta"] || [];
  const plazo = tray.plazos[hasFac ? "factura" : "boleta"];
  const conNueva = rows.some((r) => r.fecha_nueva);
  $("date-text").textContent = `${fmtCount(rows.length, "comprobante", "comprobantes")}. Solo se muestran las fechas que SUNAT acepta hoy`
    + `${mixed ? " para boletas y facturas a la vez" : ""}. La fecha original queda en el historial y el TXT original no se modifica.`;
  $("date-options").innerHTML = `<legend class="sr-only">Fechas que SUNAT acepta hoy</legend>` + fechas.map((f, i) => `
    <label class="date-opt"><input type="radio" name="fecha" value="${f}" ${i === 0 ? "checked" : ""}>
      <strong>${dayLabel(f)}${i === 0 ? ", recomendada" : ""}</strong>
      <span>${i === fechas.length - 1 ? "Último día posible: debe salir hoy mismo" : `SUNAT la acepta hasta el ${fmtDate(addDays(f, plazo))}`}</span></label>`).join("")
    + (conNueva ? `<label class="date-opt"><input type="radio" name="fecha" value="">
      <strong>Volver a la fecha original</strong><span>Quita la fecha nueva asignada</span></label>` : "");
  const warn = () => {
    const f = document.querySelector("#date-options input:checked")?.value ?? null;
    $("date-ok").disabled = f === null;
    const msgs = [];
    if (f) {
      const mes = rows.filter((r) => r.fecha_emision.slice(0, 7) !== f.slice(0, 7)).length;
      if (mes) msgs.push(`${fmtCount(mes, "comprobante cambia", "comprobantes cambian")} de mes, y con eso de periodo tributario. Confírmalo con contabilidad.`);
      const despues = rows.filter((r) => r.fecha_emision > f).length;
      if (despues) msgs.push(`${fmtCount(despues, "es posterior", "son posteriores")} a esta fecha y no se moverá${despues === 1 ? "" : "n"}.`);
    }
    $("date-warn").hidden = !msgs.length;
    $("date-warn").textContent = msgs.join(" ");
  };
  $("date-options").onchange = warn;
  warn();
  const layer = $("date-modal");
  layer.hidden = false;
  requestAnimationFrame(() => layer.classList.add("is-open"));
  $("date-options").querySelector("input")?.focus();
  const close = () => { layer.classList.remove("is-open"); setTimeout(() => { layer.hidden = true; }, 160); };
  $("date-cancel").onclick = close;
  layer.onclick = (e) => { if (e.target === layer) close(); };
  layer.onkeydown = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  $("date-ok").onclick = async () => {
    const fecha = document.querySelector("#date-options input:checked")?.value ?? "";
    busy($("date-ok"), true);
    try {
      const r = await trayPost("fecha", { claves: rows.map((x) => x.clave), fecha });
      if (!r) return;
      close();
      const hechos = new Set(r.hechos);
      await afterTrayAction(r, (h) => fecha
        ? `Fecha ${fmtDate(fecha)} asignada a ${fmtCount(h.length, "comprobante", "comprobantes")}`
        : `${fmtCount(h.length, "comprobante volvió", "comprobantes volvieron")} a su fecha original`);
      const listos = tray.rows.filter((x) => hechos.has(x.clave) && x.grupo === "listo");
      if (fecha && listos.length) await releaseRows(listos, { title: `¿Enviar ahora ${fmtCount(listos.length, "el comprobante", "los comprobantes")} con fecha nueva?` });
    } catch (err) { toast(err.message, "bad"); }
    finally { busy($("date-ok"), false); }
  };
}

async function trayAction(act, btn) {
  const R = tray.rows;
  if (act === "reload") return loadTray();
  if (act === "upload") return openUpload();
  if (act === "clear") { clearTrayFilters(); return; }
  if (act.startsWith("chip:")) { clearTrayFilters(false); tray.chip = act.slice(5); return renderTray(); }
  if (act === "sinref") { clearTrayFilters(false); tray.chip = "listo"; tray.tipo = "nota"; return renderTray(); }
  if (act === "date-fuera") return openDateModal(R.filter((r) => r.grupo === "fuera_plazo"));
  if (act === "date-auto") return autoDates(R.filter((r) => r.grupo === "fuera_plazo"), btn);
  if (act === "send-hoy") return releaseRows(R.filter((r) => r.grupo === "listo" && r.dias_restantes === 0), { title: "¿Enviar los que vencen hoy?", btn });
  if (act.startsWith("send-")) {
    const def = STEP_DEFS.find((s) => `send-${s.key}` === act);
    const rows = R.filter((r) => def.match(r) && r.grupo === "listo");
    return releaseRows(rows, { title: `¿Enviar ${fmtCount(rows.length, def.one, def.many)} a Sovos?`, btn });
  }
  if (act === "bring-back") return bringBackModal();
}

async function bulkAction(act, btn) {
  const rows = tray.rows.filter((r) => tray.selected.has(r.clave));
  if (act === "limpiar") { tray.selected.clear(); return renderTrayTable(); }
  if (!rows.length) return;
  if (act === "liberar") return releaseRows(rows, { btn });
  if (act === "fecha") return openDateModal(rows);
  if (act === "fecha-auto") return autoDates(rows, btn);
  if (act === "descartar") {
    if (!trayWho()) return;
    const ok = await confirmModal({
      title: `¿Descartar ${fmtCount(rows.length, "comprobante", "comprobantes")} de la bandeja?`,
      html: "<p>No se envían. El TXT se guarda en la carpeta de descartados y puedes volver a subirlo cuando quieras.</p>",
      ok: "Descartar", danger: true,
    });
    if (!ok) return;
  }
  busy(btn, true);
  try {
    const claves = rows.map((r) => r.clave);
    const r = act === "descartar" ? await trayPost("descartar", { claves })
      : await trayPost("separar", { claves, separar: act === "separar" });
    await afterTrayAction(r, (h) => ({
      descartar: `${fmtCount(h.length, "descartado", "descartados")} de la bandeja`,
      separar: `${fmtCount(h.length, "separado", "separados")}: no se envían hasta que lo indiques`,
      quitar: `${fmtCount(h.length, "volvió", "volvieron")} a la bandeja normal`,
    })[act]);
  } catch (err) { toast(err.message, "bad"); }
  finally { busy(btn, false); }
}

function clearTrayFilters(render = true) {
  Object.assign(tray, { chip: "todos", tipo: "", q: "", desde: "", hasta: "", page: 1 });
  $("tray-q").value = "";
  if (render) renderTray();
}

function bindTray() {
  $("tray-desde").addEventListener("change", (e) => { tray.desde = e.target.value; tray.page = 1; renderTrayTable(); renderTrayChips(); });
  $("tray-hasta").addEventListener("change", (e) => { tray.hasta = e.target.value; tray.page = 1; renderTrayTable(); renderTrayChips(); });
  $("tray-clear").addEventListener("click", () => clearTrayFilters());
  $("tray-per-page").addEventListener("change", (e) => { tray.perPage = Number(e.target.value); tray.page = 1; renderTrayTable(); });
  document.querySelectorAll("[data-tray-sort]").forEach((b) => b.addEventListener("click", () => {
    const col = b.dataset.traySort;
    tray.dir = tray.sort === col && tray.dir === "asc" ? "desc" : "asc";
    tray.sort = col; tray.page = 1;
    renderTrayTable();
  }));
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));
  $("tray-who").addEventListener("input", () => $("tray-who-field").classList.remove("is-invalid"));
  $("tray-q").addEventListener("input", debounce((e) => { tray.q = e.target.value.trim(); tray.page = 1; renderTrayTable(); }, 200));
  $("tray-tipo").addEventListener("change", (e) => { tray.tipo = e.target.value; tray.page = 1; renderTrayTable(); });
  $("tray-chips").addEventListener("click", (e) => {
    const c = e.target.closest("[data-chip]");
    if (!c) return;
    tray.chip = c.dataset.chip; tray.page = 1;
    renderTrayChips(); renderTrayTable();
  });
  $("tray-prev").addEventListener("click", () => { tray.page -= 1; renderTrayTable(); });
  $("tray-next").addEventListener("click", () => { tray.page += 1; renderTrayTable(); });
  $("tray-rows").addEventListener("change", (e) => {
    const c = e.target.closest("[data-tray]");
    if (!c) return;
    if (c.checked) tray.selected.add(c.dataset.tray); else tray.selected.delete(c.dataset.tray);
    c.closest("tr").classList.toggle("is-selected", c.checked);
    syncTraySelection();
  });
  $("tray-all").addEventListener("change", (e) => {
    trayFiltered().forEach((r) => { if (e.target.checked) tray.selected.add(r.clave); else tray.selected.delete(r.clave); });
    renderTrayTable();
  });
  $("tray-first-btn").addEventListener("click", () => {
    const n = parseInt($("tray-first").value, 10);
    if (!(n > 0)) { toast("Escribe cuántos quieres seleccionar", "warn"); $("tray-first").focus(); return; }
    const pick = trayFiltered().slice(0, n);
    tray.selected = new Set(pick.map((r) => r.clave));
    renderTrayTable();
    if (pick.length < n) toast(`Solo hay ${pick.length.toLocaleString("es-PE")} en este filtro`, "warn");
  });
  $("tray-first").addEventListener("keydown", (e) => { if (e.key === "Enter") $("tray-first-btn").click(); });
  $("view-tray").addEventListener("click", (e) => {
    const a = e.target.closest("[data-tray-action]");
    if (a) { trayAction(a.dataset.trayAction, a); return; }
    const b = e.target.closest("[data-bulk]");
    if (b) bulkAction(b.dataset.bulk, b);
  });
}

// --- configuración ----------------------------------------------------------
// Certificado, conexión con Sovos, carpeta de TXT y clave del panel. El servidor
// valida todo antes de guardar y lo aplica sin reiniciar.
const cfg = { estado: null, certFile: null };
const CFG_NOMBRE = { certificado: "el certificado digital", sovos: "la conexión con Sovos", carpeta: "la carpeta de TXT" };

function configWho() {
  const v = $("config-who").value.trim().replace(/\s+/g, " ");
  $("config-who-field").classList.toggle("is-invalid", !v);
  if (!v) { $("config-who").focus(); toast("Escribe quién hace el cambio: queda en el registro", "warn"); return null; }
  rememberWho(v);
  return v;
}

function renderConfigBanner(pend) {
  const has = pend.length > 0;
  $("config-banner").hidden = !has || currentView() === "config";
  $("tab-config-dot").hidden = !has;
  if (has) {
    const items = pend.map((p) => CFG_NOMBRE[p.clave] || p.texto);
    $("config-banner-text").textContent = `Falta ${items.length > 1 ? `${items.slice(0, -1).join(", ")} y ${items[items.length - 1]}` : items[0]}.`;
  }
}

async function loadConfig() {
  try {
    cfg.estado = await api("/api/config");
    renderConfig();
  } catch (err) { toast(err.message, "bad"); }
}

function renderConfig() {
  const d = cfg.estado;
  if (!d) return;
  const falta = new Set(d.pendientes.map((p) => p.clave));
  const c = d.certificado;
  $("card-cert").dataset.state = falta.has("certificado") ? "falta" : "ok";
  $("cert-badge").innerHTML = falta.has("certificado") ? pill("Falta", "warn")
    : c.dias <= 30 ? pill(`Vence en ${c.dias} días`, "warn") : pill("Vigente", "ok");
  $("cert-facts").innerHTML = (c.serie ? `
      <dt>Titular</dt><dd>${esc(c.titular)}</dd>
      <dt>Serie</dt><dd>${esc(c.serie)}</dd>
      <dt>Vigencia</dt><dd>${fmtDate(c.vigente_desde)} al ${fmtDate(c.vence)}, quedan ${c.dias.toLocaleString("es-PE")} días</dd>` : "")
    + (c.problema ? `<dt>Problema</dt><dd>${esc(c.problema)}</dd>` : "");
  $("cert-save").textContent = c.serie ? "Reemplazar certificado" : "Validar y guardar";

  const sv = d.sovos;
  $("card-sovos").dataset.state = falta.has("sovos") ? "falta" : "ok";
  $("sovos-badge").innerHTML = falta.has("sovos") ? pill("Falta", "warn")
    : pill(sv.ambiente === "pruebas" ? "Pruebas" : "Producción", sv.ambiente === "pruebas" ? "info" : "ok");
  if (sv.ambiente === "produccion" || sv.ambiente === "pruebas") $("sovos-amb").value = sv.ambiente;
  if (!$("sovos-id").value) $("sovos-id").value = sv.client_id || "";
  $("sovos-secret").placeholder = sv.secret_guardado ? "Guardado. Escríbelo solo si cambió" : "";

  const dir = d.carpeta;
  $("card-dir").dataset.state = falta.has("carpeta") ? "falta" : "ok";
  $("dir-badge").innerHTML = falta.has("carpeta") ? pill("Falta", "warn")
    : pill(dir.txt ? `${dir.txt.toLocaleString("es-PE")} TXT por tomar` : "Vigilando", "ok");
  if (!$("dir-path").value) $("dir-path").value = dir.ruta || "";

  $("access-user").textContent = d.panel.usuario;
  $("access-urls").innerHTML = d.panel.urls.map((u) => `<li>
      <a href="${esc(u.url)}" target="_blank" rel="noopener">${esc(u.url)}</a>
      <small>${esc(u.donde)}</small>
      <button class="btn btn-ghost btn-sm" type="button" data-copy="${esc(u.url)}">Copiar</button></li>`).join("");
  renderConfigBanner(d.pendientes);
}

function formError(id, msg) {
  const el = $(id);
  el.hidden = !msg;
  el.textContent = msg || "";
}

function setCertFile(f) {
  if (!f) return;
  if (!/\.(pfx|p12)$/i.test(f.name)) { formError("cert-error", "El certificado debe ser un archivo .pfx o .p12"); return; }
  cfg.certFile = f;
  formError("cert-error", "");
  $("cert-file-name").textContent = `${f.name} (${Math.max(1, Math.round(f.size / 1024))} KB)`;
  $("cert-pick").classList.add("has-file");
  $("cert-pass").focus();
}

async function configPost(action, body, btn, errId) {
  const who = configWho();
  if (!who) return null;
  formError(errId, "");
  busy(btn, true);
  try {
    const r = await api(`/api/config/${action}`, { method: "POST", body: { ...body, responsable: who } });
    if (r.estado) { cfg.estado = r.estado; renderConfig(); }
    loadSummary();
    return r;
  } catch (err) {
    formError(errId, err.message);
    return null;
  } finally { busy(btn, false); }
}

async function saveCert(e) {
  e.preventDefault();
  if (!cfg.certFile) { formError("cert-error", "Elige el archivo del certificado (.pfx o .p12)"); return; }
  const clave = $("cert-pass").value;
  if (!clave) { formError("cert-error", "Escribe la contraseña del certificado"); $("cert-pass").focus(); return; }
  const contenido = await readAsBase64(cfg.certFile);
  const r = await configPost("certificado", { contenido, nombre: cfg.certFile.name, clave }, $("cert-save"), "cert-error");
  if (!r) return;
  $("cert-pass").value = "";
  cfg.certFile = null;
  $("cert-file").value = "";
  $("cert-pick").classList.remove("has-file");
  $("cert-file-name").textContent = "Elige o suelta el archivo .pfx o .p12";
  toast(`Certificado de ${r.titular} cargado; vence el ${fmtDate(r.vence)}${r.avisos.length ? `. ${r.avisos.join(". ")}` : ""}`, r.avisos.length ? "warn" : "ok");
}

async function saveSovos(e) {
  e.preventDefault();
  const id = $("sovos-id").value.trim();
  const secret = $("sovos-secret").value.trim();
  if (!id) { formError("sovos-error", "Escribe el Client ID"); $("sovos-id").focus(); return; }
  if (!secret && !cfg.estado?.sovos.secret_guardado) { formError("sovos-error", "Escribe el Client Secret"); $("sovos-secret").focus(); return; }
  const amb = $("sovos-amb").value;
  const r = await configPost("sovos", { ambiente: amb, client_id: id, client_secret: secret }, $("sovos-save"), "sovos-error");
  if (!r) return;
  $("sovos-secret").value = "";
  toast(`Sovos aceptó las credenciales; conexión de ${amb === "pruebas" ? "pruebas" : "producción"} guardada`);
}

async function saveDir(e) {
  e.preventDefault();
  const ruta = $("dir-path").value.trim();
  if (!ruta) { formError("dir-error", "Escribe la ruta de la carpeta"); $("dir-path").focus(); return; }
  let r = await configPost("carpeta", { ruta }, $("dir-save"), "dir-error");
  if (!r) return;
  if (r.decision) {
    const answer = await confirmModal({
      title: `La carpeta ya tiene ${fmtCount(r.txt, "TXT", "TXT")}`,
      html: `<p>Si el servicio empieza a vigilarla así, los enviaría todos de inmediato, sin revisar fechas vencidas ni separar inafectos y exonerados.</p>
             <p><strong>Recomendado:</strong> pasarlos a la bandeja para decidir ahí qué se envía, en qué orden y con qué fecha. Los originales se guardan aparte.</p>`,
      ok: "Continuar",
      check: { text: "Prefiero que se envíen tal cual, sin pasar por la bandeja" },
    });
    if (!answer) return;
    r = await configPost("carpeta", { ruta, existentes: answer.checked ? "dejar" : "bandeja" }, $("dir-save"), "dir-error");
    if (!r) return;
  }
  const imp = r.importados;
  if (imp) {
    toast(`${fmtCount(imp.en_bandeja, "TXT pasó", "TXT pasaron")} a la bandeja${imp.separados ? ` (${imp.separados} separados)` : ""}`
      + `${imp.no_subidos ? `, ${imp.no_subidos} no se pudieron subir: ver la constancia` : ""}`, imp.no_subidos ? "warn" : "ok");
    showView("tray");
  } else {
    toast("Carpeta guardada: el servicio ya la está vigilando");
  }
}

async function savePass(e) {
  e.preventDefault();
  const nueva = $("pass-new").value;
  if (nueva.length < 8) { formError("pass-error", "La clave debe tener al menos 8 caracteres"); $("pass-new").focus(); return; }
  const r = await configPost("clave", { nueva }, $("pass-save"), "pass-error");
  if (!r) return;
  $("pass-new").value = "";
  toast("Clave cambiada. El navegador te la pedirá de nuevo");
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const t = document.createElement("textarea");
    t.value = text; document.body.appendChild(t); t.select();
    document.execCommand("copy"); t.remove();
  }
  toast(`Copiado: ${text}`);
}

function bindConfig() {
  $("config-who").addEventListener("input", () => $("config-who-field").classList.remove("is-invalid"));
  $("cert-file").addEventListener("change", (e) => setCertFile(e.target.files[0]));
  $("cert-form").addEventListener("submit", saveCert);
  $("sovos-form").addEventListener("submit", saveSovos);
  $("dir-form").addEventListener("submit", saveDir);
  $("pass-form").addEventListener("submit", savePass);
  $("access-urls").addEventListener("click", (e) => { const b = e.target.closest("[data-copy]"); if (b) copyText(b.dataset.copy); });
  document.querySelectorAll("[data-view-go]").forEach((b) => b.addEventListener("click", () => showView(b.dataset.viewGo)));
}

// --- eventos ----------------------------------------------------------------
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

function bind() {
  $("q").addEventListener("input", debounce((e) => { state.q = e.target.value.trim(); reload(); }, 250));
  $("tipo").addEventListener("change", (e) => { state.tipo = e.target.value; reload(); });
  $("estado").addEventListener("change", (e) => { state.group = e.target.value; reload(); });
  $("desde").addEventListener("change", (e) => { state.desde = e.target.value; reload(); });
  $("hasta").addEventListener("change", (e) => { state.hasta = e.target.value; reload(); });
  $("per-page").addEventListener("change", (e) => { state.perPage = Number(e.target.value); reload(); });
  $("prev").addEventListener("click", () => { state.page -= 1; reload({ resetPage: false }); });
  $("next").addEventListener("click", () => { state.page += 1; reload({ resetPage: false }); });
  $("clear").addEventListener("click", clearFilters);
  $("retry-all").addEventListener("click", (e) => retryAll(e.currentTarget));
  $("drawer-close").addEventListener("click", closeDrawer);
  $("scrim").addEventListener("click", closeDrawer);

  document.querySelectorAll("#view-docs .sort").forEach((b) => b.addEventListener("click", () => {
    const col = b.dataset.sort;
    state.dir = state.sort === col && state.dir === "desc" ? "asc" : "desc";
    state.sort = col;
    reload();
  }));

  document.querySelector(".flow").addEventListener("click", (e) => {
    const b = e.target.closest("[data-group]");
    if (!b || b.disabled) return;
    state.group = state.group === b.dataset.group ? "" : b.dataset.group;
    reload();
  });

  document.addEventListener("click", (e) => {
    const retry = e.target.closest("[data-retry]");
    if (retry) { e.stopPropagation(); retryDoc(retry.dataset.retry, retry); return; }
    const consult = e.target.closest("[data-consult]");
    if (consult) { consultDoc(consult.dataset.consult, consult); return; }
    const action = e.target.closest("[data-action]");
    if (action?.dataset.action === "clear") { clearFilters(); return; }
    if (action?.dataset.action === "reload") { reload({ resetPage: false }); return; }
    const row = e.target.closest("tr[data-clave]");
    if (row && !e.target.closest("a, button")) showDetail(row.dataset.clave);
  });
  $("rows").addEventListener("keydown", (e) => {
    const row = e.target.closest("tr[data-clave]");
    if (row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); showDetail(row.dataset.clave); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.openClave && $("modal").hidden && $("upload").hidden) closeDrawer();
    if (e.key === "/" && document.activeElement.tagName !== "INPUT") { e.preventDefault(); $("q").focus(); }
  });
}

function clearFilters() {
  state.q = state.tipo = state.desde = state.hasta = state.group = "";
  $("q").value = ""; $("tipo").value = ""; $("desde").value = ""; $("hasta").value = "";
  reload();
}

// --- arranque ---------------------------------------------------------------
function refreshLoop() {
  setInterval(() => {
    if (document.hidden || !$("modal").hidden || !$("upload").hidden || !$("date-modal").hidden) return;
    loadSummary();
    if (currentView() === "tray") { loadTray({ silent: true }); return; }
    if (currentView() === "config") return;
    loadRows({ silent: true });
    if (state.openClave) showDetail(state.openClave);
  }, REFRESH_MS);
  setInterval(tickRefreshed, 5000);
}

bind();
bindUpload();
bindTray();
bindConfig();
syncSortHeaders();
loadSummary();
(() => {
  let view = "docs";
  try { view = localStorage.getItem(VIEW_KEY) || "docs"; } catch { /* sin almacenamiento */ }
  if (view === "tray") showView("tray"); else loadRows();
})();
refreshLoop();


// --- fechas automáticas ------------------------------------------------------
// El puente propone la fecha de cada comprobante fuera de plazo: la más reciente que SUNAT
// acepta sin desordenar la numeración de su serie. El usuario solo revisa y confirma.
async function autoDates(rows, btn) {
  rows = rows.filter((r) => r.grupo !== "esperando");
  if (!rows.length) { toast("No hay comprobantes fuera de plazo para cambiarles la fecha", "warn"); return; }
  if (!trayWho()) return;
  const claves = rows.map((r) => r.clave);
  let plan;
  busy(btn, true);
  try {
    const r = await trayPost("fecha-auto", { claves, simular: true });
    if (!r) return;
    plan = r.plan;
  } catch (err) { toast(err.message, "bad"); return; }
  finally { busy(btn, false); }

  const ok = plan.filter((p) => !p.omitido);
  const omit = plan.filter((p) => p.omitido);
  if (!ok.length) { toast(omit[0]?.omitido || "No hay nada que cambiar", "warn"); return; }

  // Una fila por serie y fecha nueva: "BT05 · 00126088 a 00126111 · emitidas 10/11–13/11 → 27/11"
  const grupos = new Map();
  ok.forEach((p) => {
    const [serie, num] = p.serie_numero.split("-");
    const k = `${serie}|${p.fecha_nueva}`;
    const g = grupos.get(k) || { serie, fecha: p.fecha_nueva, n: 0, nums: [], orig: [], aviso: false, mes: false };
    g.n += 1; g.nums.push(num); g.orig.push(p.fecha_original);
    g.aviso ||= !!p.aviso; g.mes ||= p.cambia_mes;
    grupos.set(k, g);
  });
  const filas = [...grupos.values()].sort((a, b) => a.serie.localeCompare(b.serie) || a.fecha.localeCompare(b.fecha)).map((g) => {
    g.nums.sort(); g.orig.sort();
    const nums = g.n === 1 ? g.nums[0] : `${g.nums[0]} a ${g.nums[g.nums.length - 1]}`;
    const orig = g.orig[0] === g.orig[g.orig.length - 1] ? fmtDate(g.orig[0]) : `${fmtDate(g.orig[0])} – ${fmtDate(g.orig[g.orig.length - 1])}`;
    return `<tr${g.aviso ? ' data-tone="warn"' : ""}>
      <td><strong>${esc(g.serie)}</strong></td><td class="tnum">${esc(nums)}<span class="plan-n">${fmtCount(g.n, "comprobante", "comprobantes")}</span></td>
      <td class="tnum">${orig}</td><td class="tnum plan-new">${fmtDate(g.fecha)}</td></tr>`;
  }).join("");
  const mes = ok.filter((p) => p.cambia_mes).length;
  const avisos = ok.filter((p) => p.aviso).length;
  const notas = [
    mes ? `<p class="date-warn">${fmtCount(mes, "comprobante cambia", "comprobantes cambian")} de mes, y con eso de periodo tributario. Confírmalo con contabilidad.</p>` : "",
    avisos ? `<p class="date-warn">${fmtCount(avisos, "comprobante no puede", "comprobantes no pueden")} conservar el orden de numeración: un número mayor de su serie ya salió con una fecha que SUNAT ya no acepta. Se les pone la fecha de hoy.</p>` : "",
    omit.length ? `<p class="hint">${fmtCount(omit.length, "seleccionado no cambia", "seleccionados no cambian")}: ${esc(omitidosResumen(omit.map((o) => ({ clave: o.clave, motivo: o.omitido }))))}.</p>` : "",
  ].join("");
  const answer = await confirmModal({
    title: `Fecha nueva para ${fmtCount(ok.length, "comprobante", "comprobantes")}`,
    html: `<p>Cada comprobante recibe la fecha más reciente que SUNAT acepta hoy, sin que un número menor quede con una fecha posterior a la de un número mayor de su serie.</p>
      <div class="plan-wrap"><table class="plan-table"><thead><tr><th>Serie</th><th>Números</th><th>Emitidos</th><th>Fecha nueva</th></tr></thead>
      <tbody>${filas}</tbody></table></div>${notas}
      <p class="hint">El TXT original no se modifica y la fecha original queda en el historial.</p>`,
    ok: "Asignar fechas",
    check: { text: "Enviarlos a Sovos en cuanto tengan su fecha" },
  });
  if (!answer) return;
  try {
    const r = await trayPost("fecha-auto", { claves: ok.map((p) => p.clave) });
    if (!r) return;
    await afterTrayAction(r, (h) => `Fecha nueva asignada a ${fmtCount(h.length, "comprobante", "comprobantes")}`);
    if (answer.checked) {
      const hechos = new Set(r.hechos);
      const listos = tray.rows.filter((x) => hechos.has(x.clave) && x.grupo === "listo");
      if (listos.length) await releaseRows(listos, { title: `¿Enviar ahora ${fmtCount(listos.length, "el comprobante", "los comprobantes")} con fecha nueva?` });
    }
  } catch (err) { toast(err.message, "bad"); }
}

// --- traer rechazados por rango de fechas y tipo ---------------------------------
const TIPOS_TRAER = [["03", "Boletas"], ["01", "Facturas"], ["07", "Notas de crédito"], ["08", "Notas de débito"]];

async function bringBackModal() {
  const all = tray.recuperables || [];
  if (!all.length) { toast("No hay rechazados por fecha para corregir", "ok"); return; }
  if (!trayWho()) return;
  const fechas = all.map((x) => x.fecha).filter(Boolean).sort();
  const min = fechas[0] || "", max = fechas[fechas.length - 1] || "";
  const tipos = TIPOS_TRAER.filter(([k]) => all.some((x) => x.tipo === k));
  const pending = confirmModal({
    title: "Traer rechazados a la bandeja",
    html: `<p>Elige los comprobantes que quieres corregir. Vuelven a la bandeja y el puente les propone una fecha nueva.</p>
      <div class="bb-range">
        <label class="field"><span class="field-label">Emitidos desde</span><input type="date" id="bb-desde" value="${min}" min="${min}" max="${max}"></label>
        <label class="field"><span class="field-label">hasta</span><input type="date" id="bb-hasta" value="${max}" min="${min}" max="${max}"></label>
      </div>
      <fieldset class="bb-tipos"><legend class="field-label">Tipo de comprobante (uno o varios)</legend>
        ${tipos.map(([k, label]) => `<label class="bb-tipo"><input type="checkbox" value="${k}" checked><span>${label}</span><span class="bb-n" data-tipo="${k}"></span></label>`).join("")}
      </fieldset>
      <p class="bb-total" id="bb-total" aria-live="polite"></p>`,
    ok: "Traer",
  });
  const leer = () => ({
    desde: $("bb-desde").value, hasta: $("bb-hasta").value,
    tipos: [...document.querySelectorAll(".bb-tipo input:checked")].map((i) => i.value),
  });
  const elegidos = () => {
    const f = leer();
    return all.filter((x) => (!f.desde || x.fecha >= f.desde) && (!f.hasta || x.fecha <= f.hasta) && f.tipos.includes(x.tipo));
  };
  const update = () => {
    const f = leer();
    const enRango = all.filter((x) => (!f.desde || x.fecha >= f.desde) && (!f.hasta || x.fecha <= f.hasta));
    document.querySelectorAll(".bb-n").forEach((s) => { s.textContent = enRango.filter((x) => x.tipo === s.dataset.tipo).length.toLocaleString("es-PE"); });
    const sel = elegidos();
    const malRango = f.desde && f.hasta && f.desde > f.hasta;
    $("bb-total").textContent = malRango ? "La fecha inicial es posterior a la final."
      : sel.length ? `${sel.length === 1 ? "Se traerá" : "Se traerán"} ${fmtCount(sel.length, "comprobante", "comprobantes")}.` : "Ningún comprobante cumple estos filtros.";
    $("bb-total").dataset.tone = malRango || !sel.length ? "warn" : "";
    $("modal-ok").disabled = malRango || !sel.length;
    $("modal-ok").textContent = sel.length && !malRango ? `Traer ${fmtCount(sel.length, "comprobante", "comprobantes")}` : "Traer";
  };
  document.querySelectorAll("#bb-desde, #bb-hasta, .bb-tipo input").forEach((i) => i.addEventListener("input", update));
  update();
  const answer = await pending;
  if (!answer) return;
  const f = leer();
  try {
    const r = await trayPost("traer-rechazados", f);
    if (!r) return;
    await afterTrayAction(r, (h) => `${fmtCount(h.length, "rechazado volvió", "rechazados volvieron")} a la bandeja`);
    const hechos = new Set(r.hechos);
    const fuera = tray.rows.filter((x) => hechos.has(x.clave) && x.grupo === "fuera_plazo");
    if (fuera.length) await autoDates(fuera);
  } catch (err) { toast(err.message, "bad"); }
}
