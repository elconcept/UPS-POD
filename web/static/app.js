/* UPS POD UI: API-only, no credentials in the browser. */
"use strict";
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const selected = new Set();
const readySelected = new Set();
const COOKIE_AGE = 365 * 24 * 60 * 60;
function getCookie(name) {
  const value = document.cookie.split("; ").find((part) => part.startsWith(name + "="));
  if (!value) return "";
  try { return decodeURIComponent(value.slice(name.length + 1)); }
  catch { return ""; }
}
function saveCookie(name, value) {
  document.cookie = name + "=" + encodeURIComponent(value) + "; Path=/; Max-Age="
    + COOKIE_AGE + "; SameSite=Strict" + (location.protocol === "https:" ? "; Secure" : "");
}
const savedScope = getCookie("ups_pod_scope");
let scope = ["all", "ready", "missing"].includes(savedScope) ? savedScope : "all";
let offset = 0;
let total = 0;
let rows = [];
const monthParts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Warsaw", year: "numeric", month: "2-digit"
}).formatToParts(new Date()).map((part) => [part.type, part.value]));
const savedMonth = getCookie("ups_pod_month");
let activeMonth = savedMonth === "all" || /^\d{4}-(0[1-9]|1[0-2])$/.test(savedMonth)
  ? savedMonth : monthParts.year + "-" + monthParts.month;
const PAGE_SIZE = 200;
const BASE_COLUMNS = [
  "fba", "tracking", "ship_date", "delivery_date", "weight", "ups_status", "queue", "document"
];
// Allowlisted UPS Tracking API paths; values are extracted server-side without
// returning raw addresses, personal identities, signature, photo or POD data.
const EXTRA_COLUMNS = [
  ["ups.service", "Usługa UPS", "shipment.service.description"],
  ["ups.pickup_date", "Odbiór przez UPS", "shipment.pickupDate"],
  ["ups.package_count", "Liczba paczek", "shipment.packageCount"],
  ["ups.status_detail", "Opis statusu", "package.currentStatus.simplifiedTextDescription"],
  ["ups.status_code", "Kod statusu", "package.currentStatus.code"],
  ["ups.status_type", "Typ zdarzenia", "package.activity[0].status.type"],
  ["ups.event_code", "Kod zdarzenia", "package.activity[0].status.code"],
  ["ups.event_status_code", "Kod skanu", "package.activity[0].status.statusCode"],
  ["ups.activity_description", "Ostatni skan", "package.activity[0].status.description"],
  ["ups.activity_date", "Data skanu", "package.activity[0].date"],
  ["ups.activity_time", "Czas skanu", "package.activity[0].time"],
  ["ups.delivery_date_type", "Typ daty UPS", "package.deliveryDate[0].type"],
  ["ups.delivery_time_type", "Typ czasu UPS", "package.deliveryTime.type"],
  ["ups.delivery_start", "Okno od", "package.deliveryTime.startTime"],
  ["ups.delivery_end", "Okno do", "package.deliveryTime.endTime"],
  ["ups.weight_unit", "Jednostka wagi", "package.weight.unitOfMeasurement"],
];
const COLUMN_KEYS = [...BASE_COLUMNS, ...EXTRA_COLUMNS.map(([key]) => key)];
const columnLabels = {
  fba: "Shipment ID Amazon FBA", tracking: "Numer UPS", ship_date: "Data nadania",
  delivery_date: "Data doręczenia", weight: "Waga", ups_status: "Status UPS",
  queue: "Kolejka", document: "Dokument"
};
for (const [key, label] of EXTRA_COLUMNS) columnLabels[key] = label;
const DEFAULT_VISIBLE = BASE_COLUMNS.filter((key) => key !== "weight");
const DEFAULT_WIDTHS = {
  fba: 215, tracking: 178, ship_date: 119, delivery_date: 146,
  weight: 95, ups_status: 156, queue: 62, document: 126
};
for (const [key] of EXTRA_COLUMNS) DEFAULT_WIDTHS[key] = 144;
function validKeys(value) {
  return Array.isArray(value) && value.length && new Set(value).size === value.length
    && value.every((key) => COLUMN_KEYS.includes(key));
}
function readJsonCookie(key) {
  try { return JSON.parse(getCookie(key) || "null"); } catch { return null; }
}
const priorOrder = getCookie("ups_pod_column_order").split(",").filter(Boolean);
let columnOrder = validKeys(priorOrder) && BASE_COLUMNS.every((key) => priorOrder.includes(key))
  ? [...priorOrder, ...COLUMN_KEYS.filter((key) => !priorOrder.includes(key))]
  : [...COLUMN_KEYS];
const priorVisible = readJsonCookie("ups_pod_columns_visible");
let visibleColumns = new Set(validKeys(priorVisible) ? priorVisible : DEFAULT_VISIBLE);
const savedWidths = readJsonCookie("ups_pod_column_widths");
let columnWidths = { ...DEFAULT_WIDTHS };
if (savedWidths && typeof savedWidths === "object") {
  for (const key of COLUMN_KEYS) {
    const value = savedWidths[key];
    if (Number.isFinite(value) && value >= 45 && value <= 700)
      columnWidths[key] = Math.round(value);
  }
}
let sortBy = COLUMN_KEYS.includes(getCookie("ups_pod_sort"))
  ? getCookie("ups_pod_sort") : "ship_date";
let sortDirection = ["asc", "desc"].includes(getCookie("ups_pod_sort_direction"))
  ? getCookie("ups_pod_sort_direction") : "desc";
let columnFilters = {};
try {
  const stored = JSON.parse(getCookie("ups_pod_column_filters") || "{}");
  if (stored && typeof stored === "object" && !Array.isArray(stored)) {
    for (const key of COLUMN_KEYS) {
      if (typeof stored[key] === "string") columnFilters[key] = stored[key].slice(0, 100);
    }
  }
} catch { columnFilters = {}; }
let supportConfigured = false;
let refreshSerial = 0;
const $ = (id) => document.getElementById(id);

