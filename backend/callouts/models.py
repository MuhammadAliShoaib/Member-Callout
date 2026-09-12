import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models


class MemberManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Members must have an email address')

        email = self.normalize_email(email)
        member = self.model(email=email, **extra_fields)
        member.set_password(password)
        member.save(using=self._db)
        return member

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True')

        return self.create_user(email, password, **extra_fields)


class Local(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Member(AbstractBaseUser, PermissionsMixin):
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        RETIRED = 'retired', 'Retired'
        SUSPENDED = 'suspended', 'Suspended'

    class Role(models.TextChoices):
        MEMBER = 'member', 'Member'
        LEADER = 'leader', 'Leader'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    local = models.ForeignKey(Local, on_delete=models.PROTECT, related_name='members')
    full_name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    classification = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    objects = MemberManager()

    EMAIL_FIELD = 'email'
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['full_name', 'local', 'classification']

    def clean(self):
        super().clean()
        self.email = type(self).objects.normalize_email(self.email)

    def __str__(self):
        return self.email
