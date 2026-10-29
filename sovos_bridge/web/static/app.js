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
};
const SOVOS_CODES = { AP: "AP, aprobado", CA: "CA, aprobado con observaciones", RE: "RE, rechazado", IP: "IP, en proceso", PD: "PD, pendiente" };
const TIPOS = { "01": "Factura", "03": "Boleta", "07": "Nota de crédito", "08": "Nota de débito" };

// Etapas del recorrido (secuencia real) y grupos que requieren atención.
const STAGES = [
  { key: "bandeja", name: "En bandeja", hint: "TXT por procesar", estados: null, tone: "idle" },
  { key: "recibido", name: "Recibidos", hint: "Validando y armando", estados: ["RECIBIDO"], tone: "idle" },
  { key: "sovos", name: "En Sovos", hint: "Esperando a SUNAT", estados: ["ENVIADO", "EN_PROCESO"], tone: "info" },
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
  const code = d.codigo_sunat || (d.estado === "APROBADO" ? "0" : "");
  const msg = cleanMsg(d.mensaje);
  const retry = d.reintento
    ? `<button class="btn btn-secondary btn-sm" type="button" data-retry="${esc(d.clave)}">${retryLabel(d.reintento)}</button>` : "";
  return `
    <tr tabindex="0" data-clave="${esc(d.clave)}">
      <td class="c-doc"><span class="doc-id">${esc(d.serie_numero)}</span><span class="doc-type">${esc(tipoLabel(d.tipo))}</span></td>
      <td class="tnum" data-label="Emisión">${fmtDate(d.fecha_emision)}</td>
      <td class="tnum" data-label="Cliente">${esc(d.cliente_doc || "—")}</td>
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
      <p>Cuando el sistema de facturación deje TXT en la bandeja, aparecerán aquí con su estado en Sovos y SUNAT.</p>`;
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
  document.querySelectorAll("th").forEach((th) => th.removeAttribute("aria-sort"));
  const btn = document.querySelector(`.sort[data-sort="${state.sort}"]`);
  if (btn) btn.closest("th").setAttribute("aria-sort", state.dir === "asc" ? "ascending" : "descending");
}

function syncFilterUi() {
  $("clear").hidden = !hasFilters();
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

function replyCallout(d) {
  if (d.estado === "APROBADO") {
    return `<div class="callout" data-tone="ok"><strong>Aceptado por SUNAT${d.codigo_sunat ? ` (código ${esc(d.codigo_sunat)})` : ""}</strong>${d.mensaje ? `<p>${esc(d.mensaje)}</p>` : ""}</div>`;
  }
  if (!d.mensaje && !d.codigo_sunat) {
    const espera = { RECIBIDO: "El servicio lo está validando y armando para Sovos.", ENVIADO: "Sovos lo recibió; esperando la respuesta de SUNAT.", EN_PROCESO: "Sovos lo está procesando con SUNAT." }[d.estado];
    return espera ? `<div class="callout"><p>${espera}</p></div>` : "";
  }
  const tone = ESTADOS[d.estado]?.tone === "bad" ? "bad" : "warn";
  const ayuda = {
    RECHAZADO: "SUNAT no lo registró: el mismo número puede reenviarse una vez corregida la causa.",
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
        <div><dt>Total</dt><dd>${fmtMoney(d.importe_total, d.moneda)}</dd></div>
        <div><dt>Cliente</dt><dd>${esc(d.cliente_doc || "—")}</dd></div>
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
    toast(r.ok ? (d.reintento === "consulta" ? "Consulta reanudada" : "Devuelto a la bandeja para reenvío") : r.mensaje, r.ok ? "ok" : "warn");
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
const UPLOAD_CHUNK = 200;
const RESULTADOS = {
  error:       { label: "Con error",        tone: "bad",  order: 0 },
  advertencia: { label: "Listo, con aviso", tone: "warn", order: 1 },
  valido:      { label: "Listo",            tone: "ok",   order: 2 },
  ya_enviado:  { label: "Ya enviado",       tone: "idle", order: 3 },
  en_cola:     { label: "Ya en la bandeja", tone: "idle", order: 4 },
};
const upload = { files: [], results: [], done: false, sent: new Set() };

function pill(label, tone) { return `<span class="badge" data-tone="${tone}">${esc(label)}</span>`; }

function openUpload() {
  resetUpload();
  const layer = $("upload");
  layer.hidden = false;
  requestAnimationFrame(() => layer.classList.add("is-open"));
  $("upload-input").focus();
}
function closeUpload() {
  const layer = $("upload");
  layer.classList.remove("is-open");
  setTimeout(() => { layer.hidden = true; }, 160);
  $("open-upload").focus();
}
function resetUpload() {
  upload.files = []; upload.results = []; upload.done = false; upload.sent = new Set();
  $("upload-input").value = "";
  $("dropzone").hidden = false;
  $("dropzone").classList.remove("is-busy");
  $("upload-results").hidden = true;
  $("upload-reset").hidden = true;
  $("upload-send").disabled = true;
  $("upload-send").textContent = "Enviar a la bandeja";
  $("upload-sub").textContent = "Se revisan antes de enviar: nada sale a Sovos hasta que confirmes.";
  serviceNote();
}
function serviceNote() {
  const note = $("upload-note");
  const s = state.summary;
  if (s && !s.servicio.activo) {
    note.dataset.tone = "warn";
    note.textContent = "El servicio de envío está detenido: los archivos esperarán en la bandeja hasta que se inicie.";
  } else {
    note.dataset.tone = "";
    note.textContent = s ? `Al confirmar, el servicio los envía a Sovos (${s.ambiente}).` : "";
  }
}

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",", 2)[1] || "");
    r.onerror = () => reject(new Error(`No se pudo leer ${file.name}`));
    r.readAsDataURL(file);
  });
}