function saveColumnPrefs() {
  saveCookie("ups_pod_column_order", columnOrder.join(","));
  saveCookie("ups_pod_columns_visible", JSON.stringify([...visibleColumns]));
  saveCookie("ups_pod_column_widths", JSON.stringify(columnWidths));
  saveCookie("ups_pod_sort", sortBy);
  saveCookie("ups_pod_sort_direction", sortDirection);
}
function applyColumnWidths() {
  const table = $("shipments-table");
  const visibleWidth = columnOrder.filter((key) => visibleColumns.has(key))
    .reduce((sum, key) => sum + columnWidths[key], 0);
  table.style.width = Math.max(visibleWidth, 300) + "px";
  table.style.minWidth = Math.max(visibleWidth, 300) + "px";
  for (const cell of table.querySelectorAll("[data-col]")) {
    const key = cell.dataset.col;
    if (!COLUMN_KEYS.includes(key)) continue;
    const width = columnWidths[key];
    cell.style.width = width + "px";
    cell.style.minWidth = width + "px";
    cell.style.maxWidth = width + "px";
    const shown = visibleColumns.has(key);
    cell.hidden = !shown;
    cell.classList.toggle("column-hidden", !shown);
  }
  for (const resize of table.querySelectorAll(".column-resize")) {
    const key = resize.closest("[data-col]")?.dataset.col;
    if (key) resize.setAttribute("aria-valuenow", String(columnWidths[key]));
  }
}
function applyColumnOrder() {
  const table = $("shipments-table");
  const head = table.tHead.rows[0];
  const filters = table.tHead.rows[1];
  for (const key of columnOrder) {
    head.append(head.querySelector('[data-col="' + key + '"]'));
    filters.append(filters.querySelector('[data-col="' + key + '"]'));
  }
  for (const row of table.tBodies[0].rows) {
    if (row.cells.length !== COLUMN_KEYS.length) continue;
    for (const key of columnOrder)
      row.append(row.querySelector('[data-col="' + key + '"]'));
  }
  for (const th of head.cells) {
    const key = th.dataset.col;
    th.setAttribute("aria-sort", key !== sortBy ? "none"
      : sortDirection === "asc" ? "ascending" : "descending");
    const direction = th.querySelector(".sort-indicator");
    if (direction)
      direction.textContent = key === sortBy ? (sortDirection === "asc" ? "↑" : "↓") : "↕";
  }
  applyColumnWidths();
}
function moveColumn(dragged, target) {
  if (!COLUMN_KEYS.includes(dragged) || !COLUMN_KEYS.includes(target) || dragged === target) return;
  const next = columnOrder.filter((key) => key !== dragged);
  next.splice(next.indexOf(target), 0, dragged);
  columnOrder = next;
  saveColumnPrefs();
  applyColumnOrder();
}
function buildColumnPicker() {
  const target = $("columns-picker");
  if (!target) return;
  target.replaceChildren();
  for (const group of [
    { title: "Podstawowe", fields: BASE_COLUMNS },
    { title: "UPS Tracking API", fields: EXTRA_COLUMNS.map(([key]) => key) }
  ]) {
    const heading = document.createElement("h3");
    heading.textContent = group.title;
    target.append(heading);
    for (const key of group.fields) {
      const label = document.createElement("label");
      label.className = "column-choice";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = visibleColumns.has(key);
      const span = document.createElement("span");
      span.textContent = columnLabels[key];
      const path = EXTRA_COLUMNS.find(([candidate]) => candidate === key)?.[2];
      if (path) {
        const help = document.createElement("small");
        help.textContent = path;
        span.append(help);
      }
      input.addEventListener("change", () => {
        if (input.checked) visibleColumns.add(key);
        else if (visibleColumns.size > 1) visibleColumns.delete(key);
        else { input.checked = true; return; }
        saveColumnPrefs();
        applyColumnWidths();
      });
      label.append(input, span);
      target.append(label);
    }
  }
}
function initColumns() {
  const header = $("shipments-table").tHead.rows[0];
  const filterRow = document.createElement("tr");
  filterRow.className = "column-filters";
  filterRow.setAttribute("aria-label", "Filtry poszczególnych kolumn");
  let dragging = "";
  for (const [key, label] of EXTRA_COLUMNS) {
    const th = document.createElement("th");
    th.textContent = label;
    th.title = EXTRA_COLUMNS.find(([candidate]) => candidate === key)?.[2] || "";
    header.append(th);
  }
  [...header.cells].forEach((th, index) => {
    const key = COLUMN_KEYS[index];
    th.dataset.col = key;
    th.classList.add("column-header");
    const check = key === "fba" ? $("select-page") : null;
    th.replaceChildren();
    if (check) {
      check.title = "Zaznacz wszystkie przesyłki na bieżącej stronie";
      th.append(check);
    }
    const sort = document.createElement("button");
    sort.type = "button";
    sort.className = "sort-header";
    sort.setAttribute("aria-label", "Sortuj według: " + columnLabels[key]);
    sort.textContent = columnLabels[key];
    const direction = document.createElement("span");
    direction.className = "sort-indicator";
    direction.setAttribute("aria-hidden", "true");
    sort.append(direction);
    sort.addEventListener("click", () => {
      sortDirection = key === sortBy
        ? (sortDirection === "asc" ? "desc" : "asc") : "asc";
      sortBy = key;
      offset = 0;
      saveColumnPrefs();
      applyColumnOrder();
      void refresh();
    });
    th.append(sort);
    const drag = document.createElement("span");
    drag.className = "column-grip";
    drag.draggable = true;
    drag.tabIndex = 0;
    drag.setAttribute("role", "button");
    drag.setAttribute("aria-label", "Przenieś kolumnę: " + columnLabels[key]
      + ". Przeciągnij lub użyj Alt i strzałek.");
    drag.title = "Przeciągnij, aby zmienić pozycję kolumny; Alt + strzałki";
    drag.textContent = "⠿";
    drag.addEventListener("dragstart", (event) => {
      dragging = key;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", key);
      th.classList.add("dragging-column");
    });
    drag.addEventListener("dragend", () => {
      dragging = "";
      document.querySelectorAll(".dragging-column,.column-target").forEach((node) =>
        node.classList.remove("dragging-column", "column-target"));
    });
    // Native HTML DnD has inconsistent touch support. Pointer fallback moves
    // the column only when the handle is released over another header.
    let touchPointer = null;
    drag.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "mouse") return;
      touchPointer = event.pointerId;
      drag.setPointerCapture(event.pointerId);
      th.classList.add("dragging-column");
    });
    drag.addEventListener("pointermove", (event) => {
      if (touchPointer !== event.pointerId) return;
      document.querySelectorAll(".column-target").forEach((node) =>
        node.classList.remove("column-target"));
      const target = document.elementFromPoint(event.clientX, event.clientY)?.closest("th[data-col]");
      if (target && target !== th) target.classList.add("column-target");
    });
    drag.addEventListener("pointerup", (event) => {
      if (touchPointer !== event.pointerId) return;
      const target = document.elementFromPoint(event.clientX, event.clientY)?.closest("th[data-col]");
      touchPointer = null;
      th.classList.remove("dragging-column");
      document.querySelectorAll(".column-target").forEach((node) =>
        node.classList.remove("column-target"));
      if (target?.dataset.col) moveColumn(key, target.dataset.col);
    });
    drag.addEventListener("pointercancel", () => {
      touchPointer = null;
      th.classList.remove("dragging-column");
    });
    drag.addEventListener("keydown", (event) => {
      if (!event.altKey || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const pos = columnOrder.indexOf(key);
      const other = columnOrder[pos + (event.key === "ArrowRight" ? 1 : -1)];
      if (other) {
        const next = [...columnOrder];
        const neighbor = next.indexOf(other);
        [next[pos], next[neighbor]] = [next[neighbor], next[pos]];
        columnOrder = next;
        saveColumnPrefs(); applyColumnOrder(); drag.focus();
      }
    });
    th.append(drag);
    const resize = document.createElement("span");
    resize.className = "column-resize";
    resize.setAttribute("role", "separator");
    resize.tabIndex = 0;
    resize.setAttribute("aria-orientation", "vertical");
    resize.setAttribute("aria-label", "Zmień szerokość: " + columnLabels[key]);
    resize.setAttribute("aria-valuemin", "45");
    resize.setAttribute("aria-valuemax", "700");
    resize.title = "Przeciągnij, aby zmienić szerokość; strzałki na klawiaturze";
    let resizing = null;
    resize.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      resizing = { pointer: event.pointerId, startX: event.clientX, width: columnWidths[key] };
      resize.setPointerCapture(event.pointerId);
      th.classList.add("resizing-column");
    });
    resize.addEventListener("pointermove", (event) => {
      if (!resizing || resizing.pointer !== event.pointerId) return;
      columnWidths[key] = Math.min(700, Math.max(45,
        Math.round(resizing.width + event.clientX - resizing.startX)));
      applyColumnWidths();
    });
    const finishResize = () => {
      if (!resizing) return;
      resizing = null;
      th.classList.remove("resizing-column");
      saveColumnPrefs();
    };
    resize.addEventListener("pointerup", finishResize);
    resize.addEventListener("pointercancel", finishResize);
    resize.addEventListener("dblclick", () => {
      columnWidths[key] = DEFAULT_WIDTHS[key]; applyColumnWidths(); saveColumnPrefs();
    });
    resize.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault(); event.stopPropagation();
      const delta = event.key === "ArrowRight" ? 10 : -10;
      columnWidths[key] = Math.min(700, Math.max(45, columnWidths[key] + delta));
      applyColumnWidths(); saveColumnPrefs();
    });
    th.append(resize);
    th.addEventListener("dragover", (event) => {
      if (!dragging) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      th.classList.add("column-target");
    });
    th.addEventListener("dragleave", () => th.classList.remove("column-target"));
    th.addEventListener("drop", (event) => {
      event.preventDefault();
      th.classList.remove("column-target");
      moveColumn(dragging || event.dataTransfer.getData("text/plain"), key);
      dragging = "";
    });

    const filterCell = document.createElement("th");
    filterCell.dataset.col = key;
    const input = document.createElement("input");
    input.type = "search";
    input.className = "column-filter";
    input.dataset.col = key;
    input.maxLength = 100;
    input.autocomplete = "off";
    input.placeholder = "Filtruj…";
    input.value = columnFilters[key] || "";
    input.setAttribute("aria-label", "Filtruj: " + columnLabels[key]);
    let delay;
    input.addEventListener("input", () => {
      columnFilters[key] = input.value;
      saveCookie("ups_pod_column_filters", JSON.stringify(columnFilters));
      clearTimeout(delay);
      delay = setTimeout(() => { offset = 0; void refresh(); }, 300);
    });
    filterCell.append(input);
    filterRow.append(filterCell);
  });
  header.after(filterRow);
  applyColumnOrder();
  buildColumnPicker();
}


