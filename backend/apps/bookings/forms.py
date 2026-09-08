"""Формы web-слоя обращений (создание тикета студентом)."""

from django import forms

from apps.academics.scope import student_support_training_centers_qs


class TicketCreateForm(forms.Form):
    subject = forms.CharField(label="Тема", max_length=256)
    body = forms.CharField(label="Сообщение", widget=forms.Textarea(attrs={"rows": 4}))
    training_center = forms.ModelChoiceField(label="Учебный центр", queryset=None)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["training_center"].queryset = student_support_training_centers_qs(user)
