from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from user.forms import RegistrationForm

UserModel = get_user_model()
GLOSSARY = "user@example.com"
GLOSSARY_PASSWORD = "Good?Password#42"


class RegistrationViewTests(TestCase):
    def test_registration_success_lowercase_username(self):
        response = self.client.post(
            reverse("user:registration"),
            {
                "email": "User@Example.COM",
                "password1": GLOSSARY_PASSWORD,
                "password2": GLOSSARY_PASSWORD,
            },
        )
        self.assertRedirects(
            response, reverse("dashboard:index"), fetch_redirect_response=False
        )
        # username нормализуется к нижнему регистру: вход чувствителен к нему
        user = UserModel.objects.get(username="user@example.com")
        self.assertEqual(user.username, "user@example.com")

    def test_registration_password_mismatch_message(self):
        response = self.client.post(
            reverse("user:registration"),
            {
                "email": GLOSSARY,
                "password1": GLOSSARY_PASSWORD,
                "password2": GLOSSARY_PASSWORD + "!",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Введенные пароли не совпадают", response.content.decode()
        )

    def test_registration_weak_password_rejected(self):
        response = self.client.post(
            reverse("user:registration"),
            {"email": GLOSSARY, "password1": "1", "password2": "1"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("too short", body)
        self.assertIn("too common", body)
        self.assertFalse(UserModel.objects.filter(email=GLOSSARY).exists())

    def test_registration_duplicate_email_case_insensitive(self):
        UserModel.objects.create_user(GLOSSARY, GLOSSARY, "Other?Password#7")
        response = self.client.post(
            reverse("user:registration"),
            {
                "email": "USER@example.com",
                "password1": GLOSSARY_PASSWORD,
                "password2": GLOSSARY_PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Данный e-mail уже зарегистрирован", response.content.decode()
        )

    def test_registration_invalid_email_shows_error_not_500(self):
        response = self.client.post(
            reverse("user:registration"),
            {
                "email": "not-an-email",
                "password1": GLOSSARY_PASSWORD,
                "password2": GLOSSARY_PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Enter a valid email address.", response.content.decode()
        )

    def test_registration_empty_shows_error_not_500(self):
        response = self.client.post(reverse("user:registration"), {})
        self.assertEqual(response.status_code, 200)
        self.assertIn("This field is required.", response.content.decode())


class AuthViewTests(TestCase):
    def setUp(self):
        self.user = UserModel.objects.create_user(
            GLOSSARY, GLOSSARY, GLOSSARY_PASSWORD
        )

    def test_login_ok(self):
        response = self.client.post(
            reverse("user:auth"),
            {"email": GLOSSARY, "password": GLOSSARY_PASSWORD},
        )
        self.assertRedirects(
            response, reverse("dashboard:index"), fetch_redirect_response=False
        )

    def test_login_case_insensitive_email(self):
        response = self.client.post(
            reverse("user:auth"),
            {"email": "USER@EXAMPLE.COM", "password": GLOSSARY_PASSWORD},
        )
        self.assertRedirects(
            response, reverse("dashboard:index"), fetch_redirect_response=False
        )

    def test_login_wrong_password_message(self):
        response = self.client.post(
            reverse("user:auth"),
            {"email": GLOSSARY, "password": "nope"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Неверный логин или пароль", response.content.decode())

    def test_login_invalid_email_shows_error_not_500(self):
        response = self.client.post(
            reverse("user:auth"),
            {"email": "broken@", "password": "nope"},
        )
        self.assertEqual(response.status_code, 200)

    def test_logout_via_post_redirects_and_ends_session(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("user:logout"))
        self.assertRedirects(
            response, reverse("dashboard:index"), fetch_redirect_response=False
        )
        # сессия завершена: пользователь больше не аутентифицирован
        response = self.client.get(reverse("dashboard:index"))
        self.assertNotContains(response, "Привет,")

    def test_logout_get_rejected_and_session_preserved(self):
        # изменяющей сессии выход по небезопасному GET делать нельзя
        self.client.force_login(self.user)
        response = self.client.get(reverse("user:logout"))
        self.assertEqual(response.status_code, 405)
        # сессия сохранена
        self.assertIn("_auth_user_id", self.client.session)


class PasswordSpacingTests(TestCase):
    """Пароли сохраняются и проверяются посимвольно (strip=False)."""

    SPACED = "  spaced?Pass#42  "

    def test_registration_stores_password_exact(self):
        self.client.post(
            reverse("user:registration"),
            {
                "email": "spaced@example.com",
                "password1": self.SPACED,
                "password2": self.SPACED,
            },
        )
        user = UserModel.objects.get(email="spaced@example.com")
        self.assertTrue(user.check_password(self.SPACED))
        self.assertFalse(user.check_password(self.SPACED.strip()))

    def test_login_with_exact_spaced_password(self):
        UserModel.objects.create_user(
            "spaced@example.com", "spaced@example.com", self.SPACED
        )
        response = self.client.post(
            reverse("user:auth"),
            {"email": "spaced@example.com", "password": self.SPACED},
        )
        self.assertRedirects(
            response, reverse("dashboard:index"), fetch_redirect_response=False
        )

    def test_login_with_trimmed_password_fails(self):
        # обрезанный пароль не должен подходить к сохранённому с пробелами
        UserModel.objects.create_user(
            "spaced@example.com", "spaced@example.com", self.SPACED
        )
        response = self.client.post(
            reverse("user:auth"),
            {"email": "spaced@example.com", "password": self.SPACED.strip()},
        )
        self.assertEqual(response.status_code, 200)


class PasswordSimilarityTests(TestCase):
    """UserAttributeSimilarityValidator работает с кандидатным пользователем."""

    def test_too_similar_to_email_rejected(self):
        form = RegistrationForm(
            data={
                "email": "maria.ivanova@example.com",
                "password1": "maria.ivanova",
                "password2": "maria.ivanova",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertTrue(form.errors["password2"])
