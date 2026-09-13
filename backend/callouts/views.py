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
from callouts.tenant import for_request_local


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
    recipients = announcement_recipients_for_audience(announcement)

    AnnouncementRecipient.objects.bulk_create(
        recipients,
        batch_size=500,
        ignore_conflicts=True,
    )
    return len(recipients)


def announcement_recipients_for_audience(announcement):
    return [
        announcement_recipient_for_member(announcement, member)
        for member in announcement_audience_queryset(announcement).only('id', 'classification')
    ]


def announcement_recipient_for_member(announcement, member):
    return AnnouncementRecipient(
        local=announcement.local,
        announcement=announcement,
        member_id=member.id,
        classification_snapshot=member.classification,
    )


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

            expand_announcement_audience(announcement)
            update_announcement_target_count(announcement)
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

        announcement.status = Announcement.Status.QUEUED
        announcement.queued_at = timezone.now()
        announcement.save(update_fields=['status', 'queued_at'])
        expand_announcement_audience(announcement)
        update_announcement_target_count(announcement)

    return Response(AnnouncementSerializer(announcement).data)