async function requestJson(url, options = {}) {
  const headers = { "X-CSRF-Token": csrf, ...(options.headers || {}) };
  const response = await fetch(url, { credentials: "same-origin", ...options, headers });
  let payload;
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) throw new Error(payload.error || "Błąd żądania: HTTP " + response.status);
  return payload;
}

function notice(message, isError = false) {
  const el = $("notice");
  el.hidden = false;
  el.textContent = message;
  el.classList.toggle("error", isError);
}

function addCell(tr, text, className = "") {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = String(text ?? "");
  tr.appendChild(cell);
  return cell;
}

function statusInfo(row) {
  if (row.state === "ready") return ["POD pobrany", "ready", "Zarchiwizowano dokument POD"];
  if (row.state === "processing") return ["Pobieranie…", "processing", "Trwa komunikacja z UPS"];
  if (row.state === "retry" && (row.error === "Przesyłka jeszcze niedoręczona."
      || /w drodze|w tranzycie|in transit/i.test(row.ups_status || row.description)))
    return ["W drodze", "waiting", row.description || row.error];
  if (row.state === "retry") return ["Brak POD / ponowienie", "retry", row.error || "Nie udało się pobrać dokumentu"];
  return ["W kolejce", "queued", "Oczekuje na automatyczne przetworzenie"];
}

function trackingUrl(number) {
  return "https://www.ups.com/track?tracknum=" + encodeURIComponent(number);
}

let listReturnPoint = null;
function openShipmentLogs(row) {
  const table = document.querySelector("#shipments-panel .table-wrap");
  listReturnPoint = {
    tracking: row.tracking,
    scrollX: window.scrollX, scrollY: window.scrollY,
    tableX: table.scrollLeft, offset, scope, month: activeMonth,
    query: $("query").value,
  };
  $("logs-return").hidden = false;
  $("logs-tracking").value = row.tracking;
  logBefore = 0;
  $("tab-logs").click();
}

