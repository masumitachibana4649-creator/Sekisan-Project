const addRoomButton = document.querySelector("[data-add-room]");
const roomTable = document.querySelector("[data-room-table]");
const template = document.querySelector("#room-row-template");
const estimateForm = document.querySelector("[data-estimate-form]");
const loadingOverlay = document.querySelector("[data-loading-overlay]");

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
