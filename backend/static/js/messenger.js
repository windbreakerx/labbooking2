// Мессенджер обращений: авто-скролл ленты, Enter-отправка, мобильные панели.
// Бизнес-логика на сервере — здесь только поведение интерфейса.
(function () {
  "use strict";

  var bottomStates = new WeakMap();

  document.body.addEventListener("htmx:beforeSwap", function () {
    var feed = document.getElementById("chat-messages");
    if (feed) {
      bottomStates.set(feed, feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80);
    }
  });

  document.body.addEventListener("htmx:afterSwap", function (evt) {
    var pane = document.getElementById("chat-pane");
    if (pane && evt.target && pane.contains(evt.target)) {
      var messenger = document.getElementById("messenger");
      if (messenger) {
        messenger.classList.add("messenger--chat-open");
      }
    }
    var feed = document.getElementById("chat-messages");
    if (feed && bottomStates.get(feed)) {
      feed.scrollTop = feed.scrollHeight;
    }
  });

  document.body.addEventListener("keydown", function (evt) {
    if (evt.key !== "Enter" || evt.shiftKey) {
      return;
    }
    var target = evt.target;
    if (target && target.hasAttribute && target.hasAttribute("data-enter-submit")) {
      evt.preventDefault();
      target.form.requestSubmit();
    }
  });

  document.body.addEventListener("click", function (evt) {
    var back = evt.target.closest("[data-chat-back]");
    if (!back) {
      return;
    }
    var messenger = document.getElementById("messenger");
    if (messenger) {
      messenger.classList.remove("messenger--chat-open");
    }
  });
})();