function renderRows() {
  hideUpsTooltip();
  const tbody = $("rows");
  tbody.replaceChildren();
  if (!rows.length) {
    const tr = document.createElement("tr");
    addCell(tr, "Brak przesyłek w wybranym miesiącu.", "empty").colSpan = visibleColumns.size;
    tbody.append(tr);
  }
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.dataset.tracking = row.tracking;
    const tick = document.createElement("input");
    tick.type = "checkbox";
    tick.checked = selected.has(row.tracking);
    tick.setAttribute("aria-label", "Zaznacz " + row.tracking);
    tick.addEventListener("click", (event) => {
      if (event.shiftKey && tick.checked) {
        const current = rows.findIndex((item) => item.tracking === row.tracking);
        const prior = rows.slice(0, current).map((item, index) =>
          selected.has(item.tracking) ? index : -1).filter((index) => index >= 0);
        if (prior.length) {
          for (let index = prior[prior.length - 1]; index <= current; index++)
            selected.add(rows[index].tracking);
        } else selected.add(row.tracking);
      } else if (tick.checked) selected.add(row.tracking);
      else selected.delete(row.tracking);
      renderRows();
    });
    // Data columns: FBA, UPS, ship date, delivery date, weight, UPS status, queue, document.
    const fbaCell = document.createElement("td");
    fbaCell.className = "fba-cell";
    const fbaValue = document.createElement("button");
    fbaValue.type = "button";
    fbaValue.textContent = row.shipment_id || "Nieprzypisany";
    fbaValue.className = "fba-edit-link " + (row.shipment_id ? "fba-id" : "muted");
    fbaValue.title = "Kliknij, aby edytować Shipment ID Amazon FBA";
    fbaValue.setAttribute("aria-label", "Edytuj FBA dla " + row.tracking);
    fbaValue.addEventListener("click", () => editFba(row));
    fbaCell.append(tick, fbaValue);
    tr.append(fbaCell);

    const trackCell = document.createElement("td");
    trackCell.className = "track";
    const trackingLink = document.createElement("a");
    trackingLink.textContent = row.tracking;
    trackingLink.href = trackingUrl(row.tracking);
    trackingLink.target = "_blank";
    trackingLink.rel = "noopener noreferrer";
    trackingLink.referrerPolicy = "no-referrer";
    trackingLink.title = "Otwórz śledzenie przesyłki w UPS";
    trackCell.append(trackingLink);
    tr.append(trackCell);

    const shipDate = addCell(tr, row.ship_date || "—", "ship-date");
    shipDate.title = {
      manual: "Data podana przez użytkownika / import FBA",
      ups_pickup_date: "Pole pickupDate zwrócone przez UPS (data odbioru/nadania wg przewoźnika)",
      ups_pickup_scan: "Data skanu odbioru przesyłki przez UPS",
      pod_shipped_or_billed: "Data «Shipped or Billed on» z dokumentu UPS; nie musi oznaczać odbioru fizycznego",
    }[row.ship_date_source] || "Data nieznana";

    const deliveryDay = row.delivery_date && /^\d{8}$/.test(row.delivery_date)
      ? row.delivery_date.slice(0, 4) + "-" + row.delivery_date.slice(4, 6)
        + "-" + row.delivery_date.slice(6) : "—";
    const isPlannedDate = ["SDD", "RDD"].includes(row.delivery_date_type) && deliveryDay !== "—";
    const dateCell = addCell(tr, deliveryDay,
      "delivery-date" + (isPlannedDate ? " planned" : ""));
    dateCell.title = row.delivery_date_type === "DEL" ? "Potwierdzona data doręczenia"
      : row.delivery_date_type === "RDD" ? "Nowy termin dostawy (RDD)"
      : row.delivery_date_type === "SDD" ? "Planowana data dostawy (SDD)"
      : row.delivery_date_type || "Brak informacji";

    const weightLabel = row.weight_value
      ? row.weight_value + " " + (row.weight_unit === "KGS" ? "kg"
        : row.weight_unit === "LBS" ? "lb" : row.weight_unit) : "—";
    addCell(tr, weightLabel, "weight-cell");

    const upsCell = document.createElement("td");
    const upsTag = document.createElement("span");
    const upsText = row.ups_status || "Bez statusu";
    upsTag.textContent = upsText;
    const lowerStatus = upsText.toLocaleLowerCase("pl-PL");
    const upsKind = row.ups_status_code === "D" || /doręczon|dostarczon|delivered/.test(lowerStatus)
      ? "ready"
      : row.ups_status_code === "X" || /wyjątek|problem|błąd|rescheduled|delay|exception|failed/.test(lowerStatus)
      ? "retry" : /w drodze|w tranzycie|transport|on the way|in transit/.test(lowerStatus)
      ? "processing" : "queued";
    upsTag.className = "status ups-status " + upsKind;
    const statusDetails = [
      upsText,
      row.ups_status_detail && row.ups_status_detail !== upsText ? row.ups_status_detail : "",
      row.ups_status_code ? "Typ UPS: " + row.ups_status_code : "",
      row.ups_tracking_code ? "Status: " + row.ups_tracking_code : "",
      row.ups_event_code ? "Zdarzenie: " + row.ups_event_code : "",
      row.description && row.description !== upsText && row.description !== row.ups_status_detail
        ? row.description : "",
    ].filter(Boolean).join(" · ");
    upsTag.dataset.tooltip = statusDetails;
    upsTag.setAttribute("aria-label", statusDetails);
    upsTag.tabIndex = 0;
    upsCell.append(upsTag);
    tr.append(upsCell);

    const statusCell = document.createElement("td");
    statusCell.className = "compact-queue";
    const queueTag = document.createElement("span");
    const info = statusInfo(row);
    const late = isPastDeliveryDate(row);
    const queueKind = row.state === "ready" ? "done"
      : row.state === "processing" ? "in-progress"
      : late ? "exception"
      : row.state === "retry" && info[1] === "retry" ? "error"
      : "queued";
    const queueLabel = {
      done: "POD pobrany", "in-progress": "Przetwarzanie",
      exception: "Przekroczony termin, brak potwierdzenia",
      error: "Błąd przetwarzania", queued: "W kolejce"
    }[queueKind];
    queueTag.className = "queue-dot " + queueKind;
    queueTag.setAttribute("role", "img");
    queueTag.setAttribute("aria-label", queueLabel);
    queueTag.title = queueLabel + " · " + (row.error || row.description || info[2]);
    statusCell.append(queueTag);
    tr.append(statusCell);

    const doc = document.createElement("td");
    doc.className = "compact-document";
    if (row.state === "ready") {
      const link = document.createElement("a");
      link.className = "status ready document-bubble";
      link.href = "/api/shipments/" + encodeURIComponent(row.tracking) + "/pdf";
      link.textContent = "↓ PDF";
      link.setAttribute("download", "");
      link.setAttribute("aria-label", "Pobierz PDF potwierdzenia doręczenia " + row.tracking);
      link.title = "Pobierz potwierdzenie doręczenia (PDF)";
      doc.append(link);
    } else if (isPastDeliveryDate(row) && row.support_status === "sent") {
      const sent = document.createElement("span");
      sent.className = "status ready support-sent";
      sent.textContent = "E-mail wysłany";
      sent.title = "Serwer SMTP przyjął wiadomość. UPS nie potwierdził jeszcze jej odbioru.";
      doc.append(sent);
    } else if (isPastDeliveryDate(row) && ["pending", "uncertain"].includes(row.support_status)) {
      const pending = document.createElement("span");
      pending.className = "status waiting support-sent";
      pending.textContent = "Weryfikuj";
      pending.title = "Nie ponawiaj automatycznie — zweryfikuj, czy poprzednia wiadomość dotarła";
      doc.append(pending);
    } else if (isPastDeliveryDate(row)) {
      const support = document.createElement("button");
      support.type = "button";
      support.className = "status document-bubble support-send";
      support.textContent = "Zgłoś UPS";
      support.title = supportConfigured
        ? "Wyślij do oficjalnej obsługi UPS zapytanie o brak skanu doręczenia"
        : "Wysyłka wymaga skonfigurowanego serwera SMTP na Synology";
      support.dataset.tracking = row.tracking;
      support.addEventListener("click", () => void sendUpsSupport(row, support));
      doc.append(support);
    } else {
      const last = row.last_attempt_at
        ? new Date(row.last_attempt_at).toLocaleString("pl-PL") : "Nie sprawdzano";
      const next = row.next_attempt_at && row.state === "retry"
        ? " · Kolejna próba: " + new Date(row.next_attempt_at).toLocaleString("pl-PL") : "";
      const bubble = document.createElement("button");
      bubble.type = "button";
      bubble.className = "status document-bubble " + (info[1] === "waiting" ? "waiting"
        : info[1] === "retry" ? "retry" : "queued");
      bubble.textContent = info[1] === "retry" ? "Błąd" : "Oczekuje";
      bubble.title = "Próby: " + row.attempts + " · Ostatnio: " + last + next
        + (row.error ? " · " + row.error : "");
      bubble.setAttribute("aria-label", "Historia operacji " + row.tracking);
      bubble.dataset.tracking = row.tracking;
      bubble.addEventListener("click", () => openShipmentLogs(row));
      doc.append(bubble);
    }
    tr.append(doc);
    for (const [key, label] of EXTRA_COLUMNS) {
      const value = (row.ups_extra || {})[key] || "—";
      const cell = addCell(tr, value, "extra-cell");
      cell.title = label + ": " + value;
    }
    [...tr.cells].forEach((cell, index) => { cell.dataset.col = COLUMN_KEYS[index]; });
    tbody.append(tr);
  }
  $("page-info").textContent = total
    ? "Pozycje " + (offset + 1) + "–" + (offset + rows.length) + " z " + total
    : "Brak pozycji";
  $("prev-page").disabled = offset === 0;
  $("next-page").disabled = offset + PAGE_SIZE >= total;
  $("select-page").checked = rows.length > 0 && rows.every((r) => selected.has(r.tracking));
  syncSelected();
  applyColumnOrder();
}

