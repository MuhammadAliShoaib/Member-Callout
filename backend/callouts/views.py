import hashlib

from django.contrib.auth import authenticate
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from callouts.ai import AIError, get_announcement_draft_ai
from callouts.models import Announcement, AnnouncementRecipient, AnnouncementStats, Member
from callouts.permissions import IsActiveLeaderInOwnLocal
from callouts.serializers import AnnouncementSerializer
from callouts.tasks import deliver_recipient_batch, recipient_batch_size
from callouts.tenant import for_request_local

MAX_ANNOUNCEMENT_RECIPIENTS = 22400
RECIPIENT_BULK_CREATE_BATCH_SIZE = 500
AUDIENCE_QUERY_CHUNK_SIZE = 1000


def announcement_content_hash(announcement):
    content = f'{announcement.title}{announcement.body}{announcement.push_preview}'
    return hashlib.sha256(content.encode()).hexdigest()


def announcement_content_is_confirmed(announcement):
    return announcement.confirmed_content_hash == announcement_content_hash(announcement)


def announcement_audience_filters(announcement):
    filters = {
        'local_id': announcement.local_id,
        'status': Member.Status.ACTIVE,
        'is_active': True,
    }

    if announcement.target_classification:
        filters['classification'] = announcement.target_classification

    return filters


def announcement_audience_queryset(announcement):
    return Member.objects.filter(**announcement_audience_filters(announcement))


def announcement_audience_count(announcement):
    return announcement_audience_queryset(announcement).count()


def announcement_audience_size_error(audience_count):
    return {
        'detail': (
            f'Announcement audience has {audience_count} eligible members; '
            f'maximum is {MAX_ANNOUNCEMENT_RECIPIENTS}.'
        ),
    }


def validate_announcement_audience_size(announcement):
    audience_count = announcement_audience_count(announcement)

    if audience_count > MAX_ANNOUNCEMENT_RECIPIENTS:
        return Response(
            announcement_audience_size_error(audience_count),
            status=status.HTTP_400_BAD_REQUEST,
        )

    return None


def create_announcement_recipients_for_send(announcement):
    audience_size_error = validate_announcement_audience_size(announcement)
    if audience_size_error:
        return audience_size_error

    expand_and_enqueue_announcement_recipients(announcement)
    return None


def expand_and_enqueue_announcement_recipients(announcement):
    expand_announcement_audience(announcement)
    update_announcement_target_count(announcement)
    transaction.on_commit(lambda: enqueue_pending_recipients_for_delivery(announcement.id))


def update_announcement_target_count(announcement):
    stats, _ = AnnouncementStats.objects.get_or_create(
        announcement=announcement,
        defaults={'local': announcement.local},
    )
    stats.local = announcement.local
    stats.target_count = AnnouncementRecipient.objects.filter(
        announcement=announcement,
    ).values('member_id').distinct().count()
    stats.save(update_fields=['local', 'target_count', 'updated_at'])
    return stats


def expand_announcement_audience(announcement):
    created_count = 0
    batch = []

    for member_id, classification in announcement_audience_values(announcement):
        batch.append(announcement_recipient_for_member_values(announcement, member_id, classification))
        created_count += 1

        if len(batch) == RECIPIENT_BULK_CREATE_BATCH_SIZE:
            bulk_create_announcement_recipients(batch)
            batch = []

    if batch:
        bulk_create_announcement_recipients(batch)

    return created_count


def announcement_audience_values(announcement):
    return announcement_audience_values_queryset(announcement).iterator(
        chunk_size=AUDIENCE_QUERY_CHUNK_SIZE,
    )


def announcement_audience_values_queryset(announcement):
    return (
        announcement_audience_queryset(announcement)
        .order_by('id')
        .values_list('id', 'classification')[:MAX_ANNOUNCEMENT_RECIPIENTS]
    )


def announcement_recipient_for_member(announcement, member):
    return announcement_recipient_for_member_values(
        announcement,
        member.id,
        member.classification,
    )


def announcement_recipient_for_member_values(announcement, member_id, classification):
    return AnnouncementRecipient(
        local=announcement.local,
        announcement=announcement,
        member_id=member_id,
        classification_snapshot=classification,
    )


def bulk_create_announcement_recipients(recipients):
    AnnouncementRecipient.objects.bulk_create(
        recipients,
        batch_size=RECIPIENT_BULK_CREATE_BATCH_SIZE,
        ignore_conflicts=True,
    )


def pending_recipient_ids_for_delivery(announcement_id):
    return (
        AnnouncementRecipient.objects.filter(
            announcement_id=announcement_id,
            delivery_status=AnnouncementRecipient.DeliveryStatus.PENDING,
        )
        .order_by('id')
        .values_list('id', flat=True)
        .iterator(chunk_size=recipient_batch_size())
    )


