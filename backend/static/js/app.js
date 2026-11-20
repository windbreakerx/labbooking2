// Общее поведение страниц: подтверждение разрушительных форм и авто-отправка.
(function () {
  "use strict";

  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    if (!form || !form.hasAttribute || !form.hasAttribute("data-confirm")) {
      return;
    }
    if (!window.confirm(form.getAttribute("data-confirm"))) {
      evt.preventDefault();
    }
  });

  document.addEventListener("change", function (evt) {
    var control = evt.target;
    if (!control || !control.hasAttribute || !control.hasAttribute("data-auto-submit")) {
      return;
    }
    if (control.form) {
      control.form.requestSubmit();
    }
  });
})();