async function postChunks(modo, files) {
  const out = [];
  for (let i = 0; i < files.length; i += UPLOAD_CHUNK) {
    const r = await api("/api/upload", { method: "POST", body: { modo, archivos: files.slice(i, i + UPLOAD_CHUNK) } });
    out.push(...r.archivos);
  }
  return out;
}

async function handleFiles(list) {
  const picked = [...list].filter((f) => /\.txt$/i.test(f.name));
  const skipped = list.length - picked.length;
  if (!picked.length) { toast("Elige archivos .txt del sistema de facturación", "warn"); return; }
  const dz = $("dropzone");
  dz.classList.add("is-busy");
  $("upload-sub").textContent = `Revisando ${picked.length} archivo${picked.length === 1 ? "" : "s"}…`;
  try {
    upload.files = await Promise.all(picked.map(async (f) => ({ nombre: f.name, contenido: await readAsBase64(f) })));
    upload.results = await postChunks("revisar", upload.files);
    dz.hidden = true;
    renderUpload();
    if (skipped) toast(`${skipped} archivo${skipped === 1 ? "" : "s"} sin extensión .txt no se incluyeron`, "warn");
  } catch (err) {
    toast(err.message, "bad");
    resetUpload();
  } finally { dz.classList.remove("is-busy"); }
}

function renderUpload() {
  const res = [...upload.results].sort((a, b) => RESULTADOS[a.resultado].order - RESULTADOS[b.resultado].order);
  upload.results = res;
  const counts = {};
  res.forEach((r) => { counts[r.resultado] = (counts[r.resultado] || 0) + 1; });
  const queued = upload.done ? res.filter((r) => r.encolable && upload.sent.has(r.nombre)).length : 0;
  $("upload-summary").innerHTML = (upload.done ? pill(`Dejados en la bandeja: ${queued}`, "info") : "")
    + Object.keys(RESULTADOS).filter((k) => counts[k])
      .map((k) => pill(`${RESULTADOS[k].label}: ${counts[k]}`, RESULTADOS[k].tone)).join("");
  $("upload-rows").innerHTML = res.map((r, i) => {
    const meta = RESULTADOS[r.resultado];
    const check = r.encolable && !upload.done
      ? `<input class="row-check" type="checkbox" data-upload="${i}" checked aria-label="Incluir ${esc(r.nombre)}">` : "";
    const emision = r.fecha_emision
      ? `${fmtDate(r.fecha_emision)}${r.dias_desde_emision > 0 ? `<span class="file-sub">hace ${r.dias_desde_emision} días</span>` : ""}` : "—";
    return `<tr>
      <td>${check}</td>
      <td><span class="file-name">${esc(r.serie_numero || r.nombre)}</span><span class="file-sub">${esc(r.serie_numero ? `${tipoLabel(r.tipo)}, ${r.nombre}` : "")}</span></td>
      <td class="tnum">${emision}</td>
      <td class="num">${r.importe_total ? fmtMoney(r.importe_total, r.moneda) : "—"}</td>
      <td>${pill(meta.label, meta.tone)}</td>
      <td><ul class="msgs">${(r.mensajes || []).map((m) => `<li>${esc(m)}</li>`).join("") || "<li>Sin observaciones</li>"}</ul></td>
    </tr>`;
  }).join("");
  $("upload-results").hidden = false;
  $("upload-reset").hidden = false;
  $("upload-sub").textContent = upload.done
    ? "Resultado de la carga."
    : "Revisa el resultado. Desmarca los que no quieras enviar todavía.";
  syncUploadButton();
}