def enqueue_pending_recipients_for_delivery(announcement_id):
    batch = []

    for recipient_id in pending_recipient_ids_for_delivery(announcement_id):
        batch.append(str(recipient_id))

        if len(batch) == recipient_batch_size():
            enqueue_recipient_delivery_batch(batch)
            batch = []

    if batch:
        enqueue_recipient_delivery_batch(batch)


def enqueue_recipient_delivery_batch(recipient_ids):
    deliver_recipient_batch.apply_async(args=[recipient_ids])


@api_view(['GET'])
@permission_classes([AllowAny])
def health(request):
    return Response({'status': 'ok'})


@api_view(['POST'])
@permission_classes([AllowAny])
def login(request):
    email = request.data.get('email')
    password = request.data.get('password')

    if not email or not password:
        return Response(
            {'detail': 'Email and password are required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    member = authenticate(request, username=email, password=password)
    if member is None:
        return Response(
            {'detail': 'Invalid email or password.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    token, _ = Token.objects.get_or_create(user=member)
    return Response(
        {
            'token': token.key,
            'token_type': 'Token',
            'member': {
                'id': str(member.id),
                'local_id': str(member.local_id),
                'full_name': member.full_name,
                'email': member.email,
                'classification': member.classification,
                'status': member.status,
                'role': member.role,
            },
        }
    )


@api_view(['POST'])
@permission_classes([IsActiveLeaderInOwnLocal])
def announcements(request):
    serializer = AnnouncementSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    announcement = serializer.save(
        local=request.user.local,
        created_by=request.user,
        status=Announcement.Status.DRAFT,
    )
    return Response(
        AnnouncementSerializer(announcement).data,
        status=status.HTTP_201_CREATED,
    )


@api_view(['POST'])
@permission_classes([IsActiveLeaderInOwnLocal])
def announcement_ai_draft(request):
    note = request.data.get('note')

    if not note:
        return Response(
            {'detail': 'Note is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        draft = get_announcement_draft_ai().draft(note)
    except AIError as exc:
        return Response(
            {'detail': exc.detail},
            status=exc.status_code,
        )

    return Response(draft)


@api_view(['GET'])
@permission_classes([IsActiveLeaderInOwnLocal])
def announcement_detail(request, announcement_id):
    announcement = get_object_or_404(
        for_request_local(Announcement.objects.all(), request),
        id=announcement_id,
    )
    return Response(AnnouncementSerializer(announcement).data)


@api_view(['POST'])
@permission_classes([IsActiveLeaderInOwnLocal])
def announcement_confirm(request, announcement_id):
    announcement = get_object_or_404(
        for_request_local(Announcement.objects.all(), request),
        id=announcement_id,
    )

    if announcement.status != Announcement.Status.DRAFT:
        return Response(
            {'detail': 'Only draft announcements can be confirmed.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    announcement.confirmed_content_hash = announcement_content_hash(announcement)
    announcement.confirmed_at = timezone.now()
    announcement.status = Announcement.Status.CONFIRMED
    announcement.save(update_fields=['confirmed_content_hash', 'confirmed_at', 'status'])

    return Response(AnnouncementSerializer(announcement).data)


@api_view(['POST'])
@permission_classes([IsActiveLeaderInOwnLocal])
def announcement_send(request, announcement_id):
    with transaction.atomic():
        announcement = get_object_or_404(
            for_request_local(Announcement.objects.select_for_update(), request),
            id=announcement_id,
        )

        if announcement.status == Announcement.Status.QUEUED:
            if not announcement_content_is_confirmed(announcement):
                return Response(
                    {'detail': 'Announcement content has changed since confirmation.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            recipient_creation_error = create_announcement_recipients_for_send(announcement)
            if recipient_creation_error:
                return recipient_creation_error

            return Response(AnnouncementSerializer(announcement).data)

        if announcement.status != Announcement.Status.CONFIRMED:
            return Response(
                {'detail': 'Only confirmed announcements can be sent.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not announcement_content_is_confirmed(announcement):
            return Response(
                {'detail': 'Announcement content has changed since confirmation.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        audience_size_error = validate_announcement_audience_size(announcement)
        if audience_size_error:
            return audience_size_error

        announcement.status = Announcement.Status.QUEUED
        announcement.queued_at = timezone.now()
        announcement.save(update_fields=['status', 'queued_at'])
        expand_and_enqueue_announcement_recipients(announcement)

    return Response(AnnouncementSerializer(announcement).data)