const upsTooltip = document.createElement("div");
upsTooltip.className = "ups-tooltip";
upsTooltip.setAttribute("role", "tooltip");
upsTooltip.hidden = true;
document.body.append(upsTooltip);
function showUpsTooltip(target) {
  if (!target || !target.classList.contains("ups-status")) return;
  upsTooltip.textContent = target.dataset.tooltip || target.textContent;
  upsTooltip.hidden = false;
  const box = target.getBoundingClientRect();
  upsTooltip.style.left = Math.max(8, Math.min(box.left, window.innerWidth - upsTooltip.offsetWidth - 8)) + "px";
  const above = box.top - upsTooltip.offsetHeight - 8;
  upsTooltip.style.top = (above >= 8 ? above : box.bottom + 8) + "px";
}
function hideUpsTooltip() { upsTooltip.hidden = true; }
$("rows").addEventListener("mouseover", (event) => {
  if (event.target.closest) showUpsTooltip(event.target.closest(".ups-status"));
});
$("rows").addEventListener("mouseout", (event) => {
  if (event.target.closest && event.target.closest(".ups-status")) hideUpsTooltip();
});
$("rows").addEventListener("focusin", (event) => {
  if (event.target.closest) showUpsTooltip(event.target.closest(".ups-status"));
});
$("rows").addEventListener("focusout", hideUpsTooltip);

document.addEventListener("keydown", (event) => { if (event.key === "Escape") hideUpsTooltip(); });

function syncSelected() {
  for (const row of rows) {
    if (row.state === "ready" && selected.has(row.tracking)) readySelected.add(row.tracking);
    else readySelected.delete(row.tracking);
  }
  const count = selected.size;
  const available = [...readySelected].filter((number) => selected.has(number)).length;
  $("download-selected").textContent = count
    ? "Pobierz ZIP (" + available + " PDF / " + count + " wybranych)" : "Pobierz zaznaczone ZIP";
  $("download-selected").title = count && !available
    ? "Żaden zaznaczony numer nie ma jeszcze gotowego potwierdzenia POD."
    : "Pobierz gotowe POD; przesyłki bez dokumentu zostaną pominięte.";
  $("retry-selected").textContent = count
    ? "Ponów zaznaczone (" + count + ")" : "Ponów zaznaczone";
  $("fba-selected").textContent = count
    ? "Przypisz FBA zaznaczonym (" + count + ")" : "Przypisz FBA zaznaczonym";
  $("fba-selected").disabled = count === 0;
  $("download-selected").disabled = available === 0;
  $("retry-selected").disabled = count === 0;
}

async function refresh() {
  const serial = ++refreshSerial;
  const params = new URLSearchParams({
    scope, month: activeMonth, q: $("query").value.trim(),
    sort: sortBy, direction: sortDirection,
    limit: String(PAGE_SIZE), offset: String(offset),
  });
  for (const key of COLUMN_KEYS)
    if ((columnFilters[key] || "").trim()) params.set("f_" + key, columnFilters[key].trim());
  try {
    const payload = await requestJson("/api/shipments?" + params);
    if (serial !== refreshSerial) return;
    rows = payload.rows;
    total = payload.total;
    supportConfigured = payload.support_configured === true;
    const selector = $("month-filter");
    const available = [...new Set([activeMonth, ...(payload.months || [])])]
      .filter((m) => /^\d{4}-\d{2}$/.test(m)).sort().reverse();
    const currentOptions = [...selector.options].map((o) => o.value);
    if (JSON.stringify(currentOptions) !== JSON.stringify(["all", ...available])) {
      selector.replaceChildren();
      selector.add(new Option("Wszystkie miesiące", "all"));
      for (const value of available) {
        const name = new Intl.DateTimeFormat("pl-PL", { month: "long", year: "numeric" })
          .format(new Date(value + "-01T12:00:00"));
        selector.add(new Option(name, value));
      }
    }
    selector.value = activeMonth;
    $("count-total").textContent = payload.stats.total;
    $("count-ready").textContent = payload.stats.ready;
    $("count-active").textContent = payload.stats.queued + payload.stats.processing;
    $("count-retry").textContent = payload.stats.retry;
    $("refresh-status").textContent = "Aktualizacja: " + new Date().toLocaleTimeString("pl-PL");
    renderRows();
  } catch (error) {
    if (serial !== refreshSerial) return;
    // A background GET failure is not an import failure. Keep existing rows
    // and retry on the next refresh, without changing the import notice.
    $("refresh-status").textContent = "Nie udało się odświeżyć: "
      + error.message + " · ponowna próba automatycznie";
  }
}

