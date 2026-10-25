from django import forms
from django.contrib.auth import authenticate, password_validation
from django.core.exceptions import ValidationError

from user.models import User


class RegistrationForm(forms.Form):
    """Форма регистрации пользователей"""

    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(
            attrs={"class": "form-control my-3 p-4", "placeholder": "Email"}
        ),
    )
    # strip=False: пароль хранится и проверяется посимвольно, включая
    # ведущие/замыкающие пробелы — иначе круглый вход/регистрация расходятся.
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
            attrs={
                "class": "form-control my-3 p-4",
                "placeholder": "Повторите пароль",
            }
        ),
    )

    def clean(self):
        super().clean()
        cleaned_data = self.cleaned_data
        email = cleaned_data.get("email")
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")

        if password1 and password2 and password1 != password2:
            errors = ["Введенные пароли не совпадают"]
            raise ValidationError(errors)

        if email and User.objects.filter(email__iexact=email).exists():
            errors = [
                "Данный e-mail уже зарегистрирован! Введите другой e-mail или"
                " воспользуйтесь формой авторизации."
            ]
            raise ValidationError(errors)

        # Системные валидаторы пароля применяются к кандидатному пользователю
        # с нормализованными email/username (сохраняемого пользователя ещё нет):
        # UserAttributeSimilarityValidator сравнивает пароль с этими полями.
        if password1 and password1 == password2:
            normalized_email = (email or "").lower()
            candidate = User(
                username=normalized_email,
                email=normalized_email,
                first_name="",
                last_name="",
            )
            try:
                password_validation.validate_password(
                    password1, user=candidate
                )
            except ValidationError as error:
                # Ошибки валидаторов прикрепляются к полю повтора пароля,
                # чтобы были видны непосредственно у формы.
                self.add_error("password2", error.messages)
        return cleaned_data

    def save(self):
        # username хранится в нижнем регистре — иначе вход по email, введённому
        # в другом регистре, невозможен (authenticate приводит к нижнему регистру).
        email = self.cleaned_data["email"].lower()
        password = self.cleaned_data["password1"]
        User.objects.create_user(email, email, password)
        user = authenticate(username=email, password=password)
        return user


class AuthForm(forms.Form):
    """Форма авторизации пользователей"""

    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(
            attrs={"class": "form-control my-3 p-4", "placeholder": "Email"}
        ),
    )
    # strip=False согласован с RegistrationForm: пароль сравнивается как есть.
    password = forms.CharField(
        required=True,
        strip=False,
        widget=forms.PasswordInput(
            attrs={"class": "form-control my-3 p-4", "placeholder": "Пароль"}
        ),
    )
