from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect, render
from django.views.generic import View

from .forms import AuthForm, RegistrationForm


def _first_error_message(form):
    """Сообщение об ошибках формы: не-полевые ошибки + все полевые;
    не падает с KeyError, если глобальных ошибок нет."""
    if "__all__" in form.errors:
        return "; ".join(msg for msg in form.errors["__all__"])
    all_messages = [msg for msgs in form.errors.values() for msg in msgs]
    return "; ".join(all_messages) or "Введенные данные некорректны."


class AuthView(View):
    """Авторизация"""

    @staticmethod
    def get(request):
        return render(request, "user/auth.html", {"form": AuthForm})

    @staticmethod
    def post(request):
        form = AuthForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                "user/auth.html",
                {"form": form, "error": _first_error_message(form)},
            )
        # Согласовано с формой: те же данные, что проверены формой
        # (strip=False — пароль посимвольно, как введён).
        email = form.cleaned_data["email"]
        password = form.cleaned_data["password"]
        user = authenticate(request, username=email.lower(), password=password)

        if user is not None:
            login(request, user)
            return redirect("dashboard:index")
        else:
            return render(
                request,
                "user/auth.html",
                {
                    "form": form,
                    "error": "Неверный логин или пароль! Повторите попытку.",
                },
            )


class RegistrationView(View):
    """Регистрация"""

    @staticmethod
    def get(request):
        return render(
            request, "user/registration.html", {"form": RegistrationForm}
        )

    @staticmethod
    def post(request):
        form = RegistrationForm(request.POST)
        if form.is_valid():
            try:
                user = form.save()
            except Exception:
                form.add_error(
                    None, "Не удалось зарегистрировать пользователя."
                )
                user = None
            if user is not None:
                login(request, user)
                return redirect("dashboard:index")
        return render(
            request,
            "user/registration.html",
            {"form": form, "error": _first_error_message(form)},
        )


class LogoutView(View):
    """Выход — изменяет сессию, поэтому только POST (безопасный GET не
    должен завершать сессию; CSRF-защита формы обязательна)."""

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        logout(request)
        return redirect("dashboard:index")