function setIntakeCollapsed(collapsed) {
  $("intake-content").hidden = collapsed;
  $("intake-panel").classList.toggle("is-collapsed", collapsed);
  $("intake-toggle").setAttribute("aria-expanded", String(!collapsed));
  $("intake-toggle").textContent = collapsed ? "+ Rozwiń" : "− Zwiń";
  saveCookie("ups_pod_intake_collapsed", collapsed ? "1" : "0");
}
$("intake-toggle").addEventListener("click", () => {
  setIntakeCollapsed(!$("intake-content").hidden);
});
// When there is no preference, prioritize the tracking grid instead of the importer.
setIntakeCollapsed(getCookie("ups_pod_intake_collapsed") !== "0");
$("query").value = getCookie("ups_pod_query").slice(0, 200);
document.querySelectorAll("[data-scope]").forEach((button) => {
  button.classList.toggle("active", button.dataset.scope === scope);
});

function updateThemeButton() {
  const dark = document.documentElement.dataset.theme === "dark";
  $("theme-symbol").textContent = dark ? "☀" : "☾";
  $("theme-caption").textContent = dark ? "Jasny" : "Ciemny";
  $("theme-toggle").setAttribute("aria-pressed", String(dark));
  $("theme-toggle").setAttribute("aria-label", dark ? "Włącz tryb jasny" : "Włącz tryb ciemny");
}
$("theme-toggle").addEventListener("click", () => {
  document.documentElement.dataset.theme =
    document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  saveCookie("ups_pod_theme", document.documentElement.dataset.theme);
  updateThemeButton();
});
updateThemeButton();

initColumns();
$("columns-open").addEventListener("click", () => {
  buildColumnPicker();
  $("columns-dialog").showModal();
});
$("columns-done").addEventListener("click", () => $("columns-dialog").close());
$("columns-reset").addEventListener("click", () => {
  columnOrder = [...COLUMN_KEYS];
  visibleColumns = new Set(DEFAULT_VISIBLE);
  columnWidths = { ...DEFAULT_WIDTHS };
  saveColumnPrefs(); applyColumnOrder(); buildColumnPicker();
});
$("clear-column-filters").addEventListener("click", () => {
  columnFilters = {};
  document.querySelectorAll(".column-filter").forEach((input) => { input.value = ""; });
  saveCookie("ups_pod_column_filters", "{}");
  offset = 0;
  void refresh();
});

$("intake-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const raw = $("trackings").value;
  if (!raw.trim()) { notice("Wklej przynajmniej jeden numer.", true); return; }
  const button = $("add-btn");
  button.disabled = true;
  try {
    const result = await requestJson("/api/shipments", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ trackings: raw }),
    });
    notice("Dodano " + result.added + ", istniejące: " + result.existing
      + ", przypisano FBA: " + result.changed
      + ", pominięte fragmenty: " + result.rejected_count
      + (result.rejected.length ? " (" + result.rejected.slice(0, 8).join(", ") + ")" : "")
      + ". Pobieranie odbywa się automatycznie.");
    $("trackings").value = "";
    offset = 0;
    // A failed background refresh must not relabel a successful import as failed.
    // refresh() reports connection errors independently and does not throw.
    await refresh();
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});

document.querySelectorAll("[data-scope]").forEach((button) => {
  button.addEventListener("click", async () => {
    scope = button.dataset.scope;
    saveCookie("ups_pod_scope", scope);
    offset = 0;
    document.querySelectorAll("[data-scope]").forEach((b) =>
      b.classList.toggle("active", b === button));
    await refresh();
  });
});

$("month-filter").addEventListener("change", () => {
  activeMonth = $("month-filter").value;
  saveCookie("ups_pod_month", activeMonth);
  offset = 0;
  void refresh();
});
let searchTimeout;
$("query").addEventListener("input", () => {
  saveCookie("ups_pod_query", $("query").value.slice(0, 200));
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(() => { offset = 0; void refresh(); }, 300);
});
$("prev-page").addEventListener("click", () => { offset = Math.max(0, offset - PAGE_SIZE); void refresh(); });
$("next-page").addEventListener("click", () => { offset += PAGE_SIZE; void refresh(); });
$("select-page").addEventListener("change", (event) => {
  for (const row of rows) {
    if (event.target.checked) selected.add(row.tracking);
    else selected.delete(row.tracking);
  }
  renderRows();
});

async function downloadZip(all) {
  try {
    const eligible = [...readySelected].filter((number) => selected.has(number));
    if (!all && !eligible.length) {
      tableNotice("Żadna zaznaczona przesyłka nie ma gotowego POD do pobrania.", true);
      return;
    }
    const skipped = all ? 0 : selected.size - eligible.length;
    const payload = all ? { all: true } : { trackings: eligible };
    const response = await fetch("/api/zip", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || "HTTP " + response.status);
    }
    const blob = await response.blob();
    const link = document.createElement("a");
    const url = URL.createObjectURL(blob);
    link.href = url;
    link.download = "ups-pod-potwierdzenia.zip";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    const included = Number(response.headers.get("X-UPS-POD-Count")) || eligible.length;
    tableNotice("Przygotowano ZIP: " + included + " dokumentów."
      + (skipped ? " Pominięto " + skipped + " zaznaczonych przesyłek bez POD." : ""));
  } catch (error) { tableNotice("Nie udało się pobrać ZIP: " + error.message, true); }
}

