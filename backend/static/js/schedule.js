// Редактор расписания: показ полей группового режима.
(function () {
  "use strict";

  function syncMode(select) {
    var target = document.getElementById(select.dataset.target);
    if (!target) {
      return;
    }
    target.classList.toggle("mode-group--active", select.value === "GROUP");
  }

  document.querySelectorAll(".mode-select").forEach(function (select) {
    select.addEventListener("change", function () {
      syncMode(select);
    });
    syncMode(select);
  });
})();
