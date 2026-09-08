"""Форма входа по email (сессионная авторизация, задел под SSO §8 плана)."""

from django.contrib.auth.forms import AuthenticationForm


class EmailAuthenticationForm(AuthenticationForm):
    error_messages = {
        "invalid_login": "Неверный email или пароль.",
        "inactive": "Аккаунт отключён.",
    }

    def __init__(self, request=None, *args, **kwargs):
        super().__init__(request, *args, **kwargs)
        self.fields["username"].label = "Email"
        self.fields["username"].widget.attrs.update(
            {"type": "email", "autofocus": True, "autocomplete": "email"}
        )

    def clean_username(self):
        return (self.cleaned_data.get("username") or "").strip().lower()
