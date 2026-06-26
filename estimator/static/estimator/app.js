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
    <span class="room-total-cell">0.00 m2<br>0 本</span>
    <span class="room-note-cell">手動追加: 面積、開口部を入力してください</span>
    <label class="room-exclude-cell">
      <input type="hidden" name="new_room_excluded_from_summary" value="0">
      <input class="room-exclude-checkbox" type="checkbox" value="1" data-new-room-exclude>
      <span>集計対象外</span>
    </label>
    <strong class="room-sub-label room-area-label">面積(m2)</strong>
    <span class="room-measure-cell room-area-measure"><input name="new_room_east_surface_area_m2" type="number" step="0.01" min="0" value="0.00"></span>
    <span class="room-measure-cell room-area-measure"><input name="new_room_west_surface_area_m2" type="number" step="0.01" min="0" value="0.00"></span>
    <span class="room-measure-cell room-area-measure"><input name="new_room_south_surface_area_m2" type="number" step="0.01" min="0" value="0.00"></span>
    <span class="room-measure-cell room-area-measure"><input name="new_room_north_surface_area_m2" type="number" step="0.01" min="0" value="0.00"></span>
    <span class="room-measure-cell room-area-measure"><input name="new_room_ceiling_surface_area_m2" type="number" step="0.01" min="0" value="0.00"></span>
    <span class="room-total-spacer room-area-spacer"></span>
    <strong class="room-sub-label room-opening-label">開口部(m2)</strong>
    <span class="room-measure-cell"><input name="new_room_east_opening_area_m2" type="number" step="0.01" min="0" value="0.00"></span>
    <span class="room-measure-cell"><input name="new_room_west_opening_area_m2" type="number" step="0.01" min="0" value="0.00"></span>
    <span class="room-measure-cell"><input name="new_room_south_opening_area_m2" type="number" step="0.01" min="0" value="0.00"></span>
    <span class="room-measure-cell"><input name="new_room_north_opening_area_m2" type="number" step="0.01" min="0" value="0.00"></span>
    <span class="room-measure-cell"></span>
    <span class="room-total-spacer room-opening-spacer"></span>
  `;
  roomDetailBody.append(row);
  const firstInput = row.querySelector('input[name="new_room_name"]');
  if (firstInput) firstInput.focus();
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
