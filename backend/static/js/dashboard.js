// Дашборд завлаба: графики Chart.js на данных из json_script.
// Цвета берутся из CSS-токенов и обновляются при смене темы.
(function () {
  "use strict";

  function parseData(id) {
    var node = document.getElementById(id);
    return node ? JSON.parse(node.textContent) : null;
  }

  function cssVar(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function palette() {
    return {
      text: cssVar("--color-muted", "#5b6b72"),
      grid: cssVar("--color-border", "#d7dee3"),
      info: cssVar("--color-info", "#1c5d99"),
      success: cssVar("--color-success", "#23703c"),
      warning: cssVar("--color-warning", "#8a5a00"),
      muted: cssVar("--color-muted", "#5b6b72"),
      accent: cssVar("--color-accent", "#8f5a16"),
    };
  }

  function barOptions(colors) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: {
          ticks: { color: colors.text },
          grid: { color: colors.grid },
        },
        y: {
          beginAtZero: true,
          max: 100,
          ticks: { color: colors.text },
          grid: { color: colors.grid },
        },
      },
    };
  }

  if (typeof Chart === "undefined") {
    return;
  }

  var roomLabels = parseData("room-labels");
  var roomValues = parseData("room-values");
  var curriculumLabels = parseData("curriculum-labels");
  var curriculumValues = parseData("curriculum-values");
  var kpi = parseData("kpi-data");

  var charts = [];
  var colors = palette();

  var roomChart = document.getElementById("roomChart");
  if (roomChart) {
    charts.push({
      kind: "room",
      chart: new Chart(roomChart, {
        type: "bar",
        data: {
          labels: roomLabels,
          datasets: [
            {
              label: "Заполняемость мест, %",
              data: roomValues,
              backgroundColor: colors.accent,
              borderColor: colors.accent,
              borderWidth: 1,
            },
          ],
        },
        options: barOptions(colors),
      }),
    });
  }

  var curriculumChart = document.getElementById("curriculumChart");
  if (curriculumChart) {
    charts.push({
      kind: "curriculum",
      chart: new Chart(curriculumChart, {
        type: "bar",
        data: {
          labels: curriculumLabels,
          datasets: [
            {
              label: "Выполнение плана, %",
              data: curriculumValues,
              backgroundColor: colors.success,
              borderColor: colors.success,
              borderWidth: 1,
            },
          ],
        },
        options: barOptions(colors),
      }),
    });
  }

  var statusChart = document.getElementById("statusChart");
  if (statusChart && kpi) {
    charts.push({
      kind: "status",
      chart: new Chart(statusChart, {
        type: "doughnut",
        data: {
          labels: ["Посетил", "Записан", "Неявка", "Отмены записей", "Отменённые слоты"],
          datasets: [
            {
              data: [kpi.visited, kpi.booked, kpi.no_show, kpi.cancelled, kpi.slot_cancelled],
              backgroundColor: [
                colors.success,
                colors.info,
                colors.warning,
                colors.muted,
                colors.accent,
              ],
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
        },
      }),
    });
  }

  function updateCharts(next) {
    charts.forEach(function (entry) {
      var dataset = entry.chart.data.datasets[0];
      if (entry.kind === "status") {
        dataset.backgroundColor = [
          next.success,
          next.info,
          next.warning,
          next.muted,
          next.accent,
        ];
        entry.chart.options = {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
        };
      } else {
        var color = entry.kind === "room" ? next.accent : next.success;
        dataset.backgroundColor = color;
        dataset.borderColor = color;
        entry.chart.options = barOptions(next);
      }
      entry.chart.update();
    });
  }

  document.addEventListener("labbooking:themechange", function () {
    colors = palette();
    updateCharts(colors);
  });
})();
