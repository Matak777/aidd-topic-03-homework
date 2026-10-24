from django import forms
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from user.models import User


class RegistrationForm(forms.Form):
    """Форма регистрации пользователей."""

    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(
            attrs={"class": "form-control my-3 p-4", "placeholder": "Email"}
        ),
    )
    password1 = forms.CharField(
        required=True,
        strip=False,
        widget=forms.PasswordInput(
            attrs={"class": "form-control my-3 p-4", "placeholder": "Пароль"}
        ),
    )
    password2 = forms.CharField(
        required=True,
        strip=False,
        widget=forms.PasswordInput(
            attrs={"class": "form-control my-3 p-4", "placeholder": "Повторите пароль"}
        ),
    )

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError(
                "Данный e-mail уже зарегистрирован! Введите другой e-mail или "
                "воспользуйтесь формой авторизации."
            )
        return email

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")

        if password1 and password2 and password1 != password2:
            self.add_error("password2", "Введенные пароли не совпадают")
        elif password1:
            try:
                validate_password(password1)
            except ValidationError as error:
                self.add_error("password1", error)

        return cleaned_data

    def save(self):
        if not self.is_valid():
            raise ValueError("Registration form must be valid before saving.")

        email = self.cleaned_data["email"]
        password = self.cleaned_data["password1"]
        User.objects.create_user(username=email, email=email, password=password)
        user = authenticate(username=email, password=password)
        if user is None:
            raise ValidationError("Не удалось выполнить вход после регистрации.")
        return user


class AuthForm(forms.Form):
    """Форма авторизации пользователей."""

    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(
            attrs={"class": "form-control my-3 p-4", "placeholder": "Email"}
        ),
    )
    password = forms.CharField(
        required=True,
        strip=False,
        widget=forms.PasswordInput(
            attrs={"class": "form-control my-3 p-4", "placeholder": "Пароль"}
        ),
    )
