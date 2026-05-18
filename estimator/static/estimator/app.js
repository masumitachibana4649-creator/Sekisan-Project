const addRoomButton = document.querySelector("[data-add-room]");
const roomTable = document.querySelector("[data-room-table]");
const template = document.querySelector("#room-row-template");

if (addRoomButton && roomTable && template) {
  addRoomButton.addEventListener("click", () => {
    roomTable.append(template.content.cloneNode(true));
    const lastRow = roomTable.querySelector(".room-line:last-child input");
    if (lastRow) lastRow.focus();
  });
}