$("download-selected").addEventListener("click", () => void downloadZip(false));
$("download-all").addEventListener("click", () => void downloadZip(true));
$("retry-selected").addEventListener("click", async () => {
  try {
    const result = await requestJson("/api/retry", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ trackings: [...selected] }),
    });
    notice("Ponownie skierowano do kolejki: " + result.requeued);
    await refresh();
  } catch (error) { notice(error.message, true); }
});
let fbaContext = [];
function tableNotice(message, error = false) {
  const box = $("table-notice");
  box.textContent = message;
  box.hidden = false;
  box.classList.toggle("error", error);
}
function updateFbaConflictHint() {
  const typed = $("fba-dialog-id").value.trim().toUpperCase();
  const conflicts = fbaContext.filter((item) =>
    item.shipment_id && item.shipment_id !== typed).length;
  const help = $("fba-help");
  help.textContent = conflicts
    ? conflicts + " widocznych przesyłek ma inne FBA. Zaznacz zastąpienie, aby je zmienić."
    : "Jedno FBA może zostać przypisane do wielu numerów UPS.";
}
function openFbaEditor(entries) {
  if (!entries.length) return;
  fbaContext = entries;
  const single = entries.length === 1;
  $("fba-dialog-title").textContent = single ? "Edytuj Shipment ID"
    : "Przypisz Shipment ID do " + entries.length + " przesyłek";
  $("fba-dialog-caption").textContent = single
    ? "Numer UPS: " + entries[0].tracking
    : "Zmiana dotyczy " + entries.length + " zaznaczonych numerów UPS.";
  const shared = [...new Set(entries.map((r) => r.shipment_id).filter(Boolean))];
  $("fba-dialog-id").value = single && shared.length ? shared[0] : "";
  $("fba-replace").checked = false;
  $("fba-dialog-error").hidden = true;
  $("fba-save").disabled = false;
  updateFbaConflictHint();
  $("fba-dialog").showModal();
  $("fba-dialog-id").focus();
  $("fba-dialog-id").select();
}
function editFba(row) { openFbaEditor([row]); }
$("fba-dialog-id").addEventListener("input", updateFbaConflictHint);
$("fba-selected").addEventListener("click", () => {
  // Selected rows may span multiple pages; unknown assignments are verified server-side.
  const visible = new Map(rows.map((r) => [r.tracking, r]));
  openFbaEditor([...selected].map((tracking) =>
    visible.get(tracking) || { tracking, shipment_id: "" }));
});
$("fba-dialog-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const shipment_id = $("fba-dialog-id").value.trim().toUpperCase();
  const errorBox = $("fba-dialog-error");
  if (!/^FBA[A-Z0-9]+$/.test(shipment_id) || shipment_id === "FBA0000000") {
    errorBox.textContent = "Wpisz poprawny Shipment ID zaczynający się od FBA.";
    errorBox.hidden = false;
    $("fba-dialog-id").focus();
    return;
  }
  const replace = $("fba-replace").checked;
  const conflicts = fbaContext.filter((item) => item.shipment_id
    && item.shipment_id !== shipment_id);
  if (conflicts.length && !replace) {
    errorBox.textContent = "Istnieje inne przypisanie FBA. Zaznacz zgodę na jego zastąpienie.";
    errorBox.hidden = false;
    return;
  }
  const button = $("fba-save");
  button.disabled = true;
  errorBox.hidden = true;
  try {
    const result = await requestJson("/api/fba", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        entries: fbaContext.map((r) => ({ tracking: r.tracking, shipment_id })),
        replace,
      }),
    });
    $("fba-dialog").close();
    tableNotice("FBA przypisano do " + result.assigned + " przesyłek.");
    await refresh();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally { button.disabled = false; }
});

document.querySelectorAll("[data-close-dialog]").forEach((button) =>
  button.addEventListener("click", () => $(button.dataset.closeDialog).close()));

async function sendUpsSupport(row, button) {
  button.disabled = true;
  button.textContent = "Wysyłanie…";
  try {
    const result = await requestJson("/api/support/" + encodeURIComponent(row.tracking) + "/send", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    tableNotice("Serwer SMTP przyjął wiadomość dla " + row.tracking
      + " skierowaną do " + result.recipient + ". Odbiór przez UPS nie jest jeszcze potwierdzony.");
    await refresh();
  } catch (error) {
    button.disabled = false;
    button.textContent = "Zgłoś UPS";
    tableNotice(row.tracking + ": " + error.message, true);
  }
}

const WarsawDate = () => Object.fromEntries(
  new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Warsaw", year: "numeric", month: "2-digit", day: "2-digit"
  }).formatToParts(new Date()).map((item) => [item.type, item.value]));
function isPastDeliveryDate(row) {
  if (row.state === "ready" || row.ups_status_code === "D"
      || /^(delivered|doręczono)/i.test(row.ups_status || "")
      || !["SDD", "RDD"].includes(row.delivery_date_type)
      || !/^\d{8}$/.test(row.delivery_date || "")) return false;
  const now = WarsawDate();
  const today = now.year + now.month + now.day;
  return row.delivery_date < today;
}

void refresh();
setInterval(() => void refresh(), 7000);

let logsBusy = false;
let logBefore = 0;
let logScope = "all";
async function refreshLogs(older = false) {
  if (logsBusy || $("logs-panel").hidden) return;
  logsBusy = true;
  try {
    const params = new URLSearchParams();
    if (older && logBefore) params.set("before", String(logBefore));
    if ($("logs-tracking").value.trim()) params.set("tracking", $("logs-tracking").value.trim());
    if (logScope === "problems") params.set("levels", "WARNING,ERROR");
    else if (logScope !== "all") params.set("levels", logScope);
    const data = await requestJson("/api/logs?" + params);
    if (!older) $("log-rows").replaceChildren();
    for (const row of data.items) {
      const tr = document.createElement("tr");
      const level = ["INFO", "SUCCESS", "WARNING", "ERROR"].includes(row.level) ? row.level : "INFO";
      tr.className = "log-row level-" + level.toLowerCase()
        + " op-" + String(row.operation || "").replace(/[^a-z0-9_-]/gi, "").toLowerCase();
      for (const value of [new Date(row.created_at).toLocaleString("pl-PL"), level, row.actor,
        row.operation, row.tracking, row.http_status ?? "", row.description]) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.append(td);
      }
      $("log-rows").append(tr);
    }
    if (data.items.length) logBefore = data.items[data.items.length - 1].id;
    $("logs-status").textContent = "Aktualizacja " + new Date().toLocaleTimeString("pl-PL") + " · filtr: " + logScope;
    $("logs-older").disabled = data.items.length < 200;
  } catch (error) { $("logs-status").textContent = error.message; }
  finally { logsBusy = false; }
}
function markActiveTab(active) {
  for (const key of ["shipments", "logs", "config"]) {
    const button = $("tab-" + key);
    const chosen = active === key;
    button.classList.toggle("active", chosen);
    button.setAttribute("aria-selected", String(chosen));
    button.tabIndex = chosen ? 0 : -1;
    $(key + "-panel").hidden = !chosen;
  }
}
$("tab-logs").addEventListener("click", () => {
  markActiveTab("logs");
  if (!listReturnPoint) $("logs-return").hidden = true;
  void refreshLogs();
});
$("tab-shipments").addEventListener("click", () => {
  markActiveTab("shipments");
  $("logs-return").hidden = true;
});
$("tab-config").addEventListener("click", () => {
  markActiveTab("config");
  void refreshConfiguration();
});
$("logs-return").addEventListener("click", () => {
  const point = listReturnPoint;
  if (!point) return;
  $("tab-shipments").click();
  const table = document.querySelector("#shipments-panel .table-wrap");
  table.scrollLeft = point.tableX;
  requestAnimationFrame(() => {
    window.scrollTo(point.scrollX, point.scrollY);
    const target = [...document.querySelectorAll("#rows .document-bubble")]
      .find((button) => button.dataset.tracking === point.tracking);
    if (target) target.focus({ preventScroll: true });
  });
  listReturnPoint = null;
});
$("logs-older").addEventListener("click", () => void refreshLogs(true));
$("logs-tracking").addEventListener("input", () => { logBefore = 0; void refreshLogs(false); });
setInterval(() => void refreshLogs(), 7000);

