import re
import datetime
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from project.conf import app_settings
from .models import AuthOtp, User
from .choices import AuthOtpTypeEnum, LoginTypeEnum


class AuthOtpSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuthOtp
        fields = "__all__"


class AuthOtpSendSMSSerializer(serializers.ModelSerializer):
    auth_type = serializers.ChoiceField(
        choices=AuthOtpTypeEnum.choices(),
        default=AuthOtpTypeEnum.EMAIL,
        required=False
    )

    class Meta:
        model = AuthOtp
        fields = ["number", "auth_type"]

    def validate_number(self, value):
        regex = re.match(r'^(010|070)-\d{3,4}-\d{4}$', value)
        if not regex:
            raise ValidationError(detail={"detail": "invalid_number", "number_format": "010-0000-0000"})
        return regex.string

    def save(self, **kwargs):
        auth_otp = self.Meta.model.objects.create(**self.validated_data)
        self.instance = auth_otp
        return auth_otp

    def to_representation(self, instance: AuthOtp):
        expired_dt = self.instance.timestamp + datetime.timedelta(0, self.instance.otp_interval)
        data = super().to_representation(self.instance)
        data.pop('auth_type')
        data.update({
            'otp_code': self.instance.otp_code,
            'expired_at': expired_dt.strftime('%Y-%m-%d %H:%M:%S')
        })
        return data


class AuthOtpVerifyCodeSerializer(serializers.ModelSerializer):
    otp_code = serializers.CharField(max_length=128)
    verified_at = serializers.DateTimeField(required=False)
    auth_type = serializers.ChoiceField(
        choices=AuthOtpTypeEnum.choices(),
        default=AuthOtpTypeEnum.EMAIL,
        required=False
    )

    class Meta:
        model = AuthOtp
        fields = ["number", "otp_code", "verified_at", "auth_type"]

    def validate_auth_type(self, value):
        default_auth_type = self.get_fields().get('auth_type').default
        auth_type = value or default_auth_type
        try:
            assert self.instance.auth_type == auth_type
        except AssertionError:
            raise ValidationError(detail={"detail": "invalid_auth_type", "auth_type": dict(AuthOtpTypeEnum.choices())})

    def validate(self, attrs):
        if not self.instance:
            raise ValidationError(detail={"detail": "invalid_number"})
        return attrs

    def save(self, **kwargs):
        otp_code = self.validated_data.pop("otp_code")
        verified = self.instance.authenticate_code_by_otp_key(otp_code)
        if not verified:
            raise ValidationError(detail={"detail": "invalid_code"})
        self.validated_data.update({"otp_register_code": otp_code})
        if self.instance is not None:
            self.instance = self.update(self.instance, self.validated_data)
            self.verified_at = datetime.datetime.now()
        else:
            raise ValidationError(detail={"detail": "invalid_number"})

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data.pop('otp_code')
        data.pop('auth_type')
        data.update({'verified_at': self.verified_at.strftime('%Y-%m-%d %H:%M:%S')})
        return data


class SignupSerializer(serializers.ModelSerializer):
    auth_otp = AuthOtpSerializer(required=False)

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "username",
            "nickname",
            "password",
            "phone_number",
            "otp_register_code",
            "auth_otp"
        ]

    def validate(self, attrs):
        number = attrs["phone_number"]
        try:
            auth_otp = AuthOtp.objects.filter(number=number).latest()
            assert str(auth_otp.otp_register_code) == str(attrs["otp_register_code"])
            assert auth_otp.authenticated is False
            self.auth_otp = auth_otp
        except (AuthOtp.DoesNotExist, AssertionError) as e:
            raise ValidationError(detail={"detail": "invalid_auth_otp_data"})
        return attrs

    def save(self, **kwargs):
        with transaction.atomic():
            user = self.Meta.model.objects.create_authenticated_user_from_request(**self.validated_data)
            self.auth_otp.authenticated = True
            self.auth_otp.save()
            return user

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data.pop("otp_register_code")
        return data


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email", "username", "nickname", "phone_number", "is_staff", "last_login"]


class LoginSerializer(TokenObtainPairSerializer):
    email = serializers.EmailField(max_length=255)
    phone_number = serializers.CharField(max_length=17)
    password = serializers.CharField(required=True)
    login_type = serializers.ChoiceField(
        choices=LoginTypeEnum.choices(),
        default=LoginTypeEnum.EMAIL,
        required=False
    )

    def validate(self, attrs):
        default_login_type = self.get_fields().get('login_type').default
        login_type = attrs.get("login_type", default_login_type)
        filter_kwargs = {login_type: attrs[login_type]}
        try:
            user = User.objects.get(Q(**filter_kwargs))
            if not user.check_password(attrs["password"]):
                raise ValidationError(detail={"detail": "wrong_password"})
        except User.DoesNotExist:
            raise ValidationError(detail={"detail": "no_exist_user"})

        data = super().validate(attrs)
        refresh = self.get_token(self.user)
        data.update({
            "user": UserSerializer(user).data,
            "access": str(refresh.access_token),
            "refresh": str(refresh)
        })

        if app_settings.SIMPLE_JWT_UPDATE_LOGIN_SETTING:
            user.last_login_datetime = timezone.now()
            user.last_login_type = default_login_type
            user.save(update_fields=["last_login_datetime", "last_login_type"])

        return data

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data.pop("login_type")
        return data


class PasswordSerializer(serializers.Serializer):
    number = serializers.CharField(required=True)
    otp_code = serializers.CharField(max_length=6, required=True)
    new_passwd = serializers.CharField(required=True)
    user = UserSerializer(required=False)

    def validate_number(self, value):
        regex = re.match(r'^(010|070)-\d{3,4}-\d{4}$', value)
        if not regex:
            raise ValidationError(detail={"detail": "invalid_number", "number_format": "010-0000-0000"})
        return regex.string

    def validate(self, attrs):
        number = attrs["number"]
        try:
            auth_otp = AuthOtp.objects.filter(number=number).latest()
            assert str(auth_otp.otp_register_code) == str(attrs["otp_register_code"])
            assert auth_otp.authenticated is False
        except (AuthOtp.DoesNotExist, AssertionError) as e:
            raise ValidationError(detail={"detail": "invalid_auth_otp_data"})

        try:
            user = User.objects.get(phone_number=number)
            self.user = user
        except User.DoesNotExist:
            raise ValidationError(detail={"detail": "invalid_phone_number"})
        return attrs

    def save(self, **kwargs):
        self.user.set_password(self.validated_data["new_passwd"])
        self.user.save()

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data.pop("otp_code")
        return data
