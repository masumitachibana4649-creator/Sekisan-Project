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
    appendHiddenRoomField("new_room_floor", floor);
    appendHiddenRoomField("new_room_name", roomName);
    appendManualRoomPreview(floor, roomName);
    addRoomDialog.close();
  });
}

function appendHiddenRoomField(name, value) {
  const input = document.createElement("input");
  input.type = "hidden";
  input.name = name;
  input.value = value;
  newRoomFields.append(input);
}

function appendManualRoomPreview(floor, roomName) {
  const row = document.createElement("div");
  row.className = "room-detail-group";
  const rowNumber = roomDetailBody.querySelectorAll(".room-detail-group").length + 1;
  row.innerHTML = `
    <strong class="room-no-cell">${rowNumber}</strong>
    <span class="room-floor-cell">${escapeHtml(floor)}</span>
    <strong class="room-name-cell source-manual-room">${escapeHtml(roomName)}</strong>
    <span class="room-wallpaper-cell">001：標準壁紙</span>
    <span class="room-wallpaper-cell">001：標準壁紙</span>
    <span class="room-wallpaper-cell">001：標準壁紙</span>
    <span class="room-wallpaper-cell">001：標準壁紙</span>
    <span class="room-wallpaper-cell">001：標準壁紙</span>
    <span class="room-total-cell">0.00 m2<br>0 本</span>
    <span class="room-note-cell">手動追加: 面積、開口部を入力してください</span>
    <label class="room-exclude-cell"><input type="checkbox" disabled><span>集計対象外</span></label>
    <strong class="room-sub-label">面積(m2)</strong>
    <span class="room-measure-cell">0.00</span>
    <span class="room-measure-cell">0.00</span>
    <span class="room-measure-cell">0.00</span>
    <span class="room-measure-cell">0.00</span>
    <span class="room-measure-cell">0.00</span>
    <span class="room-total-spacer"></span>
    <strong class="room-sub-label">開口部(m2)</strong>
    <span class="room-measure-cell">0.00</span>
    <span class="room-measure-cell">0.00</span>
    <span class="room-measure-cell">0.00</span>
    <span class="room-measure-cell">0.00</span>
    <span class="room-measure-cell"></span>
    <span class="room-total-spacer"></span>
  `;
  roomDetailBody.append(row);
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