document.querySelectorAll("[data-log-scope]").forEach((button) => {
  button.addEventListener("click", () => {
    logScope = button.dataset.logScope;
    logBefore = 0;
    document.querySelectorAll("[data-log-scope]").forEach((b) =>
      b.classList.toggle("active", b === button));
    void refreshLogs(false);
  });
});
$("show-tracking-fields").addEventListener("click", async () => {
  const pre = $("tracking-fields-list");
  try {
    const data = await requestJson("/api/tracking-fields");
    pre.textContent = data.paths.length
      ? data.paths.join("\n")
      : "Brak zapisanych kluczy. Pola zostaną zarejestrowane przy kolejnym odczycie UPS API.";
  } catch (error) { pre.textContent = error.message; }
});

document.querySelector(".view-tabs").addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const buttons = [$("tab-shipments"), $("tab-logs"), $("tab-config")];
  const index = buttons.indexOf(document.activeElement);
  const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
    : (index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
  buttons[next].click();
  buttons[next].focus();
});

// Settings: server only supplies masked indicators; all sensitive fields stay
// empty and are sent solely when the operator enters a replacement.
function configNotice(message, error = false) {
  const panel = $("config-notice");
  panel.hidden = false;
  panel.textContent = message;
  panel.classList.toggle("error", error);
}
async function refreshConfiguration() {
  try {
    const config = await requestJson("/api/config");
    const upsReady = !!config.ups_client_id && !!config.ups_client_secret;
    $("ups-config-indicator").textContent = upsReady ? "Skonfigurowano" : "Brak danych";
    $("ups-config-indicator").classList.toggle("configured", upsReady);
    $("ups-config-id").placeholder = config.ups_client_id || "Wpisz UPS Client ID";
    $("ups-config-secret").placeholder = config.ups_client_secret || "Wpisz UPS Client Secret";
    $("gmail-config-id").placeholder = config.google_client_id || "Google OAuth Client ID";
    $("gmail-config-secret").placeholder =
      config.google_client_secret || "Google OAuth Client Secret";
    const google = config.gmail;
    $("gmail-config-indicator").textContent =
      google.configured ? "Gmail połączony" : google.oauth_ready ? "Oczekuje na logowanie" : "Nie skonfigurowano";
    $("gmail-config-indicator").classList.toggle("configured", google.configured);
    $("gmail-connected-info").hidden = !google.configured;
    $("gmail-connected-info").textContent = google.configured
      ? "Aktywny nadawca: " + google.email : "";
    $("gmail-redirect-uri").textContent = google.redirect_uri;
    $("gmail-connect").disabled = !google.oauth_ready;
    $("gmail-connect").textContent = google.configured
      ? "Połącz ponownie / zmień konto" : "Połącz z kontem Google";
    $("gmail-disconnect").hidden = !google.configured;
  } catch (error) {
    configNotice("Błąd odczytu konfiguracji: " + error.message, true);
  }
}
$("ups-config-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    const result = await requestJson("/api/config/ups", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({client_id: $("ups-config-id").value.trim(),
        client_secret: $("ups-config-secret").value.trim()}),
    });
    $("ups-config-id").value = "";
    $("ups-config-secret").value = "";
    configNotice(result.changed.length ? "Dane UPS zapisano w chronionym .env."
      : "Nie wpisano nowych wartości; konfiguracja pozostała bez zmian.");
    await refreshConfiguration();
  } catch (error) {
    configNotice(error.message, true);
  } finally { button.disabled = false; }
});
$("gmail-config-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    const result = await requestJson("/api/config/google", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({client_id: $("gmail-config-id").value.trim(),
        client_secret: $("gmail-config-secret").value.trim()}),
    });
    $("gmail-config-id").value = "";
    $("gmail-config-secret").value = "";
    configNotice(result.changed.length
      ? "Klient Google OAuth zapisany. Teraz użyj «Połącz z kontem Google»."
      : "Nie wpisano nowych danych Google OAuth.");
    await refreshConfiguration();
  } catch (error) {
    configNotice(error.message, true);
  } finally { button.disabled = false; }
});
$("gmail-connect").addEventListener("click", async () => {
  const button = $("gmail-connect");
  button.disabled = true;
  try {
    const result = await requestJson("/api/config/gmail/start", { method: "POST" });
    const url = new URL(result.url);
    if (url.protocol !== "https:" || url.hostname !== "accounts.google.com")
      throw new Error("Nieprawidłowy adres autoryzacji Google.");
    window.location.assign(url.href);
  } catch (error) {
    configNotice("Nie można rozpocząć OAuth: " + error.message, true);
    button.disabled = false;
  }
});
$("gmail-disconnect").addEventListener("click", async () => {
  const button = $("gmail-disconnect");
  button.disabled = true;
  try {
    await requestJson("/api/config/gmail/disconnect", { method: "POST" });
    configNotice("Lokalny dostęp Gmail odłączony. Uprawnienia można również cofnąć w Google.");
    await refreshConfiguration();
  } catch (error) {
    configNotice(error.message, true);
  } finally { button.disabled = false; }
});
const oauthResult = new URLSearchParams(window.location.search).get("config");
if (oauthResult) {
  $("tab-config").click();
  const notes = {
    "gmail-connected": "Konto Gmail pomyślnie połączone.",
    "gmail-denied": "Logowanie Google zostało anulowane.",
    "gmail-failed": "Nie udało się zakończyć autoryzacji Google. Sprawdź Client ID, Secret i redirect URI.",
  };
  configNotice(notes[oauthResult] || "Powrót z autoryzacji Google.",
    oauthResult !== "gmail-connected");
  window.history.replaceState({}, "", window.location.pathname);
}
