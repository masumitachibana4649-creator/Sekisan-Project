const addRoomButton = document.querySelector("[data-add-room]");
const roomTable = document.querySelector("[data-room-table]");
const template = document.querySelector("#room-row-template");
const estimateForm = document.querySelector("[data-estimate-form]");
const loadingOverlay = document.querySelector("[data-loading-overlay]");
const pdfInput = document.querySelector("[data-pdf-input]");
const pdfPreview = document.querySelector("[data-pdf-preview]");
const openAddRoomButton = document.querySelector("[data-open-add-room]");
const addRoomDialog = document.querySelector("[data-add-room-dialog]");
const cancelAddRoomButton = document.querySelector("[data-cancel-add-room]");
const confirmAddRoomButton = document.querySelector("[data-confirm-add-room]");
const newRoomFloor = document.querySelector("[data-new-room-floor]");
const newRoomName = document.querySelector("[data-new-room-name]");
const newRoomError = document.querySelector("[data-new-room-error]");
const roomDetailBody = document.querySelector("[data-room-detail-body]");
const newRoomFields = document.querySelector("[data-new-room-fields]");
const resultTable = document.querySelector("[data-result-table]");
const resultScrollX = document.querySelector("[data-result-scroll-x]");
const resultScrollY = document.querySelector("[data-result-scroll-y]");
const resultScrollXSpacer = document.querySelector("[data-result-scroll-x-spacer]");
const resultScrollYSpacer = document.querySelector("[data-result-scroll-y-spacer]");

if (addRoomButton && roomTable && template) {
  addRoomButton.addEventListener("click", () => {
    roomTable.append(template.content.cloneNode(true));
    const lastRow = roomTable.querySelector(".room-line:last-child input");
    if (lastRow) lastRow.focus();
  });
}

if (estimateForm && loadingOverlay) {
  estimateForm.addEventListener("submit", () => {
    loadingOverlay.classList.add("is-visible");
    loadingOverlay.setAttribute("aria-hidden", "false");

    const submitButton = estimateForm.querySelector('button[type="submit"]');
    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = "計算中";
    }
  });
}

if (pdfInput && pdfPreview) {
  let previewUrl = "";
  pdfInput.addEventListener("change", () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl);

    const file = pdfInput.files && pdfInput.files[0];
    if (!file) {
      pdfPreview.classList.add("is-hidden");
      pdfPreview.removeAttribute("href");
      previewUrl = "";
      return;
    }

    previewUrl = URL.createObjectURL(file);
    pdfPreview.href = previewUrl;
    pdfPreview.classList.remove("is-hidden");
  });
}

if (resultTable && resultScrollX && resultScrollY && resultScrollXSpacer && resultScrollYSpacer) {
  initializeResultScrollbars();
}

if (openAddRoomButton && addRoomDialog && confirmAddRoomButton && newRoomFloor && newRoomName && roomDetailBody && newRoomFields) {
  openAddRoomButton.addEventListener("click", () => {
    if (newRoomError) newRoomError.classList.add("is-hidden");
    newRoomName.value = "";
    newRoomFloor.value = "1F";
    addRoomDialog.showModal();
    newRoomName.focus();
  });

  if (cancelAddRoomButton) {
    cancelAddRoomButton.addEventListener("click", () => addRoomDialog.close());
  }

  confirmAddRoomButton.addEventListener("click", () => {
    const roomName = newRoomName.value.trim();
    if (!roomName) {
      if (newRoomError) newRoomError.classList.remove("is-hidden");
      newRoomName.focus();
      return;
    }

    const floor = newRoomFloor.value || "1F";
    appendManualRoomPreview(floor, roomName);
    addRoomDialog.close();
  });
}