function selectedUploads() {
  return [...document.querySelectorAll("[data-upload]:checked")].map((c) => upload.results[Number(c.dataset.upload)]);
}
function syncUploadButton() {
  const btn = $("upload-send");
  if (upload.done) { btn.disabled = false; btn.textContent = "Cerrar"; return; }
  const n = selectedUploads().length;
  btn.disabled = n === 0;
  btn.textContent = n ? `Enviar ${n} a la bandeja` : "Nada para enviar";
}

async function sendUploads(btn) {
  if (upload.done) { closeUpload(); return; }
  const chosen = new Set(selectedUploads().map((r) => r.nombre));
  const files = upload.files.filter((f) => chosen.has(f.nombre));
  if (!files.length) return;
  busy(btn, true);
  try {
    const res = await postChunks("encolar", files);
    const ok = res.filter((r) => r.encolable).length;
    const byName = new Map(res.map((r) => [r.nombre, r]));
    upload.results = upload.results.map((r) => byName.get(r.nombre) || r);
    upload.done = true;
    upload.sent = chosen;
    renderUpload();
    toast(ok === files.length ? `${ok} dejado${ok === 1 ? "" : "s"} en la bandeja` : `${ok} de ${files.length} dejados en la bandeja; revisa el detalle`, ok === files.length ? "ok" : "warn");
    loadSummary(); loadRows({ silent: true });
  } catch (err) {
    toast(err.message, "bad");
  } finally { busy(btn, false); syncUploadButton(); }
}

function bindUpload() {
  const dz = $("dropzone");
  $("open-upload").addEventListener("click", openUpload);
  $("upload-close").addEventListener("click", closeUpload);
  $("upload-reset").addEventListener("click", resetUpload);
  $("upload-send").addEventListener("click", (e) => sendUploads(e.currentTarget));
  $("upload-input").addEventListener("change", (e) => handleFiles(e.target.files));
  $("upload-rows").addEventListener("change", (e) => { if (e.target.matches("[data-upload]")) syncUploadButton(); });
  ["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("is-over"); }));
  ["dragleave", "drop"].forEach((t) => dz.addEventListener(t, () => dz.classList.remove("is-over")));
  dz.addEventListener("drop", (e) => { e.preventDefault(); handleFiles(e.dataTransfer.files); });
  $("upload").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); closeUpload(); } });
}

// --- eventos ----------------------------------------------------------------
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

function bind() {
  $("q").addEventListener("input", debounce((e) => { state.q = e.target.value.trim(); reload(); }, 250));
  $("tipo").addEventListener("change", (e) => { state.tipo = e.target.value; reload(); });
  $("desde").addEventListener("change", (e) => { state.desde = e.target.value; reload(); });
  $("hasta").addEventListener("change", (e) => { state.hasta = e.target.value; reload(); });
  $("per-page").addEventListener("change", (e) => { state.perPage = Number(e.target.value); reload(); });
  $("prev").addEventListener("click", () => { state.page -= 1; reload({ resetPage: false }); });
  $("next").addEventListener("click", () => { state.page += 1; reload({ resetPage: false }); });
  $("clear").addEventListener("click", clearFilters);
  $("retry-all").addEventListener("click", (e) => retryAll(e.currentTarget));
  $("drawer-close").addEventListener("click", closeDrawer);
  $("scrim").addEventListener("click", closeDrawer);

  document.querySelectorAll(".sort").forEach((b) => b.addEventListener("click", () => {
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
    if (document.hidden || !$("modal").hidden || !$("upload").hidden) return;
    loadSummary();
    loadRows({ silent: true });
    if (state.openClave) showDetail(state.openClave);
  }, REFRESH_MS);
  setInterval(tickRefreshed, 5000);
}

bind();
bindUpload();
syncSortHeaders();
loadSummary();
loadRows();
refreshLoop();
