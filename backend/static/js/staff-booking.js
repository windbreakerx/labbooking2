// Ручная запись: выбор студента из поиска → подгрузка ЛР из плана его группы.
(function () {
  "use strict";
  document.addEventListener("click", function (evt) {
    var option = evt.target.closest(".student-option");
    if (!option) {
      return;
    }
    document.querySelectorAll(".student-option--selected").forEach(function (node) {
      node.classList.remove("student-option--selected");
    });
    option.classList.add("student-option--selected");
    document.getElementById("student-id").value = option.dataset.id;
    var label = document.getElementById("selected-student");
    label.textContent = "Выбран: " + option.dataset.name + " (" + option.dataset.email + ")";
    label.hidden = false;
    document.getElementById("slots").innerHTML = "";
    var labWorks = document.getElementById("lab-works");
    htmx.ajax(
      "GET",
      labWorks.dataset.url + "?student_id=" + encodeURIComponent(option.dataset.id),
      { target: "#lab-works", swap: "innerHTML" }
    );
  });
})();