function appendManualRoomPreview(floor, roomName) {
  const row = document.createElement("div");
  row.className = "room-detail-group";
  const rowNumber = roomDetailBody.querySelectorAll(".room-detail-group").length + 1;
  const wallpaperOptions = wallpaperOptionsHtml("001");
  row.innerHTML = `
    <strong class="room-no-cell">${rowNumber}</strong>
    <span class="room-floor-cell">
      <select name="new_room_floor">
        <option value="1F" ${floor === "1F" ? "selected" : ""}>1F</option>
        <option value="2F" ${floor === "2F" ? "selected" : ""}>2F</option>
        <option value="3F" ${floor === "3F" ? "selected" : ""}>3F</option>
      </select>
    </span>
    <strong class="room-name-cell source-manual-room"><input name="new_room_name" value="${escapeAttribute(roomName)}"></strong>
    <span class="room-wallpaper-cell"><select class="room-wallpaper-select" name="new_room_east_wallpaper_no">${wallpaperOptions}</select></span>
    <span class="room-wallpaper-cell"><select class="room-wallpaper-select" name="new_room_west_wallpaper_no">${wallpaperOptions}</select></span>
    <span class="room-wallpaper-cell"><select class="room-wallpaper-select" name="new_room_south_wallpaper_no">${wallpaperOptions}</select></span>
    <span class="room-wallpaper-cell"><select class="room-wallpaper-select" name="new_room_north_wallpaper_no">${wallpaperOptions}</select></span>
    <span class="room-wallpaper-cell"><select class="room-wallpaper-select" name="new_room_ceiling_wallpaper_no">${wallpaperOptions}</select></span>
    <span class="room-total-cell">0.000 m2<br>0 本</span>
    <span class="room-note-cell" style="grid-row: 1 / span 4;">手動追加: 幅、高を入力してください</span>
    <label class="room-exclude-cell" style="grid-row: 5;">
      <input type="hidden" name="new_room_excluded_from_summary" value="0">
      <input class="room-exclude-checkbox" type="checkbox" value="1" data-new-room-exclude>
      <span>集計対象外</span>
    </label>
    <input type="hidden" name="new_room_opening_count" value="0" data-opening-count>
    ${surfaceMeasureRow("面積(m2)", 2, "area")}
    ${surfaceMeasureRow("幅(m)", 3, "width")}
    ${surfaceMeasureRow("高(m)", 4, "height")}
    <button class="room-opening-add-button secondary-action" type="button" style="grid-row: 6;" data-add-opening>開口部追加</button>
  `;
  roomDetailBody.append(row);
  updateResultScrollbars();
  const firstInput = row.querySelector('input[name="new_room_name"]');
  if (firstInput) firstInput.focus();
}

function initializeResultScrollbars() {
  let isSyncing = false;

  updateResultScrollbars();

  resultTable.addEventListener("scroll", () => {
    if (isSyncing) return;
    isSyncing = true;
    resultScrollX.scrollLeft = resultTable.scrollLeft;
    resultScrollY.scrollTop = resultTable.scrollTop;
    isSyncing = false;
  });

  resultScrollX.addEventListener("scroll", () => {
    if (isSyncing) return;
    isSyncing = true;
    resultTable.scrollLeft = resultScrollX.scrollLeft;
    isSyncing = false;
  });

  resultScrollY.addEventListener("scroll", () => {
    if (isSyncing) return;
    isSyncing = true;
    resultTable.scrollTop = resultScrollY.scrollTop;
    isSyncing = false;
  });

  window.addEventListener("resize", updateResultScrollbars);

  if ("ResizeObserver" in window) {
    const observer = new ResizeObserver(updateResultScrollbars);
    observer.observe(resultTable);
    if (roomDetailBody) observer.observe(roomDetailBody);
  }
}

function updateResultScrollbars() {
  if (!resultTable || !resultScrollX || !resultScrollY || !resultScrollXSpacer || !resultScrollYSpacer) return;

  resultScrollXSpacer.style.width = `${resultTable.scrollWidth}px`;
  resultScrollYSpacer.style.height = `${resultTable.scrollHeight}px`;
  resultScrollX.scrollLeft = resultTable.scrollLeft;
  resultScrollY.scrollTop = resultTable.scrollTop;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[character]));
}

