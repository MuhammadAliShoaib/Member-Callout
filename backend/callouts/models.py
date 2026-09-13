import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
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


class Announcement(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        CONFIRMED = 'confirmed', 'Confirmed'
        QUEUED = 'queued', 'Queued'
        SENT = 'sent', 'Sent'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    local = models.ForeignKey(Local, on_delete=models.PROTECT, related_name='announcements')
    created_by = models.ForeignKey(Member, on_delete=models.PROTECT, related_name='announcements')
    title = models.CharField(max_length=255)
    body = models.TextField()
    push_preview = models.CharField(max_length=255)
    target_classification = models.CharField(max_length=255, blank=True, null=True)
    needs_ack = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    confirmed_content_hash = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(blank=True, null=True)
    queued_at = models.DateTimeField(blank=True, null=True)
    sent_at = models.DateTimeField(blank=True, null=True)

    def content_fields_changed(self, previous):
        return any(
            getattr(self, field) != getattr(previous, field)
            for field in ('title', 'body', 'push_preview')
        )

    def reset_confirmation(self):
        self.confirmed_content_hash = ''
        self.confirmed_at = None
        self.status = self.Status.DRAFT

    def save(self, *args, **kwargs):
        if self.pk and self.status != self.Status.DRAFT:
            previous = type(self).objects.filter(pk=self.pk).only(
                'title',
                'body',
                'push_preview',
            ).first()
            if previous and self.content_fields_changed(previous):
                self.reset_confirmation()
                update_fields = kwargs.get('update_fields')
                if update_fields is not None:
                    kwargs['update_fields'] = set(update_fields) | {
                        'confirmed_content_hash',
                        'confirmed_at',
                        'status',
                    }

        return super().save(*args, **kwargs)

    def __str__(self):
        return self.title


class AnnouncementRecipient(models.Model):
    class DeliveryStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        SENT = 'sent', 'Sent'
        FAILED = 'failed', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    local = models.ForeignKey(Local, on_delete=models.PROTECT, related_name='announcement_recipients')
    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name='recipients')
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name='announcement_recipients')
    classification_snapshot = models.CharField(max_length=255)
    delivery_status = models.CharField(
        max_length=20,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
    )
    attempt_count = models.IntegerField(default=0)
    last_error = models.TextField(blank=True, null=True)
    claimed_at = models.DateTimeField(blank=True, null=True)
    claimed_by = models.CharField(max_length=255, blank=True, null=True)
    sent_at = models.DateTimeField(blank=True, null=True)
    read_at = models.DateTimeField(blank=True, null=True)
    acknowledged_at = models.DateTimeField(blank=True, null=True)
    rsvp = models.CharField(max_length=255, blank=True, null=True)
    rsvp_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(acknowledged_at__isnull=True) | models.Q(read_at__isnull=False),
                name='acknowledged_requires_read',
            ),
            models.UniqueConstraint(
                fields=['announcement', 'member'],
                name='unique_announcement_member',
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        local_errors = []

        if self.local_id and self.announcement_id and self.local_id != self.announcement.local_id:
            local_errors.append('Local must match the announcement local.')

        if self.local_id and self.member_id and self.local_id != self.member.local_id:
            local_errors.append('Local must match the member local.')

        if self.acknowledged_at and not self.read_at:
            errors['acknowledged_at'] = 'Acknowledged time requires read time.'

        if local_errors:
            errors['local'] = local_errors

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.announcement} - {self.member}'


class AnnouncementStats(models.Model):
    announcement = models.OneToOneField(Announcement, on_delete=models.CASCADE, related_name='stats')
    local = models.ForeignKey(Local, on_delete=models.PROTECT, related_name='announcement_stats')
    target_count = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    read_count = models.PositiveIntegerField(default=0)
    acknowledged_count = models.PositiveIntegerField(default=0)
    coming_count = models.PositiveIntegerField(default=0)
    cant_come_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(target_count__gte=0)
                    & models.Q(sent_count__gte=0)
                    & models.Q(failed_count__gte=0)
                    & models.Q(read_count__gte=0)
                    & models.Q(acknowledged_count__gte=0)
                    & models.Q(coming_count__gte=0)
                    & models.Q(cant_come_count__gte=0)
                ),
                name='announcement_stats_counts_non_negative',
            ),
        ]

    def clean(self):
        super().clean()

        if self.local_id and self.announcement_id and self.local_id != self.announcement.local_id:
            raise ValidationError({'local': 'Local must match the announcement local.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'Stats for {self.announcement}'
