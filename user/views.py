from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect, render
from django.views import View

from .forms import AuthForm, RegistrationForm


class AuthView(View):
    """Авторизация."""

    def get(self, request):
        return render(request, "user/auth.html", {"form": AuthForm()})

    def post(self, request):
        form = AuthForm(request.POST)
        if not form.is_valid():
            return render(request, "user/auth.html", {"form": form})

        email = form.cleaned_data["email"].strip().lower()
        password = form.cleaned_data["password"]
        user = authenticate(request, username=email, password=password)
        if user is None:
            form.add_error(None, "Неверный логин или пароль! Повторите попытку.")
            return render(request, "user/auth.html", {"form": form})

        login(request, user)
        return redirect("dashboard:index")


class RegistrationView(View):
    """Регистрация."""

    def get(self, request):
        return render(request, "user/registration.html", {"form": RegistrationForm()})

    def post(self, request):
        form = RegistrationForm(request.POST)
        if not form.is_valid():
            return render(request, "user/registration.html", {"form": form})

        user = form.save()
        login(request, user)
        return redirect("dashboard:index")


class LogoutView(View):
    """Выход."""

    def post(self, request):
        logout(request)
        return redirect("dashboard:index")