function escapeAttribute(value) {
  return escapeHtml(value).replace(/`/g, "&#096;");
}

function wallpaperOptionsHtml(selectedNumber) {
  const source = document.querySelector(".room-wallpaper-select");
  if (!source) {
    return '<option value="001" selected>001：標準壁紙</option>';
  }
  return Array.from(source.options).map((option) => {
    const selected = option.value === selectedNumber ? " selected" : "";
    return `<option value="${escapeAttribute(option.value)}"${selected}>${escapeHtml(option.textContent)}</option>`;
  }).join("");
}

document.addEventListener("change", (event) => {
  const checkbox = event.target.closest(".room-exclude-checkbox");
  if (!checkbox) return;

  const row = checkbox.closest(".room-detail-group");
  if (row) row.classList.toggle("is-summary-excluded", checkbox.checked);

  if (checkbox.dataset.newRoomExclude !== undefined) {
    const hidden = checkbox.parentElement.querySelector('input[type="hidden"][name="new_room_excluded_from_summary"]');
    if (hidden) hidden.value = checkbox.checked ? "1" : "0";
  }
});

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-add-opening]");
  if (!button) return;
  const row = button.closest(".room-detail-group");
  if (!row) return;
  appendOpeningRows(row, button);
});

function appendOpeningRows(row, button) {
  const countInput = row.querySelector("[data-opening-count]");
  if (!countInput) return;

  const nextCount = Number(countInput.value || "0") + 1;
  countInput.value = String(nextCount);
  const prefix = countInput.name.replace(/_opening_count$/, "");
  const startRow = 2 + 3 + ((nextCount - 1) * 3);
  const fragment = document.createDocumentFragment();
  fragment.append(openingMeasureRow(prefix, nextCount, `開口部${nextCount}(m2)`, startRow, "area"));
  fragment.append(openingMeasureRow(prefix, nextCount, "幅(m)", startRow + 1, "width"));
  fragment.append(openingMeasureRow(prefix, nextCount, "高(m)", startRow + 2, "height"));
  row.insertBefore(fragment, button);

  const detailRows = 3 + (nextCount * 3);
  const note = row.querySelector(".room-note-cell");
  const exclude = row.querySelector(".room-exclude-cell");
  if (note) note.style.gridRow = `1 / span ${detailRows + 1}`;
  if (exclude) exclude.style.gridRow = String(detailRows + 2);
  button.style.gridRow = String(detailRows + 3);
  updateResultScrollbars();
}

function surfaceMeasureRow(label, gridRow, kind) {
  const suffix = kind === "area" ? "surface_area_m2" : `surface_${kind}_m`;
  const editable = kind !== "area";
  const cells = ["east", "west", "south", "north", "ceiling"].map((field) => {
    if (editable) {
      return `<span class="room-measure-cell" style="grid-row: ${gridRow};"><input name="new_room_${field}_${suffix}" type="number" step="0.001" min="0" value="0.000" data-measure-input></span>`;
    }
    return `<span class="room-measure-cell" style="grid-row: ${gridRow};">0.000</span>`;
  }).join("");
  return `<strong class="room-sub-label" style="grid-row: ${gridRow};">${label}</strong>${cells}<span class="room-total-spacer" style="grid-row: ${gridRow};"></span>`;
}

function openingMeasureRow(prefix, sequence, label, gridRow, kind) {
  const wrapper = document.createElement("template");
  const suffix = kind === "area" ? "area_m2" : `${kind}_m`;
  const editable = kind !== "area";
  const cells = ["east", "west", "south", "north", "ceiling"].map((field) => {
    if (field === "ceiling") {
      return `<span class="room-measure-cell is-blank" style="grid-row: ${gridRow};"></span>`;
    }
    if (editable) {
      return `<span class="room-measure-cell" style="grid-row: ${gridRow};"><input name="${prefix}_${field}_opening_${sequence}_${suffix}" type="number" step="0.001" min="0" value="0.000" data-measure-input></span>`;
    }
    return `<span class="room-measure-cell" style="grid-row: ${gridRow};">0.000</span>`;
  }).join("");
  wrapper.innerHTML = `<strong class="room-sub-label" style="grid-row: ${gridRow};">${label}</strong>${cells}<span class="room-total-spacer" style="grid-row: ${gridRow};"></span>`;
  return wrapper.content;
}
